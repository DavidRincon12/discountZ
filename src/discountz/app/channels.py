"""Paso del ciclo que lee los canales de ofertas de Telegram (spec de canales §2, §4 y §5).

Por canal: primero las peticiones (hasta `MAX_PAGES` páginas, sin transacción abierta); luego,
sin HTTP, una transacción por página que guarda los posts, encola las ofertas nuevas y avanza
el cursor. Si la transacción falla, el cursor no se mueve y la página se relee en el ciclo
siguiente; `dedup_key` e `insert_channel_post` impiden reenviar lo que ya salió.

Los feeds cuyos ids no crecen con la publicación (`ordered_ids=False`: DealNews, Slickdeals
Amazon) se leen enteros en cada lectura, sin `after`: se saltan los ids ya guardados en
`channel_post`. Su primera lectura, como la de un canal, no envía nada: guarda lo que hay como
visto (`baseline`).

Lo que llega por casillero (`ChannelPost.casillero`) con precio bajo o de mercado tampoco se
envía: queda visto igual (calidad de las ofertas, 2026-10-22).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import cast

import psycopg

from discountz.app.collect import SourceRun
from discountz.app.compose import ALERT_TTL, is_quiet
from discountz.app.links import LinkResolver
from discountz.app.render import channel_card, render_channel_deal, reply_markup
from discountz.config import AppParams, Topics
from discountz.domain.community import ChannelPost, assign_channel_topic, find_duplicate
from discountz.domain.errors import BudgetExhausted, HardBlock, ParseError, SourceError
from discountz.domain.health import BreakerEvent, BreakerState, breaker_transition, effective_state
from discountz.domain.model import normalize_title
from discountz.domain.money import Currency
from discountz.domain.scheduling import SourceBudget
from discountz.domain.topics import GROCERY_TOPIC
from discountz.infra import db
from discountz.infra.log import event
from discountz.sources.base import ChannelPage, ChannelSource, NoCredentials, Source, SourceContext
from discountz.sources.telegram_channels import FORMAT_BROKEN_MIN

log = logging.getLogger(__name__)

MAX_PAGES = 3
MAX_AGE = timedelta(hours=6)  # un post más viejo se marca leído sin enviarse
RECENT_WINDOW = timedelta(hours=24)  # ventana de duplicados entre canales
PRIORITY = 9  # después de las alertas propias


@dataclass(frozen=True, slots=True)
class ChannelsResult:
    requests: int
    sent: int
    duplicates: int
    skipped: int


@dataclass(frozen=True, slots=True)
class _Step:
    """Lo que comparten todos los canales en una pasada del ciclo."""

    conn: psycopg.Connection
    now: datetime
    topics: Topics
    recent: list[db.RecentPost]  # enviadas en la ventana de duplicados; crece con cada envío
    silent: bool
    min_price: Mapping[Currency, int]  # lo que llega por casillero cuesta al menos esto


@dataclass(slots=True)
class _Tally:
    posts: int = 0
    sent: int = 0
    duplicates: int = 0
    skipped: int = 0


def due_channels(
    conn: psycopg.Connection,
    sources: Mapping[str, Source],
    budgets: Mapping[str, SourceBudget],
    breakers: Mapping[str, BreakerState],
    now: datetime,
    interval: timedelta,
) -> Sequence[str]:
    """Canales habilitados, con breaker no abierto y sin leer en el último `interval`.

    Un canal cuyo presupuesto pide más espacio entre lecturas (`watch_interval`) espera eso.
    """
    due = []
    for code, source in sources.items():
        if source.descriptor.kind != "channel" or code not in budgets:
            continue
        breaker = breakers.get(code)
        if breaker is not None and effective_state(breaker, now) == "open":
            continue
        cursor = db.get_channel_cursor(conn, code)
        wait = max(interval, budgets[code].watch_interval)
        if cursor is None or cursor.updated_at + wait <= now:
            due.append(code)
    return due


def run_channels(
    conn: psycopg.Connection,
    runs: Sequence[SourceRun],
    now: datetime,
    params: AppParams,
    topics: Topics,
    rnd: random.Random,
    deadline: float,
    *,
    resolve: LinkResolver | None = None,
) -> ChannelsResult:
    """Lee cada canal de `runs` y encola sus ofertas nuevas; actualiza breaker y contadores.

    `resolve` cambia el enlace de cada oferta nueva por el de la tienda (`app.links`).
    """
    step = _Step(
        conn,
        now,
        topics,
        list(db.recent_channel_posts(conn, now - RECENT_WINDOW)),
        is_quiet(now, params.quiet_start, params.quiet_end),
        params.glob.min_price,
    )
    total = _Tally()
    requests = 0
    for r in runs:
        before = r.http.used
        tally = _Tally()
        try:
            _run_one(step, r, rnd, deadline, tally, resolve)
        except Exception as exc:  # aislamiento por canal: el ciclo sigue
            r.failed = True
            event(log, "channel_failed", logging.ERROR, source=r.code, error=type(exc).__name__)
        made = r.http.used - before
        requests += made
        event(
            log,
            "channel_done",
            source=r.code,
            requests=made,
            posts=tally.posts,
            sent=tally.sent,
            duplicates=tally.duplicates,
            skipped=tally.skipped,
        )
        total.sent += tally.sent
        total.duplicates += tally.duplicates
        total.skipped += tally.skipped
    return ChannelsResult(requests, total.sent, total.duplicates, total.skipped)


def _run_one(
    step: _Step,
    r: SourceRun,
    rnd: random.Random,
    deadline: float,
    tally: _Tally,
    resolve: LinkResolver | None,
) -> None:
    conn, now = step.conn, step.now
    source = cast(ChannelSource, r.source)
    ordered = source.descriptor.ordered_ids
    cursor = db.get_channel_cursor(conn, r.code)
    after = cursor.last_id if cursor is not None and ordered else None
    pages, clean = _fetch(r, source, after, now, rnd, deadline)
    if not pages:
        return
    # Racha de publicaciones con precio y sin oferta: con 3 min entre lecturas casi nunca
    # llegan 5 en una página, así que el formato roto se detecta sumando entre lecturas.
    previous = cursor.unparsed_streak if cursor else 0
    found = any(p.posts for p in pages)
    streak = 0 if found else previous + sum(p.unparsed for p in pages)
    if not ordered:
        first = cursor is None
        since = None if source.descriptor.refeatures else now - MAX_AGE
        _read_unordered(step, r.code, pages, tally, streak, first, since)
    elif cursor is None:  # primera lectura: solo se fija el punto de partida
        top = max((p.max_id for p in pages if p.max_id is not None), default=None)
        if top is not None:
            with conn.transaction():
                db.save_channel_cursor(conn, r.code, top, now, unparsed_streak=streak)
    else:
        if resolve is not None:  # antes de abrir la transacción: no hay HTTP dentro de ella
            pages = [_resolved(page, resolve) for page in pages]
        last_id = cursor.last_id
        for page in pages:
            with conn.transaction():
                for post in sorted(page.posts, key=lambda p: p.post_id):
                    if post.post_id <= cursor.last_id:  # defensa: `after` ya debería excluirlo
                        continue
                    _handle(step, r.code, post, tally)
                tally.skipped += page.skipped
                last_id = max(last_id, page.max_id or last_id)
                db.save_channel_cursor(conn, r.code, last_id, now, unparsed_streak=streak)
    if streak >= FORMAT_BROKEN_MIN:
        _on_error(r, ParseError("format", loc=r.code), now, rnd)
    elif clean:
        r.breaker = breaker_transition(r.breaker, BreakerEvent.OK, now, rnd)


def _resolved(page: ChannelPage, resolve: LinkResolver) -> ChannelPage:
    posts = tuple(replace(p, link=resolve(p.link)) for p in page.posts)
    return replace(page, posts=posts)


def _read_unordered(
    step: _Step,
    code: str,
    pages: Sequence[ChannelPage],
    tally: _Tally,
    streak: int,
    first: bool,
    since: datetime | None,
) -> None:
    """Un feed sin orden de ids trae su ventana entera en cada lectura.

    Lo ya guardado y lo publicado antes de `since` se salta sin contarse, igual que los
    `skipped` de la página: se repiten en cada lectura. Sin `since` (DealNews) no hay tope.
    Se envía de la publicación más vieja a la más nueva, para que la primera sea la original
    si hay duplicados. En la primera lectura (`first`) todo queda marcado sin enviarse; si no
    trae ninguna oferta legible (feed vacío o formato roto), no fija nada y la siguiente vuelve
    a ser primera: así no envía de golpe lo que ya estaba en el feed.
    """
    if first and not any(p.posts for p in pages):
        return
    top = max((p.max_id for p in pages if p.max_id is not None), default=None)
    fresh = [p for page in pages for p in page.posts if since is None or p.published_at >= since]
    seen = db.channel_post_ids(step.conn, code, [p.post_id for p in fresh])
    with step.conn.transaction():
        for post in sorted(fresh, key=lambda p: (p.published_at, p.post_id)):
            if post.post_id in seen:
                continue
            seen.add(post.post_id)
            if first:
                _mark(step, code, post, assign_channel_topic(post, step.topics.channel_rules))
            else:
                _handle(step, code, post, tally, max_age=None)
        db.save_channel_cursor(step.conn, code, top or 0, step.now, unparsed_streak=streak)


def _mark(step: _Step, code: str, post: ChannelPost, topic: str) -> None:
    """Guarda la publicación como vista sin enviarla: la primera lectura de un feed sin orden o
    lo que no compensa por casillero. No cuenta como original para los duplicados."""
    db.insert_channel_post(
        step.conn,
        code,
        post.post_id,
        published_at=post.published_at,
        title_norm=normalize_title(post.title),
        price_minor=post.price.minor,
        currency=post.price.currency.value,
        coupon=post.coupon,
        topic=topic,
        duplicate_of=None,
        now=step.now,
        baseline=True,
    )


def _worth_by_casillero(post: ChannelPost, topic: str, min_price: Mapping[Currency, int]) -> bool:
    """Por casillero no compensan lo barato ni el mercado; lo que envía directo a Colombia
    no tiene este filtro (calidad de las ofertas, 2026-10-22)."""
    return topic != GROCERY_TOPIC and post.price.minor >= min_price[post.price.currency]


def _fetch(
    r: SourceRun,
    source: ChannelSource,
    after: int | None,
    now: datetime,
    rnd: random.Random,
    deadline: float,
) -> tuple[list[ChannelPage], bool]:
    """Hasta `MAX_PAGES` páginas y si se leyeron sin error; un error conserva lo ya leído.

    El evento `OK` del breaker lo aplica el llamador: antes tiene que mirar la racha.
    """
    ctx = SourceContext(r.http, NoCredentials(), deadline, now)
    pages: list[ChannelPage] = []
    while len(pages) < MAX_PAGES:
        try:
            page = source.fetch_posts(ctx, after)
        except BudgetExhausted:
            r.stopped = True
            break
        except (SourceError, ParseError) as exc:
            _on_error(r, exc, now, rnd)
            return pages, False
        pages.append(page)
        if not page.has_more or after is None or page.max_id is None:
            break
        after = page.max_id
    return pages, True


def _on_error(
    r: SourceRun, exc: SourceError | ParseError, now: datetime, rnd: random.Random
) -> None:
    r.failed = True
    if isinstance(exc, ParseError):
        r.counters.parse_errors += 1
        ev = BreakerEvent.PARSE_ERROR
    elif isinstance(exc, HardBlock):
        r.counters.blocked += 1
        r.stopped = True
        ev = BreakerEvent.HARD_BLOCK
    else:
        r.counters.unavailable += 1
        ev = BreakerEvent.UNAVAILABLE
    r.breaker = breaker_transition(r.breaker, ev, now, rnd)
    event(log, "source_error", source=r.code, code=exc.code)


def _handle(
    step: _Step,
    code: str,
    post: ChannelPost,
    tally: _Tally,
    *,
    max_age: timedelta | None = MAX_AGE,
) -> None:
    conn, now = step.conn, step.now
    tally.posts += 1
    if max_age is not None and post.published_at < now - max_age:
        tally.skipped += 1
        return
    topic = assign_channel_topic(post, step.topics.channel_rules)
    if post.casillero and not _worth_by_casillero(post, topic, step.min_price):
        _mark(step, code, post, topic)
        tally.skipped += 1
        return
    dup = find_duplicate(post, step.recent)
    title_norm = normalize_title(post.title)
    inserted = db.insert_channel_post(
        conn,
        code,
        post.post_id,
        published_at=post.published_at,
        title_norm=title_norm,
        price_minor=post.price.minor,
        currency=post.price.currency.value,
        coupon=post.coupon,
        topic=topic,
        duplicate_of=dup,
        now=now,
    )
    if dup is not None:
        tally.duplicates += 1
        return
    if not inserted:
        return
    msg = render_channel_deal(post, topic)
    db.enqueue(
        conn,
        "alert",
        msg.html,
        priority=PRIORITY,
        dedup_key=f"canal:{code}:{post.post_id}",
        now=now,
        ttl=ALERT_TTL,
        reply_markup=reply_markup(msg.buttons) if msg.buttons else None,
        silent=step.silent,
        topic=topic,
        card=channel_card(post, topic),
    )
    step.recent.append(
        db.RecentPost(
            f"{code}:{post.post_id}",
            title_norm,
            post.price.minor,
            post.price.currency.value,
            post.coupon,
        )
    )
    tally.sent += 1
