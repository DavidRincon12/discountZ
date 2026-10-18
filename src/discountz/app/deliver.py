"""Entrega (spec §5.5): hasta 15 mensajes por ciclo, 1,1 s entre envíos, por prioridad.

Cada mensaje va al tema de su categoría (spec del rastreo §5.1); si el tema no existe, sale en
«General» y se avisa una vez al día para correr `/temas`. Las filas con `card` salen como foto
(tarjeta PNG) con el texto como pie; la foto del producto solo se descarga de los hosts que la
fuente declara (`image_hosts`).

Se confirma `sending` antes de llamar a la API (`claim_outbox`): una caída entre el envío y el
`sent` produce como mucho un duplicado (at-least-once, aceptado).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from typing import Any
from urllib.parse import urlsplit

import psycopg

from discountz.app.render import source_name
from discountz.config import load_topics
from discountz.domain.errors import (
    DiscountzError,
    TelegramBadRequest,
    TelegramError,
    TelegramForbidden,
    TelegramRetryAfter,
    TelegramThreadNotFound,
)
from discountz.domain.model import AlertKind
from discountz.domain.money import Currency, Money
from discountz.infra import db
from discountz.infra.card import DEFAULT_COLOR, STORE_COLORS, CardData, render_card, validate_photo
from discountz.infra.http import HttpClient
from discountz.infra.log import event
from discountz.infra.telegram import TelegramClient
from discountz.sources.base import Source

log = logging.getLogger(__name__)

MAX_PER_CYCLE = 15
PACE_S = 1.1
MAX_RETRY_AFTER_S = 30
_TAG_RE = re.compile(r"<[^>]+>")
NOTICE_TTL = timedelta(hours=24)
TOPICS_NOTICE = (
    "Hay alertas que salieron en General porque falta su tema en el grupo. "
    "Escribe /temas para crearlos."
)

PhotoFetcher = Callable[[str, str], bytes | None]  # (fuente, URL de la foto) → bytes o None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    sent: int
    forbidden: bool


def plain_text(body: str) -> str:
    return html.unescape(_TAG_RE.sub("", body))


def make_photo_fetcher(http: HttpClient, sources: Mapping[str, Source]) -> PhotoFetcher:
    """Descarga la foto solo por https y desde los `image_hosts` de la fuente; valida la imagen."""

    def fetch(source: str, url: str) -> bytes | None:
        src = sources.get(source)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if src is None or parts.scheme != "https" or host not in src.descriptor.image_hosts:
            return None
        try:
            resp = http.request("GET", url)
        except DiscountzError:
            return None
        return validate_photo(resp.body) if resp.status == 200 else None

    return fetch


@cache
def _topic_titles() -> Mapping[str, str]:
    """Título de cada tema sin el emoji (Inter no lo dibuja en la tarjeta)."""
    out = {}
    for code, p in load_topics().params.items():
        head, _, rest = p.title.partition(" ")
        out[code] = rest if rest and not any(ch.isalnum() for ch in head) else p.title
    return out


def card_png(card: Mapping[str, Any], photo: bytes | None) -> bytes:
    store = str(card["store"])
    currency = Currency(str(card["currency"]))
    topic = str(card["topic"])
    data = CardData(
        store_name=source_name(store),
        store_color=STORE_COLORS.get(store, DEFAULT_COLOR),
        topic_title=_topic_titles().get(topic, topic),
        price=Money(int(card["price_minor"]), currency),
        usual=Money(int(card["usual_minor"]), currency) if "usual_minor" in card else None,
        drop_bp=int(card["drop_bp"]) if "drop_bp" in card else None,
        min_days=int(card["min_days"]) if "min_days" in card else None,
        kind=AlertKind(str(card["kind"])),
        badge=str(card["badge"]) if card.get("badge") else None,
        coupon=str(card["coupon"]) if card.get("coupon") else None,
        reference_label=str(card.get("reference_label") or "Antes"),
        note=str(card["note"]) if card.get("note") else None,
    )
    return render_card(data, photo)


def deliver(
    conn: psycopg.Connection,
    tg: TelegramClient,
    chat_id: int,
    now: datetime,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    deadline: float,
    photos: PhotoFetcher | None = None,
    limit: int = MAX_PER_CYCLE,
) -> DeliveryResult:
    rows = db.claim_outbox(conn, limit, now)
    threads = db.topic_threads(conn)
    sent = 0
    missing_topic = False
    for i, row in enumerate(rows):
        if i:
            sleep(PACE_S)
        thread = threads.get(row.topic) if row.topic else None
        if row.topic and thread is None and threads:  # sin temas aún: todo a General, sin aviso
            missing_topic = True
        png = None
        if row.card is not None:
            url = row.card.get("image_url")
            photo = photos(str(row.card["store"]), url) if photos and isinstance(url, str) else None
            png = card_png(row.card, photo)

        def send(
            body: str,
            parse_mode: str | None = "HTML",
            row: db.OutboxRow = row,
            png: bytes | None = png,
            thread: int | None = thread,
        ) -> int:
            if png is not None and parse_mode is not None:
                return tg.send_photo(
                    chat_id, png, body, row.reply_markup, silent=row.silent, thread_id=thread
                )
            return tg.send_message(
                chat_id,
                body,
                row.reply_markup,
                silent=row.silent,
                reply_to_message_id=row.reply_to_message_id,
                parse_mode=parse_mode,
                thread_id=thread,
            )

        def send_anywhere(body: str, topic: str | None = row.topic) -> int:
            nonlocal thread, missing_topic
            try:
                return send(body, thread=thread)
            except TelegramThreadNotFound:
                # Tema borrado en Telegram: se olvida para que /temas lo vuelva a crear.
                if topic is not None:
                    threads.pop(topic, None)
                    with conn.transaction():
                        db.forget_topic_thread(conn, topic)
                thread, missing_topic = None, True
                return send(body, thread=None)

        try:
            try:
                message_id = send_anywhere(row.body_html)
            except TelegramRetryAfter as exc:
                wait = exc.retry_after
                if wait > MAX_RETRY_AFTER_S or monotonic() + wait >= deadline:
                    raise
                sleep(wait)
                message_id = send_anywhere(row.body_html)
        except TelegramRetryAfter:
            db.release_outbox(conn, [r.id for r in rows[i:]])
            event(log, "deliver_rate_limited", count=len(rows) - i)
            break
        except TelegramBadRequest:
            try:
                message_id = send(plain_text(row.body_html), None, thread=thread)
            except TelegramError as exc:
                db.mark_retry(conn, row.id, str(exc))
                continue
            event(log, "render_bug", ref=f"O{row.id}", level=logging.WARNING)
        except TelegramForbidden:
            db.mark_failed(conn, row.id, "forbidden")
            db.release_outbox(conn, [r.id for r in rows[i + 1 :]])
            event(log, "telegram_forbidden", logging.ERROR)
            return DeliveryResult(sent, forbidden=True)
        except TelegramError as exc:
            db.mark_retry(conn, row.id, str(exc))
            continue
        db.mark_sent(conn, row.id, message_id, now)
        sent += 1
    if missing_topic:
        db.enqueue(
            conn,
            "system",
            TOPICS_NOTICE,
            priority=2,
            dedup_key=f"topics-missing:{now:%Y%m%d}",
            now=now,
            ttl=NOTICE_TTL,
        )
    return DeliveryResult(sent, forbidden=False)
