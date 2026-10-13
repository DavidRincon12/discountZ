"""Entrega (spec §5.5): hasta 15 mensajes por ciclo, 1,1 s entre envíos, por prioridad.

Se confirma `sending` antes de llamar a la API (`claim_outbox`): una caída entre el envío y el
`sent` produce como mucho un duplicado (at-least-once, aceptado).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psycopg

from discountz.domain.errors import (
    TelegramBadRequest,
    TelegramError,
    TelegramForbidden,
    TelegramRetryAfter,
)
from discountz.infra import db
from discountz.infra.log import event
from discountz.infra.telegram import TelegramClient

log = logging.getLogger(__name__)

MAX_PER_CYCLE = 15
PACE_S = 1.1
MAX_RETRY_AFTER_S = 30
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    sent: int
    forbidden: bool


def plain_text(body: str) -> str:
    return html.unescape(_TAG_RE.sub("", body))


def deliver(
    conn: psycopg.Connection,
    tg: TelegramClient,
    chat_id: int,
    now: datetime,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    deadline: float,
    limit: int = MAX_PER_CYCLE,
) -> DeliveryResult:
    rows = db.claim_outbox(conn, limit, now)
    sent = 0
    for i, row in enumerate(rows):
        if i:
            sleep(PACE_S)

        def send(body: str, parse_mode: str | None = "HTML", row: db.OutboxRow = row) -> int:
            return tg.send_message(
                chat_id,
                body,
                row.reply_markup,
                silent=row.silent,
                reply_to_message_id=row.reply_to_message_id,
                parse_mode=parse_mode,
            )

        try:
            try:
                message_id = send(row.body_html)
            except TelegramRetryAfter as exc:
                wait = exc.retry_after
                if wait > MAX_RETRY_AFTER_S or monotonic() + wait >= deadline:
                    raise
                sleep(wait)
                message_id = send(row.body_html)
        except TelegramRetryAfter:
            db.release_outbox(conn, [r.id for r in rows[i:]])
            event(log, "deliver_rate_limited", count=len(rows) - i)
            break
        except TelegramBadRequest:
            try:
                message_id = send(plain_text(row.body_html), None)
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
    return DeliveryResult(sent, forbidden=False)
