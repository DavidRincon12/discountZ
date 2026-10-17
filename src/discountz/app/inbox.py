"""Bandeja de entrada de Telegram: comandos y botones, exactamente una vez en efecto (spec §5.4).

Cada update se aplica en UNA transacción junto con su respuesta (outbox) y el avance del offset
(`WHERE telegram_offset < $1`): reaplicarlo es un no-op. Un update que lanza excepción se aparta
en una segunda transacción para que no bloquee la cola. Los updates ajenos (otro `from.id` o
`chat.id`) se descartan sin tocar la BD; su offset avanza en lote al final.
"""

from __future__ import annotations

import contextlib
import html
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

import psycopg

from discountz.app.render import esc, render_health, render_list, source_name
from discountz.config import load_topics
from discountz.domain import commands as c
from discountz.domain.errors import ParseError, TelegramError, UnsafeUrl
from discountz.domain.model import ListingRef
from discountz.domain.money import Currency, parse_price
from discountz.infra import db
from discountz.infra.telegram import CallbackQuery, IncomingMessage, TelegramApi, Update
from discountz.sources.base import Capability, NeedsResolution, Source
from discountz.sources.registry import SOURCES, route_url

log = logging.getLogger(__name__)

REPLY_PRIORITY = 1
REPLY_TTL = timedelta(hours=24)
RAMP = timedelta(days=7)
_MUTE_CB = re.compile(r"^mute:L(\d{1,12}):(\d{1,2})$")
_UNF_CB = re.compile(r"^unf:p(\d{1,9})$")
_FOL_CB = re.compile(r"^fol:L(\d{1,12})$")

HELP = (
    "<b>Comandos</b> (respondo en el siguiente ciclo, 10-40 min)\n"
    "/seguir &lt;url…&gt; [objetivo] [#cat]: hasta 20 URLs, una por línea\n"
    "/buscar &lt;fuente&gt; &lt;consulta&gt; [-excluir] [min:p] [max:p] [#cat]\n"
    "/lista [#cat|fuente] · /dejar &lt;p12|a12|b3|auto&gt;\n"
    "/objetivo &lt;p12&gt; &lt;precio|no&gt; · /categoria &lt;p12|L1234&gt; &lt;cat&gt;\n"
    "/silenciar &lt;p12|L1234|todo&gt; &lt;días&gt; · /fuente &lt;on|off&gt; &lt;codigo&gt;\n"
    "/token_vence AAAA-MM-DD · /temas · /estado · /ayuda"
)
TOPICS_FAILED = (
    "No pude crear los temas. Revisa que el grupo tenga los temas activados y que el bot sea"
    " administrador con permiso para gestionar temas; luego escribe /temas otra vez."
)
_URL_REASONS = {
    "userinfo": "lleva usuario o contraseña",
    "host_not_allowed": "tienda no admitida",
    "unsupported_path": "no es una página de producto",
    "not_https": "debe empezar por https://",
    "port": "puerto no permitido",
    "ip_literal": "usa una IP en lugar de un dominio",
}


def process_inbox(
    conn: psycopg.Connection,
    tg: TelegramApi,
    owner_id: int,
    chat_id: int,
    now: datetime,
    max_seconds: int = 20,
    *,
    sources: Mapping[str, Source] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Procesa los updates pendientes durante hasta `max_seconds`; devuelve cuántos de David."""
    deadline = monotonic() + max_seconds
    offset = db.telegram_offset(conn) + 1
    processed = 0
    while monotonic() < deadline:
        updates = tg.get_updates(offset, timeout_s=0)
        if not updates:
            break
        for upd in updates:
            offset = max(offset, upd.update_id + 1)
            if not _is_owner(upd, owner_id, chat_id):
                continue
            if _is_topics(upd):
                _topics(conn, tg, upd, chat_id, now)
            else:
                _apply(conn, tg, upd, now, sources)
            processed += 1
    with conn.transaction():
        db.advance_offset(conn, offset - 1)  # ajenos al final del lote: un solo UPDATE
    return processed


def _is_owner(upd: Update, owner_id: int, chat_id: int) -> bool:
    if upd.message is not None:
        return upd.message.from_id == owner_id and upd.message.chat_id == chat_id
    if upd.callback is not None:
        return upd.callback.from_id == owner_id and upd.callback.chat_id == chat_id
    return False


def _is_topics(upd: Update) -> bool:
    text = upd.message.text if upd.message is not None else None
    return text is not None and isinstance(c.parse_command(text), c.Topics)


def _topics(
    conn: psycopg.Connection, tg: TelegramApi, upd: Update, chat_id: int, now: datetime
) -> None:
    """`/temas`: crea en el momento los temas que falten (HTTP fuera de toda transacción)."""
    with conn.transaction():
        if not db.advance_offset(conn, upd.update_id):
            return
    try:
        text = _create_topics(conn, tg, chat_id)
    except TelegramError as exc:
        log.error("topics_failed error=%s", type(exc).__name__)
        text = TOPICS_FAILED
    with conn.transaction():
        _reply(conn, upd.update_id, 0, text, _reply_to(upd), now)


def _create_topics(conn: psycopg.Connection, tg: TelegramApi, chat_id: int) -> str:
    existing = db.topic_threads(conn)
    created = []
    for code, params in load_topics().params.items():
        if code in existing:
            continue
        thread_id = tg.create_forum_topic(chat_id, params.title)
        with conn.transaction():  # uno a uno: si Telegram falla a la mitad, lo creado queda
            db.save_topic(conn, code, params.title, thread_id)
        created.append(params.title)
    if not created:
        return "Todos los temas ya existen."
    return f"Creé {len(created)} temas: " + ", ".join(esc(t) for t in created) + "."


def _apply(
    conn: psycopg.Connection,
    tg: TelegramApi,
    upd: Update,
    now: datetime,
    sources: Mapping[str, Source] | None,
) -> None:
    reply_to = _reply_to(upd)
    try:
        with conn.transaction():
            if db.advance_offset(conn, upd.update_id):
                for i, text in enumerate(_handle(conn, upd, now, sources)):
                    _reply(conn, upd.update_id, i, text, reply_to, now)
    except Exception as exc:  # update venenoso: no puede bloquear la cola
        with conn.transaction():
            if db.advance_offset(conn, upd.update_id):
                text = f"No pude procesar ese mensaje (ref U{upd.update_id})."
                _reply(conn, upd.update_id, 0, text, reply_to, now)
        log.error("update_failed update_id=%s error=%s", upd.update_id, type(exc).__name__)
    if upd.callback is not None:
        # Fuera de la transacción: nunca HTTP dentro de una. Casi siempre llega tarde.
        with contextlib.suppress(TelegramError):
            tg.answer_callback(upd.callback.id)


def _reply_to(upd: Update) -> int | None:
    if upd.message is not None:
        return upd.message.message_id
    if upd.callback is not None:
        return upd.callback.message_id
    return None


def _reply(
    conn: psycopg.Connection,
    update_id: int,
    index: int,
    body: str,
    reply_to: int | None,
    now: datetime,
) -> None:
    key = f"reply:{update_id}" if index == 0 else f"reply:{update_id}:{index}"
    db.enqueue(
        conn,
        "reply",
        body,
        priority=REPLY_PRIORITY,
        dedup_key=key,
        now=now,
        ttl=REPLY_TTL,
        reply_to_message_id=reply_to,
    )


def _handle(
    conn: psycopg.Connection, upd: Update, now: datetime, sources: Mapping[str, Source] | None
) -> list[str]:
    if upd.message is not None:
        return _handle_message(conn, upd.message, now, sources)
    if upd.callback is not None:
        return [_handle_callback(conn, upd.callback, now)]
    return []


def _handle_message(
    conn: psycopg.Connection,
    m: IncomingMessage,
    now: datetime,
    sources: Mapping[str, Source] | None,
) -> list[str]:
    if m.text is None:
        return []
    cmd = c.parse_command(m.text)
    match cmd:
        case c.CommandRejected(reason):
            return [esc(reason)]
        case c.Follow():
            return [_follow(conn, cmd, m.message_id, now, sources)]
        case c.Search():
            return [_search(conn, cmd, now, sources)]
        case c.ListCmd(filter=flt):
            return _list(conn, flt)
        case c.Drop("auto"):
            n = db.drop_auto_watches(conn)
            noun = "seguimiento automático" if n == 1 else "seguimientos automáticos"
            return [f"Listo: quité {n} {noun}."]
        case c.Drop(ref):
            ok = db.drop_ref(conn, ref)
            return [f"Listo: dejé {ref}." if ok else f"No encontré {ref}."]
        case c.Target():
            return [_target(conn, cmd, now)]
        case c.Category(ref, category):
            ok = db.set_category(conn, ref, category)
            return [f"{ref} ahora es #{category}." if ok else f"No encontré {ref}."]
        case c.Silence(ref, days):
            ok = db.mute(conn, ref, now + timedelta(days=days))
            what = "Todo" if ref == "todo" else ref
            if ok:
                return [f"{what} silenciado {days} d."]
            return [f"No encontré {ref} (o aún no tiene la primera lectura)."]
        case c.SourceToggle(source, enabled):
            ok = db.set_source_enabled(conn, source, enabled, now + RAMP if enabled else None)
            if not ok:
                return [f"No conozco la fuente {esc(source)}."]
            state = "activada (presupuesto al 50 % durante 7 días)" if enabled else "apagada"
            return [f"Fuente {esc(source)} {state}."]
        case c.TokenExpiry(day):
            db.set_token_expiry(conn, day)
            return [f"Anotado: el token de disparo vence el {day.isoformat()}."]
        case c.Status():
            return [render_health(db.status_snapshot(conn, now), now).html]
        case c.Help():
            return [HELP]
        case c.Topics():
            return []  # lo atiende `_topics` antes de llegar aquí
    return []  # pragma: no cover - `match` exhaustivo


def _url_rejected(i: int, exc: UnsafeUrl) -> str:
    return f"URL {i} rechazada: {_URL_REASONS.get(exc.code, 'URL no válida')}."


def _follow(
    conn: psycopg.Connection,
    cmd: c.Follow,
    message_id: int,
    now: datetime,
    sources: Mapping[str, Source] | None,
) -> str:
    lines = []
    for i, raw in enumerate(cmd.urls, start=1):
        try:
            source, ref = route_url(raw, sources)
        except UnsafeUrl as exc:
            lines.append(_url_rejected(i, exc))
            continue
        code = source.descriptor.code
        target = None
        if cmd.target_minor_text is not None:
            try:
                target = parse_price(cmd.target_minor_text, source.descriptor.currency)
            except ParseError:
                lines.append(c.PRICE_HELP)
                continue
        category = cmd.category or c.DEFAULT_CATEGORY
        created = _create_watch(conn, code, ref, category, target, now)
        name = esc(source_name(code))
        if created.created:
            db.set_watch_reply_to(conn, created.id, message_id)
            lines.append(
                f"p{created.id}: recibido ({name}). "
                "Te confirmo precio y stock tras la primera lectura."
            )
        else:
            lines.append(f"Ya lo sigues (p{created.id}).")
    return "\n".join(lines)


def _create_watch(
    conn: psycopg.Connection,
    code: str,
    ref: ListingRef | NeedsResolution,
    category: str,
    target: int | None,
    now: datetime,
) -> db.WatchCreated:
    if isinstance(ref, NeedsResolution):
        return db.create_watch(conn, code, ref.pending_ref, category, target, now)
    listing_id = db.find_listing(conn, code, ref.external_id)
    if listing_id is not None:
        return db.create_watch_on_listing(conn, code, listing_id, target, now)
    return db.create_watch(conn, code, f"id:{ref.external_id}", category, target, now)


def _price(text: str | None, currency: Currency) -> int | None:
    return None if text is None else parse_price(text, currency)


def _search(
    conn: psycopg.Connection, cmd: c.Search, now: datetime, sources: Mapping[str, Source] | None
) -> str:
    source = (SOURCES if sources is None else sources).get(cmd.source)
    d = source.descriptor if source is not None else None
    if d is not None and (
        d.kind == "channel" or (d.kind == "store" and Capability.SEARCH not in d.capabilities)
    ):
        name = esc(source_name(cmd.source))
        return f"{name} no admite /buscar; usa /seguir con la URL del producto."
    currency = db.source_currency(conn, cmd.source)
    if currency is None:
        return f"No conozco la fuente {esc(cmd.source)}."
    try:
        low, high = _price(cmd.min_text, currency), _price(cmd.max_text, currency)
    except ParseError:
        return c.PRICE_HELP
    if low is not None and high is not None and low >= high:
        return "El mínimo debe ser menor que el máximo."
    sid = db.create_search(conn, cmd.source, cmd.query, cmd.category, cmd.exclude, low, high, now)
    return f"Búsqueda b{sid}: «{esc(cmd.query)}» en {esc(cmd.source)} (#{cmd.category})."


def _list(conn: psycopg.Connection, flt: str | None) -> list[str]:
    watches, searches = db.list_watches(conn), db.list_searches(conn)
    if flt is not None:
        if flt.startswith("#"):
            cat = flt[1:].lower()
            watches = [w for w in watches if w.category == cat]
            searches = [s for s in searches if s.category == cat]
        else:
            watches = [w for w in watches if w.source == flt]
            searches = [s for s in searches if s.source == flt]
    return [m.html for m in render_list(watches, searches)]


def _target(conn: psycopg.Connection, cmd: c.Target, now: datetime) -> str:
    watch_id = int(cmd.ref[1:])
    currency = db.watch_currency(conn, watch_id)
    if currency is None:
        return f"No encontré {cmd.ref}."
    try:
        target = _price(cmd.price_text, currency)
    except ParseError:
        return c.PRICE_HELP
    db.set_target(conn, watch_id, target, now)
    if target is None:
        return f"{cmd.ref}: objetivo quitado."
    return f"{cmd.ref}: objetivo {html.escape(cmd.price_text or '')} ({currency.value})."


def _handle_callback(conn: psycopg.Connection, q: CallbackQuery, now: datetime) -> str:
    data = q.data or ""
    if m := _MUTE_CB.fullmatch(data):
        days = int(m.group(2))
        if days not in c.MUTE_DAYS:
            return "Botón no reconocido."
        ok = db.mute(conn, f"L{m.group(1)}", now + timedelta(days=days))
        return f"L{m.group(1)} silenciado {days} d." if ok else f"No encontré L{m.group(1)}."
    if m := _UNF_CB.fullmatch(data):
        ref = f"p{m.group(1)}"
        # El botón va en alertas de seguimientos manuales y automáticos.
        ok = db.drop_ref(conn, ref) or db.drop_ref(conn, f"a{m.group(1)}")
        return f"Listo: dejé de seguir {ref}." if ok else f"No encontré {ref}."
    if m := _FOL_CB.fullmatch(data):
        listing_id = int(m.group(1))
        source = db.listing_source(conn, listing_id)
        if source is None:
            return f"No encontré L{listing_id}."
        created = db.create_watch_on_listing(conn, source, listing_id, None, now)
        verb = "Siguiendo" if created.created else "Ya lo sigues"
        return f"{verb}: p{created.id} (L{listing_id})."
    return "Botón no reconocido."
