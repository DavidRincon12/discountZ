"""Recolección (spec §1.3, pasos 9 y 10): tareas por fuente en turnos rotativos.

Por tarea: **F** lee de la fuente → **R** carga el histórico → **E** `evaluate()` puro →
**W** transacción corta. Nunca hay HTTP dentro de una transacción. Cada tarea está aislada:
una excepción la marca fallida y el run queda `partial`.
"""

from __future__ import annotations

import logging
import random
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

import psycopg

from discountz.app.render import (
    MAX_TITLE,
    REFERENCE_LABEL,
    AlertView,
    esc,
    source_name,
    truncate,
)
from discountz.config import AppParams, Canary
from discountz.domain.decide import RearmEvidence, Skip, decide
from discountz.domain.errors import (
    BudgetExhausted,
    HardBlock,
    NotFound,
    ParseError,
    SourceError,
    UnsafeUrl,
)
from discountz.domain.evaluate import (
    EvalContext,
    Evaluation,
    ListingState,
    Origin,
    WatchTerms,
    evaluate,
)
from discountz.domain.guards import batch_is_constant, check_offer
from discountz.domain.health import (
    QUARANTINE_CYCLES,
    BreakerEvent,
    BreakerState,
    HourStat,
    breaker_transition,
)
from discountz.domain.model import AlertKind, Flag, Lead, ListingRef, Offer, SellerClass
from discountz.domain.money import Money, format_money
from discountz.domain.params import CategoryParams
from discountz.domain.scheduling import SourceBudget, Task, effective_interval, next_check_at
from discountz.domain.segments import Observation, Segment, SegmentStatus, gap_h
from discountz.domain.stats import real_saving_bp
from discountz.infra import db
from discountz.infra.http import PacedClient
from discountz.infra.log import event
from discountz.sources.base import (
    Capability,
    CredentialStore,
    ListingFailed,
    ListingFound,
    ListingGone,
    ListingOutcome,
    NeedsResolution,
    NoCredentials,
    Source,
    SourceContext,
)
from discountz.sources.registry import route_url

log = logging.getLogger(__name__)

HOT_FOR = timedelta(hours=2)
GONE_AFTER = 3
REPLY_TTL = timedelta(hours=24)
SYSTEM_TTL = timedelta(hours=48)
LEAD_FRESH = timedelta(hours=6)  # un lead no relee un listing leído hace menos (spec §4.3)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Lectura que espera confirmación independiente (fase C)."""

    offer: Offer
    listing_id: int
    url: str
    segment: Segment
    evaluation: Evaluation
    prev: ListingState
    cat: CategoryParams
    watch: db.WatchRow | None


@dataclass(frozen=True, slots=True)
class LeadRead:
    """Lectura de ficha que pide un lead de Slickdeals, con prioridad de confirmación."""

    ref: ListingRef
    price: Money
    category: str


@dataclass
class Counters:
    blocked: int = 0
    unavailable: int = 0
    parse_errors: int = 0
    not_found: int = 0
    confirm_mismatch: int = 0
    soft_empty: int = 0
    offers: int = 0

    def hour_stat(self, requests: int) -> tuple[HourStat, dict[str, int]]:
        """Contadores acotados a `requests` (CHECK `shs_counts_ck`)."""
        left = requests
        capped = {}
        for name in ("blocked", "unavailable", "parse_errors", "not_found"):
            value = min(getattr(self, name), left)
            capped[name] = value
            left -= value
        stat = HourStat(
            requests=requests,
            ok=left,
            blocked=capped["blocked"],
            parse_errors=capped["parse_errors"],
            soft_empty=self.soft_empty,
            confirm_mismatch=self.confirm_mismatch,
        )
        return stat, {"unavailable": capped["unavailable"], "not_found": capped["not_found"]}


@dataclass
class SourceRun:
    source: Source
    http: PacedClient
    budget: SourceBudget
    breaker: BreakerState
    tasks: deque[Task]
    counters: Counters = field(default_factory=Counters)
    candidates: list[Candidate] = field(default_factory=list)
    leads: deque[LeadRead] = field(default_factory=deque)
    failed: bool = False
    stopped: bool = False
    canary_ok: bool | None = None

    @property
    def code(self) -> str:
        return self.source.descriptor.code


class Collector:
    def __init__(
        self,
        conn: psycopg.Connection,
        now: datetime,
        params: AppParams,
        canaries: Mapping[str, Sequence[Canary]],
        rnd: random.Random,
        monotonic: Callable[[], float],
        deadline: float,
        cycle: timedelta,
        *,
        stores: Mapping[str, Source] | None = None,
        spawn: Callable[[str], SourceRun | None] | None = None,
        credentials: CredentialStore | None = None,
    ) -> None:
        self.conn = conn
        self.now = now
        self.params = params
        self.canaries = canaries
        self.rnd = rnd
        self.monotonic = monotonic
        self.deadline = deadline
        self.cycle = cycle
        self.views: dict[int, AlertView] = {}
        self.alerts_created = 0
        self.evaluated: set[int] = set()
        # Tiendas activas a las que un lead puede llevar y fábrica de su SourceRun si esa
        # tienda no tenía tareas en este ciclo.
        self.stores: Mapping[str, Source] = stores or {}
        self.spawn = spawn
        self.credentials: CredentialStore = credentials or NoCredentials()
        self._runs: list[SourceRun] = []
        self._lead_refs: set[ListingRef] = set()

    # ───────────── Bucle de turnos rotativos

    def run(self, runs: list[SourceRun]) -> None:
        """Turnos rotativos; las lecturas de leads van antes que las tareas de su fuente.

        `runs` puede crecer: un lead abre el SourceRun de una tienda sin tareas.
        """
        self._runs = runs
        while self.monotonic() < self.deadline:
            active = [r for r in runs if not r.stopped and (r.leads or r.tasks)]
            if not active:
                break
            for r in active:
                if r.leads:
                    self._execute(r, "lead", r.leads.popleft())
                elif r.tasks:
                    task = r.tasks.popleft()
                    self._execute(r, task.kind, task)

    def _execute(self, r: SourceRun, kind: str, work: Task | LeadRead) -> None:
        try:
            if isinstance(work, LeadRead):
                self._lead(r, work)
                return
            task = work
            if task.kind == "canary":
                self._canary(r)
            elif task.kind == "resolve":
                for watch_id in task.payload_ids:
                    self._resolve(r, watch_id)
            elif task.kind in ("confirm", "hot", "watch_target", "watch"):
                self._listings(r, task.payload_ids)
            elif task.kind == "search":
                for search_id in task.payload_ids:
                    self._search(r, search_id)
            elif task.kind == "feed":
                self._feed(r, task.payload_ids)
            else:
                event(log, "task_skipped", source=r.code, kind=task.kind)
        except BudgetExhausted:
            r.stopped = True
        except Exception as exc:  # aislamiento por tarea (spec §7.2)
            r.failed = True
            event(
                log,
                "task_failed",
                logging.ERROR,
                source=r.code,
                kind=kind,
                error=type(exc).__name__,
            )

    def ctx(self, r: SourceRun) -> SourceContext:
        return SourceContext(r.http, self.credentials, self.deadline, self.now)

    # ───────────── Errores y breaker

    def breaker_event(self, r: SourceRun, ev: BreakerEvent) -> None:
        r.breaker = breaker_transition(r.breaker, ev, self.now, self.rnd)

    def on_error(self, r: SourceRun, exc: SourceError | ParseError) -> None:
        if isinstance(exc, ParseError):
            r.counters.parse_errors += 1
            r.failed = True
            self.breaker_event(r, BreakerEvent.PARSE_ERROR)
        elif isinstance(exc, HardBlock):
            r.counters.blocked += 1
            r.failed = r.stopped = True
            self.breaker_event(r, BreakerEvent.HARD_BLOCK)
        elif isinstance(exc, NotFound):
            r.counters.not_found += 1
        elif isinstance(exc, BudgetExhausted):
            r.stopped = True
        else:
            r.counters.unavailable += 1
            r.failed = True
            self.breaker_event(r, BreakerEvent.UNAVAILABLE)
        event(log, "source_error", source=r.code, code=exc.code)

    # ───────────── Tareas

    def _canary(self, r: SourceRun) -> None:
        items = self.canaries.get(r.code, ())
        half_open = r.breaker.state == "open"
        if not items:  # sin canario configurado: se asume sana y las tareas reales lo confirman
            self.breaker_event(r, BreakerEvent.PROBE_OK if half_open else BreakerEvent.OK)
            return
        refs = [ListingRef(r.code, c.external_id) for c in items]
        outcomes = r.source.fetch_listings(self.ctx(r), refs)
        ok = True
        for c, out in zip(items, outcomes, strict=True):
            if isinstance(out, ListingFailed):
                self.on_error(r, out.error)
                ok = False
            elif (
                not isinstance(out, ListingFound)
                or out.offer.price is None
                or not c.in_range(out.offer.price.minor)
            ):
                ok = False
        r.canary_ok = ok
        if not ok:  # fuera de rango: posible cambio de formato ⇒ cuarentena (spec §4.7)
            quarantine = max(r.breaker.quarantine_cycles, QUARANTINE_CYCLES)
            r.breaker = replace(r.breaker, quarantine_cycles=quarantine)
        db.record_canary(self.conn, r.code, ok, self.now)
        if half_open:
            self.breaker_event(r, BreakerEvent.PROBE_OK if ok else BreakerEvent.PROBE_FAIL)
        event(log, "canary", source=r.code, status="ok" if ok else "out_of_range")

    def _listings(self, r: SourceRun, listing_ids: Sequence[int]) -> None:
        refs_by_id = db.listing_refs(self.conn, listing_ids)
        ids = [i for i in listing_ids if i in refs_by_id and i not in self.evaluated]
        if not ids:
            return
        refs = [refs_by_id[i] for i in ids]
        outcomes = r.source.fetch_listings(self.ctx(r), refs)
        for listing_id, out in zip(ids, outcomes, strict=True):
            self._outcome(r, listing_id, out)

    def _outcome(self, r: SourceRun, listing_id: int, out: ListingOutcome) -> None:
        watch = db.watch_for_listing(self.conn, listing_id)
        if isinstance(out, ListingFound):
            r.counters.offers += 1
            self.breaker_event(r, BreakerEvent.OK)
            self.process_offer(r, out.offer, "detail")
            return
        if isinstance(out, ListingGone):
            r.counters.not_found += 1
            if watch is not None:
                self._reschedule(r, watch, failed=False, not_found=True)
            return
        self.on_error(r, out.error)
        if watch is not None:
            self._reschedule(r, watch, failed=True, not_found=False)

    def _reschedule(
        self, r: SourceRun, watch: db.WatchRow, *, failed: bool, not_found: bool
    ) -> None:
        failures = watch.consecutive_failures + 1 if failed else 0
        misses = watch.consecutive_not_found + 1 if not_found else 0
        interval = self._watch_interval(r, watch)
        nxt = next_check_at(self.now, interval, failures, not_found, self.rnd)
        gone = misses >= GONE_AFTER
        with self.conn.transaction():
            db.record_watch_check(
                self.conn, watch.id, self.now, nxt, failures=failures, not_found=misses, gone=gone
            )
            if gone and watch.listing_id is not None:
                db.mark_listing_gone(self.conn, watch.listing_id, self.now)
                db.enqueue(
                    self.conn,
                    "system",
                    f"p{watch.id} parece retirado de {source_name(r.code)}: dejo de revisarlo.",
                    priority=2,
                    dedup_key=f"gone:{watch.id}",
                    now=self.now,
                    ttl=SYSTEM_TTL,
                )

    def _watch_interval(self, r: SourceRun, watch: db.WatchRow | None) -> timedelta:
        events = any(e.active(r.code, self.now) for e in self.params.events)
        has_target = watch is not None and watch.target_minor is not None
        return effective_interval(r.budget.watch_interval, has_target, False, events, self.cycle)

    def _resolve(self, r: SourceRun, watch_id: int) -> None:
        watch = db.get_watch(self.conn, watch_id)
        if watch is None or watch.pending_ref is None:
            return
        ref: ListingRef
        try:
            if watch.pending_ref.startswith("id:"):
                ref = ListingRef(r.code, watch.pending_ref.removeprefix("id:"))
            else:
                ref = r.source.resolve(self.ctx(r), NeedsResolution(r.code, watch.pending_ref))
            outcomes = r.source.fetch_listings(self.ctx(r), [ref])
        except (SourceError, ParseError) as exc:
            if isinstance(exc, BudgetExhausted):
                raise
            self.on_error(r, exc)
            self._resolve_failed(r, watch, not_found=isinstance(exc, NotFound))
            return
        out = outcomes[0]
        if not isinstance(out, ListingFound):
            if isinstance(out, ListingFailed):
                self.on_error(r, out.error)
            self._resolve_failed(r, watch, not_found=isinstance(out, ListingGone))
            return
        self.breaker_event(r, BreakerEvent.OK)
        r.counters.offers += 1

        def attach(listing_id: int, title: str, offer: Offer) -> None:
            existing = db.watch_for_listing(self.conn, listing_id)
            if existing is not None and existing.id != watch.id:
                db.delete_watch(self.conn, watch.id)
                text = f"Ya lo sigues (p{existing.id})."
            else:
                db.activate_watch(self.conn, watch.id, listing_id)
                text = _follow_reply(watch.id, title, offer)
            db.enqueue(
                self.conn,
                "reply",
                text,
                priority=1,
                dedup_key=f"follow:{watch.id}",
                now=self.now,
                ttl=REPLY_TTL,
                reply_to_message_id=watch.reply_to_message_id,
            )

        self.process_offer(
            r, out.offer, "detail", category=watch.requested_category, on_listing=attach
        )

    def _resolve_failed(self, r: SourceRun, watch: db.WatchRow, *, not_found: bool) -> None:
        if not_found and watch.consecutive_not_found + 1 >= GONE_AFTER:
            with self.conn.transaction():
                db.delete_watch(self.conn, watch.id)
                db.enqueue(
                    self.conn,
                    "reply",
                    f"No encontré el producto de p{watch.id} en {source_name(r.code)}; "
                    "lo dejé de seguir.",
                    priority=1,
                    dedup_key=f"follow:{watch.id}",
                    now=self.now,
                    ttl=REPLY_TTL,
                    reply_to_message_id=watch.reply_to_message_id,
                )
            return
        self._reschedule(r, watch, failed=not not_found, not_found=not_found)

    def _search(self, r: SourceRun, search_id: int) -> None:
        row = db.get_search(self.conn, search_id)
        if row is None:
            return
        events = any(e.active(r.code, self.now) for e in self.params.events)
        interval = effective_interval(r.budget.search_interval, False, False, events, self.cycle)
        try:
            page = r.source.search(self.ctx(r), row.spec, 1)
        except (SourceError, ParseError) as exc:
            if isinstance(exc, BudgetExhausted):
                raise
            self.on_error(r, exc)
            nxt = next_check_at(self.now, interval, row.consecutive_failures + 1, False, self.rnd)
            db.record_search_run(self.conn, search_id, self.now, nxt, result_count=None)
            return
        self.breaker_event(r, BreakerEvent.OK)
        offers = [o for o in page.offers if row.spec.accepts(o)]
        if not page.offers:
            r.counters.soft_empty += 1
        r.counters.offers += len(offers)
        constant = batch_is_constant(offers)  # G10 sin histórico: se persiste y no se alerta
        for o in offers:
            peers = [
                p.price.minor for p in offers if p is not o and p.in_stock and p.price is not None
            ]
            self.process_offer(
                r, o, "search", category=row.spec.category, peers=peers, alerts=not constant
            )
        nxt = next_check_at(self.now, interval, 0, False, self.rnd)
        db.record_search_run(self.conn, search_id, self.now, nxt, result_count=len(page.offers))

    # ───────────── Feed comunitario (spec §4.3 «Slickdeals»)

    def _feed(self, r: SourceRun, search_ids: Sequence[int]) -> None:
        """Un RSS para todas las búsquedas vencidas; los leads no se guardan."""
        events = any(e.active(r.code, self.now) for e in self.params.events)
        interval = effective_interval(r.budget.search_interval, False, False, events, self.cycle)
        rows = [row for i in search_ids if (row := db.get_search(self.conn, i)) is not None]
        try:
            leads = r.source.fetch_leads(self.ctx(r))
        except (SourceError, ParseError) as exc:
            if isinstance(exc, BudgetExhausted):
                raise
            self.on_error(r, exc)
            for row in rows:
                failures = row.consecutive_failures + 1
                nxt = next_check_at(self.now, interval, failures, False, self.rnd)
                db.record_search_run(self.conn, row.id, self.now, nxt, result_count=None)
            return
        self.breaker_event(r, BreakerEvent.OK)
        if not leads:
            r.counters.soft_empty += 1
        for row in rows:
            for lead in leads:
                if row.spec.matches(lead.title, lead.price):
                    self._queue_lead(lead, row.spec.category)
            nxt = next_check_at(self.now, interval, 0, False, self.rnd)
            db.record_search_run(self.conn, row.id, self.now, nxt, result_count=len(leads))

    def _queue_lead(self, lead: Lead, category: str) -> None:
        """Encola la ficha en la tienda de destino si está activa y no se leyó hace poco."""
        if lead.price is None:  # sin precio del lead no hay COMMUNITY_DEAL posible
            return
        try:
            source, ref = route_url(str(lead.target_url), self.stores)
        except UnsafeUrl:  # tienda inactiva o ruta que no es una ficha
            return
        if not isinstance(ref, ListingRef) or ref in self._lead_refs:
            return  # un enlace corto exigiría red: se ignora
        seen = db.listing_last_seen(self.conn, ref.source, ref.external_id)
        if seen is not None and self.now - seen < LEAD_FRESH:
            return
        target = self._run_for(source.descriptor.code)
        if target is None or target.stopped:
            return
        self._lead_refs.add(ref)
        target.leads.append(LeadRead(ref, lead.price, category))
        event(log, "lead_queued", source=ref.source)

    def _run_for(self, code: str) -> SourceRun | None:
        for r in self._runs:
            if r.code == code:
                return r
        spawned = self.spawn(code) if self.spawn is not None else None
        if spawned is not None:
            self._runs.append(spawned)
        return spawned

    def _lead(self, r: SourceRun, item: LeadRead) -> None:
        (out,) = r.source.fetch_listings(self.ctx(r), [item.ref])
        if isinstance(out, ListingFound):
            r.counters.offers += 1
            self.breaker_event(r, BreakerEvent.OK)
            same = out.offer.price is None or out.offer.price.currency is item.price.currency
            self.process_offer(
                r,
                out.offer,
                "lead",
                category=item.category,
                lead_price=item.price.minor if same else None,
            )
        elif isinstance(out, ListingGone):
            r.counters.not_found += 1
        else:
            self.on_error(r, out.error)

    # ───────────── F → R → E → W de una oferta

    def process_offer(
        self,
        r: SourceRun,
        raw: Offer,
        origin: Origin,
        *,
        category: str | None = None,
        peers: Sequence[int] | None = None,
        alerts: bool = True,
        on_listing: Callable[[int, str, Offer], None] | None = None,
        lead_price: int | None = None,
    ) -> None:
        d = r.source.descriptor
        prev_lp = db.prev_list_price(self.conn, raw.ref.source, raw.ref.external_id)
        try:
            offer = check_offer(raw, d.currency, prev_lp)  # G1–G5
        except ParseError as exc:
            self.on_error(r, exc)
            return
        known = db.find_listing(self.conn, offer.ref.source, offer.ref.external_id)
        if known is not None and known in self.evaluated:
            return
        host = sorted(d.hosts)[0]
        url = f"https://{host}{offer.url_path or '/'}"
        prev = db.upsert_listing(self.conn, offer, category or "otros", url, self.now)
        lid = prev.id
        self.evaluated.add(lid)
        if on_listing is not None:
            with self.conn.transaction():
                on_listing(lid, offer.title, offer)
        if r.breaker.quarantine_cycles > 0:  # cuarentena: se lee, no se escribe histórico
            return
        watch = db.watch_for_listing(self.conn, lid)
        cat = self.params.categories.get(prev.category) or self.params.categories["otros"]
        interval = (
            self._watch_interval(r, watch) if origin == "detail" else r.budget.search_interval
        )
        h = gap_h(interval)
        history = db.load_history(self.conn, [lid], self.now - self.params.glob.extended_window)[
            lid
        ]
        open_seg = next((s for s in history if s.ended_at is None), None)
        price = offer.price.minor if offer.price is not None else None
        obs = Observation(
            offer.observed_at,
            price,
            offer.list_price.minor if offer.list_price is not None else None,
            offer.in_stock and price is not None,
            offer.seller_class,
            offer.unit_key,
        )
        carried = (
            open_seg is not None
            and open_seg.status in (SegmentStatus.SUSPECT, SegmentStatus.GLITCH)
            and open_seg.price_minor == obs.price_minor
            and open_seg.in_stock == obs.in_stock
        )
        extends = (
            open_seg is not None
            and open_seg.price_minor == obs.price_minor
            and open_seg.in_stock == obs.in_stock
            and open_seg.seller_class == obs.seller_class
            and open_seg.unit_key == obs.unit_key
            and (carried or open_seg.status is SegmentStatus.OPEN)
            and obs.t - open_seg.last_seen_at <= h
        )
        s_cur = open_seg.started_at if extends and open_seg is not None else obs.t
        past = [
            s if s.ended_at is not None else replace(s, ended_at=s_cur)
            for s in history
            if s.started_at < s_cur
        ]
        muted = _later(prev.muted_until, db.global_muted_until(self.conn))
        listing = replace(prev, muted_until=muted)
        terms = WatchTerms(watch.target_minor, watch.target_set_at) if watch else None
        ev = evaluate(
            EvalContext(
                now=self.now,
                offer=offer,
                origin=origin,
                segments=past,
                current_started_at=s_cur,
                listing=listing,
                cat=cat,
                glob=self.params.glob,
                nominal_interval=interval,
                watch=terms,
                peers=peers,
                lead_price=lead_price,
                last_alert=db.last_alert(self.conn, lid),
                has_independent_route=Capability.CONFIRM in d.capabilities,
            )
        )
        needs = alerts and ev.verdict is not None and ev.confirmation != "none"
        status = SegmentStatus.OPEN
        if carried and open_seg is not None:
            status = open_seg.status
        elif needs:
            status = SegmentStatus.SUSPECT
        with self.conn.transaction():
            seg = db.write_sample(self.conn, lid, obs, status, h)
            dropped = (
                open_seg is not None
                and open_seg.in_stock
                and open_seg.price_minor is not None
                and price is not None
                and obs.in_stock
                and price < open_seg.price_minor
            )
            if dropped:
                db.bump_hot(self.conn, lid, self.now + HOT_FOR)
            if watch is not None:
                nxt = next_check_at(self.now, self._watch_interval(r, watch), 0, False, self.rnd)
                db.record_watch_check(self.conn, watch.id, self.now, nxt, failures=0, not_found=0)
        if not alerts or ev.verdict is None or price is None:
            if db.pending_confirmation_at(self.conn, lid) is not None:
                db.set_pending_confirmation(self.conn, lid, None)
            return
        cand = Candidate(offer, lid, url, seg, ev, listing, cat, watch)
        if seg.status is SegmentStatus.GLITCH or ev.confirmation == "none":
            self.finalize(cand)
        elif ev.confirmation == "independent":
            r.candidates.append(cand)
        elif ev.confirmation == "persistence":
            self._persistence(cand)
        # "forbidden": queda 's' para siempre y nunca alerta (spec §3.6)

    def _persistence(self, c: Candidate) -> None:
        glob = self.params.glob
        seg = c.segment
        if seg.sample_count >= 2 and self.now - seg.started_at >= glob.persistence_gap:
            confirmed = (
                SegmentStatus.GLITCH
                if c.evaluation.verdict is AlertKind.PRICE_ERROR
                else SegmentStatus.OPEN
            )
            with self.conn.transaction():
                db.set_segment_status(self.conn, c.listing_id, seg.started_at, confirmed)
                db.set_pending_confirmation(self.conn, c.listing_id, None)
                self.finalize(c, extra=frozenset({Flag.PERSISTENCE_ONLY}), tx=False)
            return
        with self.conn.transaction():
            db.set_pending_confirmation(self.conn, c.listing_id, self.now)
            db.bump_hot(self.conn, c.listing_id, self.now + HOT_FOR)

    def finalize(
        self, c: Candidate, extra: frozenset[Flag] = frozenset(), *, tx: bool = True
    ) -> None:
        """`decide()` y, si corresponde, la alerta (`new` o `muted`) en una transacción."""
        if tx:
            with self.conn.transaction():
                self._finalize(c, extra)
        else:
            self._finalize(c, extra)

    def _finalize(self, c: Candidate, extra: frozenset[Flag]) -> None:
        ev, offer = c.evaluation, c.offer
        assert offer.price is not None  # noqa: S101 - garantizado por process_offer
        p = offer.price.minor
        last = db.last_alert(self.conn, c.listing_id)
        rearm = _rearm(
            self.conn, c, last.created_at if last else None, last.reference_minor if last else None
        )
        decision = decide(
            ev,
            last,
            rearm,
            c.prev,
            self.now,
            c.cat,
            price_minor=p,
            segment_started_at=c.segment.started_at,
        )
        if isinstance(decision, Skip) and decision.reason != "muted":
            return
        delivery: Literal["new", "muted"] = "muted" if isinstance(decision, Skip) else "new"
        assert ev.verdict is not None  # noqa: S101
        flags = ev.flags | extra
        r_ref = ev.stats.r
        lp = offer.list_price.minor if offer.list_price is not None else None
        adv = (lp - p) * 10_000 // lp if lp is not None and lp > p else None
        real = real_saving_bp(r_ref, p) if r_ref else None
        alert_id = db.insert_alert(
            self.conn,
            db.NewAlert(
                listing_id=c.listing_id,
                segment_started_at=c.segment.started_at,
                watch_id=c.watch.id if c.watch else None,
                kind=ev.verdict,
                maturity=ev.maturity,
                price_minor=p,
                reference_minor=r_ref,
                real_saving_bp=max(real, -100_000) if real is not None else None,
                advertised_saving_bp=adv if adv else None,
                flags=tuple(sorted(f.value for f in flags)),
                delivery=delivery,
                observed_at=offer.observed_at,
            ),
        )
        if alert_id is None or delivery != "new":
            return
        self.alerts_created += 1
        currency = offer.price.currency
        before = c.prev.last_price_minor
        self.views[alert_id] = AlertView(
            listing_id=c.listing_id,
            watch_id=c.watch.id if c.watch else None,
            kind=ev.verdict,
            category=c.prev.category,
            source=offer.ref.source,
            title=offer.title,
            url=c.url,
            price=offer.price,
            before=Money(before, currency) if before is not None and before != p else None,
            reference=Money(r_ref, currency) if r_ref else None,
            reference_label=REFERENCE_LABEL if r_ref else None,
            real_saving_bp=real,
            advertised_saving_bp=adv,
            list_price=offer.list_price,
            history_min=Money(ev.stats.m_robust, currency) if ev.stats.m_robust else None,
            history_median=Money(ev.stats.m90, currency) if ev.stats.m90 else None,
            history_days=ev.stats.days,
            flags=frozenset(flags),
            observed_at=offer.observed_at,
        )


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _rearm(
    conn: psycopg.Connection, c: Candidate, since: datetime | None, reference: int | None
) -> RearmEvidence:
    """Evidencia de rearme (spec §3.8): tiempo en precio «normal» desde la última alerta."""
    if since is None:
        return RearmEvidence(0, 0, False)
    history = db.load_history(conn, [c.listing_id], since)[c.listing_id]
    target = c.watch.target_minor if c.watch else None
    half = c.cat.t_good_bp // 2
    above_ref = above_target = 0
    for s in history:
        if s.status is not SegmentStatus.OPEN or not s.in_stock or s.price_minor is None:
            continue
        start = max(s.started_at, since)
        seconds = max(0, int((s.last_seen_at - start).total_seconds()))
        if reference is not None and s.price_minor * 10_000 >= reference * (10_000 - half):
            above_ref += seconds
        if target is not None and s.price_minor > target:
            above_target += seconds
    reset = (
        c.watch is not None and c.watch.target_set_at is not None and c.watch.target_set_at > since
    )
    return RearmEvidence(above_ref, above_target, reset)


def _follow_reply(watch_id: int, title: str, offer: Offer) -> str:
    price = format_money(offer.price) if offer.price is not None else "sin precio"
    stock = "en stock" if offer.in_stock else "sin stock"
    seller = " · vende un tercero" if offer.seller_class is SellerClass.MARKETPLACE else ""
    return (
        f"Siguiendo p{watch_id}: {esc(truncate(title, MAX_TITLE))} · {price} · {stock}{seller}"
        " · alertas por histórico desde 3 días de cobertura"
    )
