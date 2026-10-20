"""Entrega (spec §5.5): hasta 15 mensajes por ciclo, 1,1 s entre envíos, por prioridad.

Cada mensaje va al tema de su categoría (spec del rastreo §5.1); si el tema no existe, sale en
«General» y se avisa una vez al día para correr `/temas`. Las filas con `card` salen como foto
(tarjeta PNG) con el texto como pie; la foto del producto solo se descarga de los hosts que la
fuente declara (`image_hosts`).

Con `channel_id`, cada oferta (`alert` y `digest`) se copia además al canal general, sin tema y
solo con los botones de enlace: los de callback solo los atiende el grupo. La copia es de mejor
esfuerzo: no se reintenta y su fallo nunca cambia el estado de la fila del grupo.

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
from urllib.parse import urljoin, urlsplit

import psycopg

from discountz.app.render import source_name
from discountz.config import load_topics
from discountz.domain.errors import (
    DiscountzError,
    SourceError,
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

CHANNEL_PURPOSES = frozenset({"alert", "digest"})

MAX_PHOTO_REDIRECTS = 2
PHOTO_ATTEMPTS = 3  # el CDN de Telegram (telesco.pe) da 502 pasajeros
PHOTO_RETRY_S = 1.0
REDIRECTS = frozenset({301, 302, 303, 307, 308})
PhotoFetcher = Callable[[str, str], bytes | None]  # (fuente, URL de la foto) → bytes o None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    sent: int
    forbidden: bool


def plain_text(body: str) -> str:
    return html.unescape(_TAG_RE.sub("", body))


def make_photo_fetcher(
    http: HttpClient,
    sources: Mapping[str, Source],
    *,
    sleep: Callable[[float], None] = lambda _: None,
) -> PhotoFetcher:
    """Descarga la foto solo por https y desde los `image_hosts` de la fuente; valida la imagen.

    Sigue hasta `MAX_PHOTO_REDIRECTS` redirecciones, cada una a un host permitido (el CDN de
    VTEX puede mandar de `vteximg.com.br` a `vtexassets.com`). Un 5xx o un error de red se
    reintenta hasta `PHOTO_ATTEMPTS` veces con pausa creciente. Sin foto, registra el porqué.
    """

    def allowed(src: Source, url: str) -> bool:
        parts = urlsplit(url)
        return (
            parts.scheme == "https" and (parts.hostname or "").lower() in src.descriptor.image_hosts
        )

    def missing(source: str, code: str, status: int | None = None) -> None:
        event(log, "photo_missing", source=source, code=code, http_status=status)

    def fetch(source: str, url: str) -> bytes | None:
        src = sources.get(source)
        if src is None or not allowed(src, url):
            return None
        for attempt in range(1, PHOTO_ATTEMPTS + 1):
            photo, retry = download(src, source, url, last=attempt == PHOTO_ATTEMPTS)
            if not retry:
                return photo
            sleep(PHOTO_RETRY_S * attempt)
        return None

    def download(src: Source, source: str, url: str, *, last: bool) -> tuple[bytes | None, bool]:
        """(foto, reintentar): solo un 5xx o un error de red merecen otro intento."""
        for _ in range(MAX_PHOTO_REDIRECTS + 1):
            try:
                resp = http.request("GET", url)
            except DiscountzError as exc:
                if not last:
                    return None, True
                missing(source, exc.code if isinstance(exc, SourceError) else "network")
                return None, False
            if resp.status not in REDIRECTS:
                break
            target = {k.lower(): v for k, v in resp.headers.items()}.get("location", "")
            url = urljoin(url, target)
            if not allowed(src, url):
                missing(source, "redirect", resp.status)
                return None, False
        else:
            missing(source, "redirect_loop")
            return None, False
        if resp.status >= 500 and not last:
            return None, True
        if resp.status != 200:
            missing(source, "status", resp.status)
            return None, False
        photo = validate_photo(resp.body)
        if photo is None:
            missing(source, "invalid")
        return photo, False

    return fetch


@cache
def _topic_titles() -> Mapping[str, str]:
    """Título de cada tema sin el emoji (Inter no lo dibuja en la tarjeta)."""
    out = {}
    for code, p in load_topics().params.items():
        head, _, rest = p.title.partition(" ")
        out[code] = rest if rest and not any(ch.isalnum() for ch in head) else p.title
    return out


def link_buttons_only(markup: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Teclado sin botones de callback (en el canal nadie los puede atender)."""
    if markup is None:
        return None
    rows = [
        [b for b in row if isinstance(b, Mapping) and "url" in b]
        for row in markup.get("inline_keyboard", [])
    ]
    rows = [row for row in rows if row]
    return {"inline_keyboard": rows} if rows else None


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
    channel_id: int | None = None,
) -> DeliveryResult:
    rows = db.claim_outbox(conn, limit, now)
    threads = db.topic_threads(conn)
    sent = 0
    missing_topic = False
    channel_ok = channel_id is not None

    def mirror(row: db.OutboxRow, png: bytes | None, body: str, parse_mode: str | None) -> None:
        nonlocal channel_ok
        if channel_id is None or not channel_ok or row.purpose not in CHANNEL_PURPOSES:
            return
        sleep(PACE_S)
        markup = link_buttons_only(row.reply_markup)
        try:
            if png is not None and parse_mode is not None:
                tg.send_photo(channel_id, png, body, markup, silent=row.silent, thread_id=None)
            else:
                tg.send_message(channel_id, body, markup, silent=row.silent, parse_mode=parse_mode)
        except (TelegramForbidden, TelegramBadRequest, TelegramRetryAfter) as exc:
            # Sin permiso, id errado o límite de tasa: no se insiste con el canal en este ciclo.
            channel_ok = False
            event(log, "channel_off", logging.ERROR, error=type(exc).__name__)
        except TelegramError as exc:
            event(log, "channel_failed", ref=f"O{row.id}", code=str(exc))

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

        mirrored: tuple[str, str | None] = (row.body_html, "HTML")
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
            mirrored = (plain_text(row.body_html), None)
            try:
                message_id = send(*mirrored, thread=thread)
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
        mirror(row, png, *mirrored)
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
