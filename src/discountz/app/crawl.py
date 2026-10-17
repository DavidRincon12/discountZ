"""Rastreo del catálogo de una tienda (spec del rastreo §2.2, §3.2 y §4).

Una corrida: lease propio, capa caliente (relectura de lo que cambió hace poco), vuelta completa
desde el cursor, detección por producto, confirmación por la ficha y alerta encolada con su
tarjeta. Para al acabar el plazo, el presupuesto o la vuelta, o ante un bloqueo.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Literal

import psycopg

from discountz.app.render import esc, render_catalog_alert, reply_markup, source_name, truncate
from discountz.config import AppParams, Topics
from discountz.domain.catalog import CatalogCandidate, CatalogObservation, observe
from discountz.domain.errors import BudgetExhausted, HardBlock, ParseError, SourceError
from discountz.domain.guards import check_offer
from discountz.domain.health import BreakerEvent, BreakerState, breaker_transition, effective_state
from discountz.domain.model import AlertKind, ListingRef, Maturity, Offer, SellerClass
from discountz.domain.money import format_money
from discountz.domain.scheduling import CrawlBudget
from discountz.domain.segments import Observation, SegmentStatus, gap_h
from discountz.domain.topics import TopicParams, assign_topic
from discountz.infra import db
from discountz.infra.http import HttpClient, PacedClient
from discountz.infra.log import event
from discountz.sources.base import (
    CardFacts,
    CrawlSource,
    CredentialStore,
    ListingFailed,
    ListingFound,
    SourceContext,
)

log = logging.getLogger(__name__)

LEASE_TTL = timedelta(minutes=60)
HOT_WINDOW = timedelta(days=7)
EAN_WINDOW = timedelta(hours=6)
ALERT_TTL = timedelta(hours=24)
PARSE_STOP = 3  # errores de parseo seguidos que detienen la corrida
CONFIRM_TOL_PCT = 1  # la relectura debe quedar a ±1 % del precio visto
SAMPLE_GAP = gap_h(timedelta(hours=6))
ERRORS_TOPIC = "errores"

# Con la BD en ≥ 450 MB el mantenimiento pausa el descubrimiento: también el rastreo de las
# tiendas más grandes (spec del rastreo §7.2).
PAUSED_AT_450 = frozenset({"exito", "carulla"})
CrawlStatus = Literal["ok", "partial", "blocked", "skipped_locked", "disabled"]


@dataclass(frozen=True)
class CrawlDeps:
    conn: psycopg.Connection
    source: CrawlSource
    http: HttpClient  # transporte crudo; la corrida lo envuelve en PacedClient
    credentials: CredentialStore
    topics: Topics
    params: AppParams
    budget: CrawlBudget
    holder: str
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    rnd: random.Random


@dataclass(frozen=True, slots=True)
class CrawlResult:
    status: CrawlStatus
    pages: int
    items: int
    changes: int
    alerts: int
    requests: int


@dataclass(frozen=True, slots=True)
class _Found:
    obs: CatalogObservation
    candidate: CatalogCandidate
    topic: str


class _Stop(Exception):
    """Fin anticipado de la corrida con su estado."""

    def __init__(self, status: CrawlStatus) -> None:
        super().__init__(status)
        self.status = status


class _Run:
    def __init__(self, deps: CrawlDeps, now: datetime, end: float, breaker: BreakerState) -> None:
        self.deps = deps
        self.conn = deps.conn
        self.now = now
        self.end = end
        self.code = deps.source.descriptor.code
        self.breaker = breaker
        self.cursor = db.get_cursor(self.conn, self.code)
        if self.cursor.day != now.date():
            self.cursor = replace(self.cursor, day=now.date(), day_requests=0)
        self.day_base = self.cursor.day_requests
        cap = min(deps.budget.max_per_run, deps.budget.max_per_24h - self.day_base)
        self.cap = max(cap, 0)
        self.http = PacedClient(
            deps.http,
            deps.budget.paced(self.code, max(self.cap, 1)),
            deps.sleep,
            deps.monotonic,
            deps.rnd,
            deps.source.detect_block,
            source=self.code,
            deadline=end,
        )
        self.ctx = SourceContext(self.http, deps.credentials, end, now)
        self.pages = self.items = self.changes = self.alerts = 0
        self.parse_streak = 0
        self.ok_pages = 0

    # ───────────── Bucle

    def time_left(self) -> bool:
        return self.deps.monotonic() < self.end and self.http.used < self.cap

    def run(self) -> CrawlStatus:
        if self.cap == 0:
            return "ok"
        self.hot_tier()
        return self.sweep()

    def hot_tier(self) -> None:
        cap = self.cap * self.deps.budget.hot_share_bp // 10_000
        d = self.deps.source.descriptor
        if d.supports_changed:
            since = self.cursor.changed_since
            if since is None:
                self.cursor = replace(self.cursor, changed_since=self.now)
                return
            page, more = 0, True
            while more and self.http.used < cap and self.time_left():
                result = self.guarded(
                    partial(self.deps.source.fetch_changed, self.ctx, since, page)
                )
                if result is None:
                    page += 1
                    continue
                self.process(result.items)
                more = result.has_more
                page += 1
            if not more:
                self.cursor = replace(self.cursor, changed_since=self.now)
            return
        ids = db.hot_catalog_ids(self.conn, self.code, self.now - HOT_WINDOW, cap * d.max_batch)
        for i in range(0, len(ids), d.max_batch):
            if self.http.used >= cap or not self.time_left():
                return
            batch = ids[i : i + d.max_batch]
            items = self.guarded(partial(self.deps.source.fetch_catalog_items, self.ctx, batch))
            if items is not None:
                self.process(items)

    def sweep(self) -> CrawlStatus:
        while self.time_left():
            self.check_breaker()
            c = self.cursor
            if not c.partitions:
                parts = self.guarded(lambda: self.deps.source.catalog_partitions(self.ctx))
                if not parts:
                    return "partial"
                self.cursor = replace(
                    c,
                    sweep_no=c.sweep_no + 1,
                    partitions=tuple(parts),
                    partition_idx=0,
                    page=0,
                    sweep_started_at=self.now,
                )
                self.save_cursor()
                continue
            part = c.partitions[c.partition_idx]
            result = self.guarded(
                partial(self.deps.source.fetch_catalog_page, self.ctx, part, c.page)
            )
            if result is None:  # página ilegible: se salta
                self.cursor = replace(c, page=c.page + 1)
            elif result.split:
                parts = (*c.partitions[: c.partition_idx], *result.split)
                parts += c.partitions[c.partition_idx + 1 :]
                self.cursor = replace(c, partitions=parts, page=0)
            else:
                self.process(result.items)
                if result.has_more:
                    self.cursor = replace(c, page=c.page + 1)
                elif c.partition_idx + 1 < len(c.partitions):
                    self.cursor = replace(c, partition_idx=c.partition_idx + 1, page=0)
                else:
                    self.finish_sweep()
                    return "ok"
            self.save_cursor()
        return "ok"

    def finish_sweep(self) -> None:
        started = self.cursor.sweep_started_at or self.now
        self.cursor = replace(
            self.cursor,
            partitions=(),
            partition_idx=0,
            page=0,
            last_sweep_s=int((self.now - started).total_seconds()),
        )
        self.save_cursor()

    def guarded[R](self, call: Callable[[], R]) -> R | None:
        """Ejecuta una petición de la fuente; `None` si la página no se pudo leer."""
        try:
            result = call()
        except ParseError:
            self.parse_streak += 1
            self.breaker_event(BreakerEvent.PARSE_ERROR)
            if self.parse_streak >= PARSE_STOP:
                self.cursor = replace(self.cursor, page=self.cursor.page + 1)
                raise _Stop("partial") from None
            return None
        except BudgetExhausted:
            raise _Stop("ok") from None
        except HardBlock:
            self.breaker_event(BreakerEvent.HARD_BLOCK)
            raise _Stop("blocked") from None
        except SourceError:
            self.breaker_event(BreakerEvent.UNAVAILABLE)
            raise _Stop("partial") from None
        self.parse_streak = 0
        self.ok_pages += 1
        return result

    def breaker_event(self, ev: BreakerEvent) -> None:
        """Transición atómica sobre la fila de la BD: el ciclo de la v1 escribe la misma fila."""
        with self.conn.transaction():
            current = db.load_breaker_for_update(self.conn, self.code) or self.breaker
            self.breaker = breaker_transition(current, ev, self.now, self.deps.rnd)
            db.save_breaker(self.conn, self.code, self.breaker)

    def check_breaker(self) -> None:
        """Si otro proceso abrió el breaker durante la corrida, el rastreo para."""
        current = db.load_breaker_for_update(self.conn, self.code)
        if current is not None and effective_state(current, self.now) == "open":
            self.breaker = current
            raise _Stop("blocked")

    def save_cursor(self) -> None:
        db.save_cursor(self.conn, self.with_requests())

    def with_requests(self) -> db.CrawlCursor:
        return replace(
            self.cursor,
            last_run_at=self.now,
            day_requests=self.day_base + self.http.used,
        )

    # ───────────── Detección

    def process(self, items: Sequence[CatalogObservation]) -> None:
        if not items:
            return
        self.pages += 1
        params = self.deps.topics.params
        states = db.load_catalog_states(self.conn, self.code, [o.external_id for o in items])
        rows: dict[str, db.CatalogRow] = {}
        found: list[_Found] = []
        for o in items:
            topic = assign_topic(self.deps.topics.rules, self.code, o.category_path)
            state, cand, changed = observe(
                states.get(o.external_id), o, self.now, params[topic], self.deps.params.glob
            )
            currency = o.price.currency.value
            rows[o.external_id] = db.CatalogRow(
                o.external_id, o.ean, topic, currency, state, changed
            )
            self.items += 1
            self.changes += int(changed)
            if cand is not None:
                found.append(_Found(o, cand, topic))
        fresh = [f for f in found if not self.duplicate(f)]
        # Un candidato no mueve su estado hasta que la confirmación responda: si falla por red,
        # bloqueo o corte, la próxima lectura lo vuelve a detectar (revisión final, hallazgo 2).
        real = {f.obs.external_id: rows[f.obs.external_id] for f in fresh}
        for ext, row in real.items():
            prev = states.get(ext)
            if prev is not None:
                rows[ext] = replace(row, state=prev, price_changed=False)
        db.save_catalog_page(self.conn, self.code, list(rows.values()), self.now)
        if fresh:
            self.confirm(fresh, real)

    def duplicate(self, f: _Found) -> bool:
        if f.obs.ean is None:
            return False
        dup = db.recent_alert_for_ean(
            self.conn,
            f.obs.ean,
            f.obs.price.currency.value,
            f.obs.price.minor,
            self.now - EAN_WINDOW,
            exclude_source=self.code,
        )
        if dup:
            event(log, "catalog_skip", source=self.code, code="ean_duplicate")
        return dup

    # ───────────── Confirmación y alerta (spec §4)

    def confirm(self, found: Sequence[_Found], real: Mapping[str, db.CatalogRow]) -> None:
        src = self.deps.source
        refs = [ListingRef(self.code, f.obs.external_id) for f in found]
        outcomes = self.guarded(lambda: src.fetch_catalog_listings(self.ctx, refs))
        if outcomes is None:
            return
        by_ext = {
            (o.offer.ref if isinstance(o, ListingFound) else o.ref).external_id: o for o in outcomes
        }
        settled = [
            f
            for f in found
            if f.obs.external_id in by_ext
            and not isinstance(by_ext[f.obs.external_id], ListingFailed)
        ]
        offers = {o.offer.ref.external_id: o.offer for o in outcomes if isinstance(o, ListingFound)}
        ok = [f for f in settled if self.accepts(f, offers.get(f.obs.external_id))]
        rejected = [real[f.obs.external_id] for f in settled if f not in ok]
        db.save_catalog_page(self.conn, self.code, rejected, self.now)
        if not ok:
            return
        ids = [f.obs.external_id for f in ok]
        facts = self.guarded(lambda: src.fetch_card_facts(self.ctx, ids)) or {}
        for f in ok:
            self.alert(f, offers[f.obs.external_id], facts.get(f.obs.external_id))
            db.save_catalog_page(self.conn, self.code, [real[f.obs.external_id]], self.now)

    def accepts(self, f: _Found, raw: Offer | None) -> bool:
        reason = None
        offer = None
        if raw is None:
            reason = "gone"
        else:
            try:
                offer = check_offer(raw, self.deps.source.descriptor.currency, None)
            except ParseError:
                reason = "guard"
        if offer is not None:
            price = offer.price.minor if offer.price is not None else None
            p = f.candidate.price.minor
            cat = self.category(f.topic)
            allowed = self.deps.params.categories[cat].allow_marketplace
            if not offer.in_stock or price is None:
                reason = "out_of_stock"
            elif abs(price - p) * 100 > p * CONFIRM_TOL_PCT:
                reason = "price_mismatch"
            elif offer.seller_class is SellerClass.MARKETPLACE and not allowed:
                reason = "seller"
        if reason is not None:
            event(log, "catalog_reject", source=self.code, code=reason)
        return reason is None

    def category(self, topic: str) -> str:
        return self.deps.topics.params[topic].category

    def alert(self, f: _Found, offer: Offer, facts: CardFacts | None) -> None:
        d = self.deps.source.descriptor
        cand = f.candidate
        kind = cand.kind
        url = facts.url if facts else f"https://{sorted(d.hosts)[0]}{offer.url_path or '/'}"
        title = facts.title if facts and facts.title else offer.title
        topic: TopicParams = self.deps.topics.params[f.topic]
        price = cand.price.minor
        with self.conn.transaction():
            prev = db.upsert_listing(self.conn, offer, topic.category, url, self.now)
            obs = Observation(
                self.now,
                price,
                offer.list_price.minor if offer.list_price else None,
                True,
                offer.seller_class,
                offer.unit_key,
            )
            status = SegmentStatus.GLITCH if kind is AlertKind.PRICE_ERROR else SegmentStatus.OPEN
            seg = db.write_sample(self.conn, prev.id, obs, status, SAMPLE_GAP)
            muted = _later(prev.muted_until, db.global_muted_until(self.conn))
            delivery: Literal["new", "muted"] = (
                "muted" if muted is not None and muted > self.now else "new"
            )
            alert_id = db.insert_alert(
                self.conn,
                db.NewAlert(
                    listing_id=prev.id,
                    segment_started_at=seg.started_at,
                    watch_id=None,
                    kind=kind,
                    maturity=Maturity.WARM if kind is AlertKind.CATALOG_DROP else Maturity.COLD,
                    price_minor=price,
                    reference_minor=cand.usual.minor,
                    real_saving_bp=cand.drop_bp,
                    advertised_saving_bp=None,
                    flags=(),
                    delivery=delivery,
                    observed_at=self.now,
                ),
            )
            db.mark_catalog_alert(self.conn, self.code, f.obs.external_id, price, self.now)
            if alert_id is None or delivery != "new":
                return
            card = _card(self.code, f.topic, title, url, facts, cand)
            msg = render_catalog_alert(card, prev.id, topic.title)
            is_error = kind is AlertKind.PRICE_ERROR
            outbox_id = db.enqueue(
                self.conn,
                "alert",
                msg.html,
                priority=0 if is_error else max(2, 9 - cand.drop_bp // 1000),
                dedup_key=f"cat:{alert_id}",
                now=self.now,
                ttl=ALERT_TTL,
                reply_markup=reply_markup(msg.buttons),
                topic=ERRORS_TOPIC if is_error else f.topic,
                card=card,
            )
            db.mark_alerts_queued(self.conn, [alert_id], outbox_id)
            if is_error:
                db.enqueue(
                    self.conn,
                    "alert",
                    _error_line(self.code, title, cand, prev.id),
                    priority=0,
                    dedup_key=f"pe-line:{alert_id}",
                    now=self.now,
                    ttl=ALERT_TTL,
                    topic=f.topic,
                )
        self.alerts += 1


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


def _card(
    store: str,
    topic: str,
    title: str,
    url: str,
    facts: CardFacts | None,
    cand: CatalogCandidate,
) -> dict[str, Any]:
    return {
        "store": store,
        "topic": topic,
        "title": title,
        "url": url,
        "image_url": facts.image_url if facts else None,
        "price_minor": cand.price.minor,
        "usual_minor": cand.usual.minor,
        "currency": cand.price.currency.value,
        "drop_bp": cand.drop_bp,
        "min_days": cand.min_days,
        "kind": cand.kind.value,
    }


def _error_line(store: str, title: str, cand: CatalogCandidate, listing_id: int) -> str:
    return (
        f"🔥 Posible error de precio en {esc(source_name(store))}: "
        f"<b>{esc(truncate(title, 120))}</b> a <b>{format_money(cand.price)}</b> "
        f"(antes {format_money(cand.usual)}). Detalle en «Errores de precio» · L{listing_id}"
    )


def run_crawl(deps: CrawlDeps, now: datetime, minutes: int) -> CrawlResult:
    code = deps.source.descriptor.code
    breaker = db.load_breakers(deps.conn).get(code)
    if breaker is None or (code in PAUSED_AT_450 and db.bot_state(deps.conn).discovery_paused):
        return CrawlResult("disabled", 0, 0, 0, 0, 0)
    if effective_state(breaker, now) == "open":
        return CrawlResult("blocked", 0, 0, 0, 0, 0)
    if not db.acquire_crawl_lease(deps.conn, code, deps.holder, now, LEASE_TTL):
        return CrawlResult("skipped_locked", 0, 0, 0, 0, 0)
    run = _Run(deps, now, deps.monotonic() + minutes * 60, breaker)
    try:
        try:
            status = run.run()
        except _Stop as stop:
            status = stop.status
        if status == "ok" and run.ok_pages:
            run.breaker_event(BreakerEvent.OK)
        db.save_cursor(deps.conn, run.with_requests())
    finally:
        db.release_crawl_lease(deps.conn, code, deps.holder)
    result = CrawlResult(status, run.pages, run.items, run.changes, run.alerts, run.http.used)
    event(
        log,
        "crawl_done",
        source=code,
        status=status,
        requests=result.requests,
        alerts=result.alerts,
        count=result.items,
    )
    return result
