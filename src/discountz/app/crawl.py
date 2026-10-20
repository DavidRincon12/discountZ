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
from discountz.domain.catalog import (
    CatalogCandidate,
    CatalogObservation,
    CatalogState,
    model_candidate,
    observe,
)
from discountz.domain.errors import (
    BudgetExhausted,
    HardBlock,
    ParseError,
    SourceError,
    Unavailable,
)
from discountz.domain.guards import check_offer
from discountz.domain.health import BreakerEvent, BreakerState, breaker_transition, effective_state
from discountz.domain.model import AlertKind, ListingRef, Maturity, Offer, SellerClass
from discountz.domain.money import format_money
from discountz.domain.scheduling import CrawlBudget
from discountz.domain.segments import Observation, SegmentStatus, gap_h
from discountz.domain.topics import TopicParams, assign_topic
from discountz.infra import db
from discountz.infra.http import CATALOG_MAX_BODY_BYTES, HttpClient, PacedClient
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
MODEL_WINDOW = timedelta(days=7)  # pares del mismo modelo (spec de eBay §4.2)
QUOTA = "quota"  # cuota diaria de una API agotada: no es una falla de la fuente
ALERT_TTL = timedelta(hours=24)
PARSE_STOP = 3  # errores de parseo seguidos que detienen la corrida
# Un corte de la tienda puede durar más que los reintentos del cliente HTTP (~6 s): antes de
# rendirse, la misma petición se repite tras una pausa (corridas VTEX cortadas por
# `retries_exhausted` en sus primeros minutos, 2026-10-18 y 19).
UNAVAILABLE_TRIES = 3
UNAVAILABLE_PAUSE_S = 60.0
# Caída de la BD a mitad de corrida (pooler, 18-oct 20:42 y 19-oct 11:00): reconexiones y pausa.
DB_RECONNECTS = 2
DB_RECONNECT_PAUSE_S = 30.0
# Lo único que una pausa puede arreglar: red, descarga lenta y 5xx o 429 tras los reintentos.
TRANSIENT_CODES = frozenset({"network", "timeout", "deadline", "retries_exhausted"})
CONFIRM_TOL_PCT = 1  # la relectura debe quedar a ±1 % del precio visto
SAMPLE_GAP = gap_h(timedelta(hours=6))
ERRORS_TOPIC = "errores"

# Con la BD en ≥ 450 MB el mantenimiento pausa el descubrimiento: también el rastreo de las
# tiendas más grandes (spec del rastreo §7.2).
PAUSED_AT_450 = frozenset({"exito", "carulla", "ebay_us", "alkosto", "jumbo"})
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
    max_items: int | None = None  # tope de publicaciones guardadas (eBay: 150 mil)


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
    peers: int = 0  # > 0: la referencia es la mediana de esos pares del mismo modelo


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
        self.cap = deps.budget.run_cap(self.day_base, now.hour)
        self.http = PacedClient(
            deps.http,
            deps.budget.paced(self.code, max(self.cap, 1)),
            deps.sleep,
            deps.monotonic,
            deps.rnd,
            deps.source.detect_block,
            source=self.code,
            deadline=end,
            max_body=CATALOG_MAX_BODY_BYTES,
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
        """Ejecuta una petición de la fuente; `None` si la página no se pudo leer.

        Un error pasajero (red, 5xx) repite la misma petición tras una pausa, hasta
        `UNAVAILABLE_TRIES` veces; después la corrida para y la próxima sigue desde esa página.
        """
        for attempt in range(1, UNAVAILABLE_TRIES + 1):
            try:
                result = call()
            except ParseError as exc:
                self.log_error(exc)
                self.parse_streak += 1
                self.breaker_event(BreakerEvent.PARSE_ERROR)
                if self.parse_streak >= PARSE_STOP:
                    self.cursor = replace(self.cursor, page=self.cursor.page + 1)
                    raise _Stop("partial") from None
                return None
            except BudgetExhausted:
                raise _Stop("ok") from None
            except HardBlock as exc:
                self.log_error(exc)
                self.breaker_event(BreakerEvent.HARD_BLOCK)
                raise _Stop("blocked") from None
            except SourceError as exc:
                self.log_error(exc)
                if exc.code != QUOTA:  # la cuota vuelve sola mañana: no cuenta para el breaker
                    self.breaker_event(BreakerEvent.UNAVAILABLE)
                if not self.can_retry(exc, attempt):
                    raise _Stop("partial") from None
                self.deps.sleep(UNAVAILABLE_PAUSE_S)
                continue
            if attempt > 1:  # se recuperó: sus fallas ya no son seguidas para el breaker
                self.breaker_event(BreakerEvent.OK)
            self.parse_streak = 0
            self.ok_pages += 1
            return result
        raise AssertionError("inalcanzable")  # el último intento retorna o para la corrida

    def can_retry(self, exc: SourceError, attempt: int) -> bool:
        """Solo un corte pasajero, con intentos, breaker cerrado y tiempo para la pausa."""
        return (
            isinstance(exc, Unavailable)
            and exc.code in TRANSIENT_CODES
            and attempt < UNAVAILABLE_TRIES
            and effective_state(self.breaker, self.now) != "open"
            and self.deps.monotonic() + UNAVAILABLE_PAUSE_S < self.end
        )

    def log_error(self, exc: ParseError | SourceError) -> None:
        status = exc.http_status if isinstance(exc, SourceError) else None
        event(
            log,
            "source_error",
            logging.WARNING,
            source=self.code,
            code=exc.code,
            http_status=status,
        )

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
        items = self.within_cap(items, states)
        peers = self.model_peers(items)
        rows: dict[str, db.CatalogRow] = {}
        found: list[_Found] = []
        for o in items:
            topic = assign_topic(self.deps.topics.rules, self.code, o.category_path)
            prev = states.get(o.external_id)
            state, cand, changed = observe(prev, o, self.now, params[topic], self.deps.params.glob)
            currency = o.price.currency.value
            rows[o.external_id] = db.CatalogRow(
                o.external_id, o.ean, topic, currency, state, changed, o.model_key
            )
            self.items += 1
            self.changes += int(changed)
            best = self.best_candidate(prev, o, cand, peers, topic)
            if best is not None:
                found.append(best)
        fresh = _cheapest_per_model([f for f in found if not self.duplicate(f)])
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

    def within_cap(
        self, items: Sequence[CatalogObservation], states: Mapping[str, CatalogState]
    ) -> Sequence[CatalogObservation]:
        """Con el tope alcanzado solo se actualizan las publicaciones ya guardadas."""
        cap = self.deps.max_items
        if cap is None or db.count_catalog_items(self.conn, self.code) < cap:
            return items
        return [o for o in items if o.external_id in states]

    def model_peers(self, items: Sequence[CatalogObservation]) -> dict[str, list[tuple[str, int]]]:
        """Pares guardados más los de esta misma página, con el precio de la página: las
        publicaciones de un modelo suelen salir juntas, así que la primera lectura de una
        categoría también compara."""
        keys = sorted({o.model_key for o in items if o.model_key is not None})
        stored = db.model_peer_prices(self.conn, self.code, keys, self.now - MODEL_WINDOW)
        merged = {k: dict(v) for k, v in stored.items()}
        for o in items:
            if o.model_key is not None:
                merged.setdefault(o.model_key, {})[o.external_id] = o.price.minor
        return {k: list(v.items()) for k, v in merged.items()}

    def best_candidate(
        self,
        prev: CatalogState | None,
        o: CatalogObservation,
        cand: CatalogCandidate | None,
        peers: Mapping[str, list[tuple[str, int]]],
        topic: str,
    ) -> _Found | None:
        """Bajada de la misma publicación (§4.1) o precio bajo frente al modelo (§4.2): gana la
        de mayor descuento."""
        best = _Found(o, cand, topic) if cand is not None else None
        if o.model_key is None:
            return best
        prices = [p for ext, p in peers.get(o.model_key, []) if ext != o.external_id]
        params = self.deps.topics.params[topic]
        model = model_candidate(prev, self.now, o, prices, params, self.deps.params.glob)
        if model is not None and (best is None or model.drop_bp > best.candidate.drop_bp):
            return _Found(o, model, topic, len(prices))
        return best

    def duplicate(self, f: _Found) -> bool:
        if f.obs.model_key is not None and db.recent_alert_for_model(
            self.conn,
            self.code,
            f.obs.model_key,
            f.obs.price.minor,
            self.now - EAN_WINDOW,
            exclude_external_id=f.obs.external_id,
        ):
            event(log, "catalog_skip", source=self.code, code="model_duplicate")
            return True
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
            card = _card(self.code, f.topic, title, url, facts, cand, f.peers)
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


def _cheapest_per_model(found: Sequence[_Found]) -> list[_Found]:
    """Dos publicaciones del mismo modelo en una página alertan una sola vez: la más barata."""
    best: dict[str, _Found] = {}
    out: list[_Found] = []
    for f in found:
        key = f.obs.model_key
        if key is None:
            out.append(f)
        elif key not in best or f.obs.price.minor < best[key].obs.price.minor:
            best[key] = f
    return out + list(best.values())


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
    peers: int = 0,
) -> dict[str, Any]:
    if peers:
        label = f"Mediana de {peers} publicaciones del mismo modelo"
    else:
        label = facts.reference_label if facts else "Antes"
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
        "reference_label": label,
        "note": facts.note if facts else None,
        "peers": peers,
    }


def _error_line(store: str, title: str, cand: CatalogCandidate, listing_id: int) -> str:
    return (
        f"🔥 Posible error de precio en {esc(source_name(store))}: "
        f"<b>{esc(truncate(title, 120))}</b> a <b>{format_money(cand.price)}</b> "
        f"(antes {format_money(cand.usual)}). Detalle en «Errores de precio» · L{listing_id}"
    )


def crawl_max_items(code: str, params: AppParams) -> int | None:
    """Tope de publicaciones guardadas: solo eBay lo tiene (spec de eBay §3)."""
    return params.ebay.max_items if code == "ebay_us" else None


def run_crawl(deps: CrawlDeps, now: datetime, minutes: int) -> CrawlResult:
    return _run_crawl(deps, now, deps.monotonic() + minutes * 60)


def run_crawl_reconnecting(
    deps: CrawlDeps, now: datetime, minutes: int, reconnect: Callable[[], psycopg.Connection]
) -> CrawlResult:
    """Corrida que sobrevive a una caída de la BD (el pooler de Supabase corta conexiones).

    Tras la caída espera, abre otra conexión y sigue desde el cursor guardado, con el mismo
    holder del lease y el mismo límite de tiempo; a la tercera caída la corrida se rinde.
    Cierra las conexiones que abre; la primera es de quien llama.
    """
    end = deps.monotonic() + minutes * 60
    opened: list[psycopg.Connection] = []
    try:
        for attempt in range(1, DB_RECONNECTS + 2):
            try:
                return _run_crawl(deps, now, end)
            except psycopg.OperationalError:
                if attempt > DB_RECONNECTS or deps.monotonic() + DB_RECONNECT_PAUSE_S >= end:
                    raise
                event(
                    log,
                    "db_reconnect",
                    logging.WARNING,
                    source=deps.source.descriptor.code,
                    count=attempt,
                )
                deps.sleep(DB_RECONNECT_PAUSE_S)
                opened.append(reconnect())
                deps = replace(deps, conn=opened[-1])
        raise AssertionError("inalcanzable")
    finally:
        for conn in opened:
            conn.close()


def _run_crawl(deps: CrawlDeps, now: datetime, end: float) -> CrawlResult:
    code = deps.source.descriptor.code
    breaker = db.load_breakers(deps.conn).get(code)
    if breaker is None or (code in PAUSED_AT_450 and db.bot_state(deps.conn).discovery_paused):
        return _skipped(code, "disabled")
    if effective_state(breaker, now) == "open":
        return _skipped(code, "blocked")
    if not db.acquire_crawl_lease(deps.conn, code, deps.holder, now, LEASE_TTL):
        return _skipped(code, "skipped_locked")
    run = _Run(deps, now, end, breaker)
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


def _skipped(code: str, status: CrawlStatus) -> CrawlResult:
    """Corrida que no pidió nada: el log dice por qué (con el breaker abierto sale en rojo)."""
    event(log, "crawl_done", source=code, status=status, requests=0)
    return CrawlResult(status, 0, 0, 0, 0, 0)
