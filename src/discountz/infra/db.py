"""Acceso a Postgres: conexión, lease y funciones SQL (Repository como módulo de funciones).

Reglas: transacciones cortas con `with conn.transaction()` y nunca HTTP dentro de una.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from discountz.domain.evaluate import ListingState
from discountz.domain.health import BreakerState, HourStat
from discountz.domain.model import AlertKind, Maturity, Offer, SellerClass
from discountz.domain.scheduling import Event, PlanInput, SearchDue, SourceBudget, WatchDue
from discountz.domain.segments import (
    CloseAndOpen,
    Extend,
    Observation,
    Segment,
    SegmentStatus,
    apply_sample,
)

SCHEMA_VERSION = 1
CONNECT_TIMEOUT_S = 10
LEASE_TTL = "16 minutes"
STUCK_SENDING = timedelta(minutes=10)
MAX_ATTEMPTS = 5
CANARY_EVERY = timedelta(hours=24)

RunStatus = Literal["running", "ok", "partial", "failed", "skipped_locked", "skipped_schema"]
Purpose = Literal["alert", "digest", "reply", "system"]


def connect(url: str, *, application_name: str = "discountz") -> psycopg.Connection:
    """Conexión en autocommit: cada escritura va en `with conn.transaction()` explícita."""
    conn = psycopg.connect(
        url,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        application_name=application_name,
    )
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def _one(conn: psycopg.Connection, query: str, params: Any = None) -> tuple[Any, ...] | None:
    return conn.execute(query, params).fetchone()


def _required(row: tuple[Any, ...] | None) -> tuple[Any, ...]:
    if row is None:
        raise RuntimeError("expected_row")
    return row


# ───────────── Lease y runs


def acquire_lease(conn: psycopg.Connection, holder: str) -> bool:
    """Toma el lease si está libre o vencido (el reloj de la BD manda)."""
    row = _one(
        conn,
        f"""UPDATE discountz.run_lease
               SET holder = %(holder)s, acquired_at = now(),
                   expires_at = now() + interval '{LEASE_TTL}'
             WHERE id = 1 AND (holder IS NULL OR expires_at < now())
         RETURNING holder""",
        {"holder": holder},
    )
    return row is not None


def release_lease(conn: psycopg.Connection, holder: str) -> None:
    conn.execute(
        """UPDATE discountz.run_lease SET holder = NULL, acquired_at = NULL, expires_at = NULL
            WHERE id = 1 AND holder = %(holder)s""",
        {"holder": holder},
    )


def schema_version(conn: psycopg.Connection) -> int:
    row = _one(conn, "SELECT to_regclass('discountz.schema_migrations') IS NOT NULL")
    if not row or not row[0]:
        return 0
    got = _one(conn, "SELECT coalesce(max(version), 0) FROM discountz.schema_migrations")
    return int(got[0]) if got else 0


def start_run(
    conn: psycopg.Connection,
    now: datetime,
    trigger_kind: str,
    gh_run_id: int | None,
    gh_run_attempt: int | None,
) -> int:
    """Registra el run, marca como `failed` los huérfanos y calcula `gap_seconds`."""
    with conn.transaction():
        conn.execute(
            """UPDATE discountz.run SET status = 'failed', finished_at = %(now)s,
                      error_code = 'orphaned'
                WHERE status = 'running' AND started_at < %(now)s - interval '16 minutes'""",
            {"now": now},
        )
        prev = _one(conn, "SELECT max(started_at) FROM discountz.run")
        gap = (
            None
            if prev is None or prev[0] is None
            else max(0, int((now - prev[0]).total_seconds()))
        )
        row = _one(
            conn,
            """INSERT INTO discountz.run (gh_run_id, gh_run_attempt, trigger_kind, started_at,
                                          gap_seconds)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (gh_run_id, gh_run_attempt, trigger_kind, now, gap),
        )
    return int(_required(row)[0])


@dataclass(frozen=True, slots=True)
class RunCounters:
    requests: int = 0
    alerts_created: int = 0
    messages_sent: int = 0
    updates_processed: int = 0
    error_code: str | None = None


def finish_run(
    conn: psycopg.Connection, run_id: int, now: datetime, status: RunStatus, counters: RunCounters
) -> None:
    conn.execute(
        """UPDATE discountz.run
              SET status = %s, finished_at = %s, requests = %s, alerts_created = %s,
                  messages_sent = %s, updates_processed = %s, error_code = %s
            WHERE id = %s""",
        (
            status,
            now,
            counters.requests,
            counters.alerts_created,
            counters.messages_sent,
            counters.updates_processed,
            counters.error_code,
            run_id,
        ),
    )


# ───────────── Listings e histórico


_LISTING_COLS = (
    "id, category, alltime_min_minor, history_reset_at, last_price_minor, hot_until, muted_until"
)


def _listing_state(row: tuple[Any, ...]) -> ListingState:
    return ListingState(
        id=int(row[0]),
        category=str(row[1]),
        alltime_min_minor=row[2],
        history_reset_at=row[3],
        last_price_minor=row[4],
        hot_until=row[5],
        muted_until=row[6],
    )


def get_listing(conn: psycopg.Connection, listing_id: int) -> ListingState | None:
    row = _one(conn, f"SELECT {_LISTING_COLS} FROM discountz.listing WHERE id = %s", (listing_id,))
    return None if row is None else _listing_state(row)


def find_listing(conn: psycopg.Connection, source: str, external_id: str) -> int | None:
    row = _one(
        conn,
        "SELECT id FROM discountz.listing WHERE source = %s AND external_id = %s",
        (source, external_id),
    )
    return None if row is None else int(row[0])


def upsert_listing(
    conn: psycopg.Connection, offer: Offer, category: str, canonical_url: str, now: datetime
) -> ListingState:
    """Crea o actualiza el listing; devuelve el estado PREVIO (con el `last_price` de antes)."""
    price = offer.price.minor if offer.price else None
    list_price = offer.list_price.minor if offer.list_price else None
    with conn.transaction():
        before = _one(
            conn,
            f"""SELECT {_LISTING_COLS} FROM discountz.listing
                 WHERE source = %s AND external_id = %s FOR UPDATE""",
            (offer.ref.source, offer.ref.external_id),
        )
        if before is None:
            row = _one(
                conn,
                f"""INSERT INTO discountz.listing (source, external_id, parent_external_id,
                        canonical_url, title, category, first_seen_at, last_seen_at,
                        last_price_minor, last_list_price_minor, last_in_stock, last_seller_class)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                 RETURNING {_LISTING_COLS}""",
                (
                    offer.ref.source,
                    offer.ref.external_id,
                    offer.parent_external_id,
                    canonical_url,
                    offer.title[:300],
                    category,
                    now,
                    now,
                    price,
                    list_price,
                    offer.in_stock,
                    offer.seller_class.value,
                ),
            )
            created = _listing_state(_required(row))
            return ListingState(
                created.id, created.category, None, None, None, None, created.muted_until
            )
        conn.execute(
            """UPDATE discountz.listing
                  SET title = %s, canonical_url = %s, last_seen_at = greatest(last_seen_at, %s),
                      last_price_minor = %s, last_list_price_minor = %s, last_in_stock = %s,
                      last_seller_class = %s, parent_external_id = %s, gone_at = NULL
                WHERE id = %s""",
            (
                offer.title[:300],
                canonical_url,
                now,
                price,
                list_price,
                offer.in_stock,
                offer.seller_class.value,
                offer.parent_external_id,
                before[0],
            ),
        )
    return _listing_state(before)


_SEG_COLS = (
    "listing_id, started_at, last_seen_at, ended_at, price_minor, list_price_minor, in_stock,"
    " seller_class, unit_key, status, sample_count"
)


def _segment(row: tuple[Any, ...]) -> Segment:
    return Segment(
        listing_id=int(row[0]),
        started_at=row[1],
        last_seen_at=row[2],
        ended_at=row[3],
        price_minor=row[4],
        list_price_minor=row[5],
        in_stock=bool(row[6]),
        seller_class=SellerClass(row[7]),
        unit_key=str(row[8]),
        status=SegmentStatus(row[9]),
        sample_count=int(row[10]),
    )


def open_segment(conn: psycopg.Connection, listing_id: int) -> Segment | None:
    row = _one(
        conn,
        f"SELECT {_SEG_COLS} FROM discountz.price_segment"
        " WHERE listing_id = %s AND ended_at IS NULL",
        (listing_id,),
    )
    return None if row is None else _segment(row)


def load_history(
    conn: psycopg.Connection, listing_ids: Sequence[int], since: datetime
) -> dict[int, list[Segment]]:
    """Segmentos de N listings desde `since` en una sola ida (`= ANY`)."""
    out: dict[int, list[Segment]] = {lid: [] for lid in listing_ids}
    rows = conn.execute(
        f"""SELECT {_SEG_COLS} FROM discountz.price_segment
             WHERE listing_id = ANY(%s) AND coalesce(ended_at, 'infinity') >= %s
             ORDER BY listing_id, started_at""",
        (list(listing_ids), since),
    ).fetchall()
    for row in rows:
        out[int(row[0])].append(_segment(row))
    return out


def write_sample(
    conn: psycopg.Connection,
    listing_id: int,
    obs: Observation,
    status: SegmentStatus,
    h: timedelta,
) -> Segment:
    """Aplica una lectura al histórico (extender o cerrar y abrir) en una transacción corta."""
    with conn.transaction():
        row = _one(
            conn,
            f"SELECT {_SEG_COLS} FROM discountz.price_segment"
            " WHERE listing_id = %s AND ended_at IS NULL FOR UPDATE",
            (listing_id,),
        )
        current = None if row is None else _segment(row)
        action = apply_sample(current, obs, h, status)
        if isinstance(action, Extend):
            seg = action.segment
            conn.execute(
                """UPDATE discountz.price_segment
                      SET last_seen_at = %s, sample_count = %s, list_price_minor = %s
                    WHERE listing_id = %s AND ended_at IS NULL""",
                (seg.last_seen_at, seg.sample_count, seg.list_price_minor, listing_id),
            )
            return seg
        if isinstance(action, CloseAndOpen):
            if action.closed is not None:
                conn.execute(
                    "UPDATE discountz.price_segment SET ended_at = %s"
                    " WHERE listing_id = %s AND ended_at IS NULL",
                    (obs.t, listing_id),
                )
            new = action.opened
            conn.execute(
                """INSERT INTO discountz.price_segment (listing_id, started_at, last_seen_at,
                       price_minor, list_price_minor, in_stock, seller_class, unit_key, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    listing_id,
                    new.started_at,
                    new.last_seen_at,
                    new.price_minor,
                    new.list_price_minor,
                    new.in_stock,
                    new.seller_class.value,
                    new.unit_key,
                    new.status.value,
                ),
            )
            return Segment(
                listing_id,
                new.started_at,
                new.last_seen_at,
                None,
                new.price_minor,
                new.list_price_minor,
                new.in_stock,
                new.seller_class,
                new.unit_key,
                new.status,
                1,
            )
    if current is None:  # Ignore solo ocurre con un segmento abierto
        raise RuntimeError("ignored_without_segment")
    return current


def set_segment_status(
    conn: psycopg.Connection, listing_id: int, started_at: datetime, status: SegmentStatus
) -> None:
    conn.execute(
        "UPDATE discountz.price_segment SET status = %s WHERE listing_id = %s AND started_at = %s",
        (status.value, listing_id, started_at),
    )


def bump_hot(conn: psycopg.Connection, listing_id: int, until: datetime) -> None:
    conn.execute(
        """UPDATE discountz.listing SET hot_until = greatest(coalesce(hot_until, %s), %s)
            WHERE id = %s""",
        (until, until, listing_id),
    )


def set_pending_confirmation(
    conn: psycopg.Connection, listing_id: int, at: datetime | None
) -> None:
    conn.execute(
        "UPDATE discountz.listing SET pending_confirmation_at = %s WHERE id = %s",
        (at, listing_id),
    )


# ───────────── Alertas y outbox


@dataclass(frozen=True, slots=True)
class NewAlert:
    listing_id: int
    segment_started_at: datetime | None
    watch_id: int | None
    kind: AlertKind
    maturity: Maturity
    price_minor: int
    reference_minor: int | None
    real_saving_bp: int | None
    advertised_saving_bp: int | None
    flags: tuple[str, ...]
    delivery: Literal["new", "muted", "suppressed"]
    observed_at: datetime


def insert_alert(conn: psycopg.Connection, alert: NewAlert) -> int | None:
    """Inserta la alerta; `None` si ya existía (`alert_dedup_uq`)."""
    row = _one(
        conn,
        """INSERT INTO discountz.alert (listing_id, segment_started_at, watch_id, kind, maturity,
               price_minor, reference_minor, real_saving_bp, advertised_saving_bp, flags,
               delivery, observed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT ON CONSTRAINT alert_dedup_uq DO NOTHING
           RETURNING id""",
        (
            alert.listing_id,
            alert.segment_started_at,
            alert.watch_id,
            alert.kind.value,
            alert.maturity.value,
            alert.price_minor,
            alert.reference_minor,
            alert.real_saving_bp,
            alert.advertised_saving_bp,
            list(alert.flags),
            alert.delivery,
            alert.observed_at,
        ),
    )
    return None if row is None else int(row[0])


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    purpose: str
    dedup_key: str
    priority: int
    body_html: str
    reply_markup: dict[str, Any] | None
    silent: bool
    reply_to_message_id: int | None
    attempts: int


def enqueue(
    conn: psycopg.Connection,
    purpose: Purpose,
    body_html: str,
    *,
    priority: int,
    dedup_key: str,
    now: datetime,
    ttl: timedelta,
    reply_markup: Mapping[str, Any] | None = None,
    silent: bool = False,
    reply_to_message_id: int | None = None,
) -> int:
    """Encola un mensaje; si el `dedup_key` ya existe devuelve el id existente."""
    row = _one(
        conn,
        """INSERT INTO discountz.outbox (purpose, dedup_key, priority, body_html, reply_markup,
               silent, reply_to_message_id, not_before, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (dedup_key) DO NOTHING
           RETURNING id""",
        (
            purpose,
            dedup_key,
            priority,
            body_html,
            None if reply_markup is None else Jsonb(dict(reply_markup)),
            silent,
            reply_to_message_id,
            now,
            now + ttl,
        ),
    )
    if row is None:
        row = _one(conn, "SELECT id FROM discountz.outbox WHERE dedup_key = %s", (dedup_key,))
    return int(_required(row)[0])


def claim_outbox(conn: psycopg.Connection, limit: int, now: datetime) -> list[OutboxRow]:
    """Marca como `sending` hasta `limit` mensajes listos, por prioridad; expira los vencidos."""
    with conn.transaction():
        conn.execute(
            """UPDATE discountz.outbox SET status = 'expired'
                WHERE status = 'pending' AND expires_at <= %s""",
            (now,),
        )
        rows = conn.execute(
            """UPDATE discountz.outbox o SET status = 'sending', sending_since = %(now)s
                 FROM (SELECT id FROM discountz.outbox
                        WHERE status = 'pending' AND not_before <= %(now)s
                        ORDER BY priority, not_before, id
                        LIMIT %(limit)s FOR UPDATE SKIP LOCKED) ready
                WHERE o.id = ready.id
            RETURNING o.id, o.purpose, o.dedup_key, o.priority, o.body_html, o.reply_markup,
                      o.silent, o.reply_to_message_id, o.attempts""",
            {"now": now, "limit": limit},
        ).fetchall()
    out = [OutboxRow(*row) for row in rows]
    return sorted(out, key=lambda r: (r.priority, r.id))


def mark_sent(conn: psycopg.Connection, outbox_id: int, message_id: int, now: datetime) -> None:
    conn.execute(
        """UPDATE discountz.outbox
              SET status = 'sent', sent_at = %s, telegram_message_id = %s, sending_since = NULL
            WHERE id = %s""",
        (now, message_id, outbox_id),
    )


def mark_retry(conn: psycopg.Connection, outbox_id: int, error_code: str) -> None:
    """Vuelve a `pending` con un intento más; con 5 intentos pasa a `failed`."""
    conn.execute(
        """UPDATE discountz.outbox
              SET attempts = least(attempts + 1, 10), last_error_code = %s, sending_since = NULL,
                  status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END
            WHERE id = %s""",
        (error_code[:60], MAX_ATTEMPTS, outbox_id),
    )


def mark_failed(conn: psycopg.Connection, outbox_id: int, error_code: str) -> None:
    conn.execute(
        """UPDATE discountz.outbox SET status = 'failed', last_error_code = %s,
                  sending_since = NULL WHERE id = %s""",
        (error_code[:60], outbox_id),
    )


def recover_outbox(conn: psycopg.Connection, now: datetime) -> int:
    """Lo que lleva más de 10 min en `sending` vuelve a `pending` con un intento más."""
    cur = conn.execute(
        """UPDATE discountz.outbox
              SET status = 'pending', sending_since = NULL, attempts = least(attempts + 1, 10)
            WHERE status = 'sending' AND sending_since < %s""",
        (now - STUCK_SENDING,),
    )
    return cur.rowcount


# ───────────── Fuentes: breaker y contadores


def load_breakers(conn: psycopg.Connection) -> dict[str, BreakerState]:
    rows = conn.execute(
        """SELECT code, breaker, breaker_open_until, breaker_trips, consecutive_unavailable,
                  consecutive_parse_failures, quarantine_cycles, last_trip_at, last_trip_kind
             FROM discountz.source WHERE enabled"""
    ).fetchall()
    return {r[0]: BreakerState(r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]) for r in rows}


def save_breaker(conn: psycopg.Connection, source: str, b: BreakerState) -> None:
    persisted = "open" if b.state != "closed" else "closed"
    conn.execute(
        """UPDATE discountz.source
              SET breaker = %s, breaker_open_until = %s, breaker_trips = %s,
                  consecutive_unavailable = %s, consecutive_parse_failures = %s,
                  quarantine_cycles = %s, last_trip_at = %s, last_trip_kind = %s
            WHERE code = %s""",
        (
            persisted,
            b.open_until if persisted == "open" else None,
            b.trips,
            b.consecutive_unavailable,
            b.consecutive_parse,
            b.quarantine_cycles,
            b.last_trip_at,
            b.last_trip_kind,
            source,
        ),
    )


def add_hour_stat(
    conn: psycopg.Connection,
    source: str,
    hour: datetime,
    stat: HourStat,
    *,
    unavailable: int = 0,
    not_found: int = 0,
    offers: int = 0,
    latency_ms_sum: int = 0,
    latency_ms_max: int = 0,
) -> None:
    """Suma contadores a la hora (UPSERT); `hour` se trunca a la hora."""
    start = hour.replace(minute=0, second=0, microsecond=0)
    conn.execute(
        """INSERT INTO discountz.source_hourly_stat AS s (source, hour_start, requests, ok,
               blocked, unavailable, parse_errors, not_found, confirm_mismatch, soft_empty, offers,
               latency_ms_sum, latency_ms_max)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (source, hour_start) DO UPDATE SET
               requests = s.requests + EXCLUDED.requests, ok = s.ok + EXCLUDED.ok,
               blocked = s.blocked + EXCLUDED.blocked,
               unavailable = s.unavailable + EXCLUDED.unavailable,
               parse_errors = s.parse_errors + EXCLUDED.parse_errors,
               not_found = s.not_found + EXCLUDED.not_found,
               confirm_mismatch = s.confirm_mismatch + EXCLUDED.confirm_mismatch,
               soft_empty = s.soft_empty + EXCLUDED.soft_empty,
               offers = s.offers + EXCLUDED.offers,
               latency_ms_sum = s.latency_ms_sum + EXCLUDED.latency_ms_sum,
               latency_ms_max = greatest(s.latency_ms_max, EXCLUDED.latency_ms_max)""",
        (
            source,
            start,
            stat.requests,
            stat.ok,
            stat.blocked,
            unavailable,
            stat.parse_errors,
            not_found,
            stat.confirm_mismatch,
            stat.soft_empty,
            offers,
            latency_ms_sum,
            latency_ms_max,
        ),
    )


def load_hour_stats(conn: psycopg.Connection, source: str, now: datetime) -> list[HourStat]:
    """Las últimas 24 horas en orden, con ceros en las horas sin datos."""
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=23)
    rows = conn.execute(
        """SELECT hour_start, requests, ok, blocked, parse_errors, soft_empty, confirm_mismatch
             FROM discountz.source_hourly_stat
            WHERE source = %s AND hour_start BETWEEN %s AND %s""",
        (source, start, end),
    ).fetchall()
    by_hour = {r[0]: HourStat(*r[1:]) for r in rows}
    zero = HourStat(0, 0, 0, 0, 0, 0)
    return [by_hour.get(start + timedelta(hours=i), zero) for i in range(24)]


# ───────────── Seguimientos y búsquedas


@dataclass(frozen=True, slots=True)
class WatchCreated:
    id: int
    created: bool


def create_watch(
    conn: psycopg.Connection,
    source: str,
    pending_ref: str,
    category: str,
    target_minor: int | None,
    now: datetime,
) -> WatchCreated:
    """Crea un `watch` pendiente; idempotente por `watch_pending_uq`."""
    row = _one(
        conn,
        """INSERT INTO discountz.watch (source, pending_ref, requested_category,
               target_price_minor, target_set_at, next_check_at)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (source, pending_ref) WHERE pending_ref IS NOT NULL DO NOTHING
           RETURNING id""",
        (source, pending_ref, category, target_minor, now if target_minor else None, now),
    )
    if row is not None:
        return WatchCreated(int(row[0]), True)
    existing = _one(
        conn,
        "SELECT id FROM discountz.watch WHERE source = %s AND pending_ref = %s",
        (source, pending_ref),
    )
    return WatchCreated(int(_required(existing)[0]), False)


def create_watch_on_listing(
    conn: psycopg.Connection,
    source: str,
    listing_id: int,
    target_minor: int | None,
    now: datetime,
) -> WatchCreated:
    """Seguimiento activo sobre un listing ya conocido (conserva su histórico)."""
    row = _one(
        conn,
        """INSERT INTO discountz.watch (source, listing_id, status, target_price_minor,
               target_set_at, next_check_at)
           VALUES (%s, %s, 'active', %s, %s, %s)
           ON CONFLICT (listing_id) WHERE listing_id IS NOT NULL DO NOTHING
           RETURNING id""",
        (source, listing_id, target_minor, now if target_minor else None, now),
    )
    if row is not None:
        return WatchCreated(int(row[0]), True)
    existing = _one(conn, "SELECT id FROM discountz.watch WHERE listing_id = %s", (listing_id,))
    return WatchCreated(int(_required(existing)[0]), False)


def activate_watch(conn: psycopg.Connection, watch_id: int, listing_id: int) -> None:
    conn.execute(
        """UPDATE discountz.watch SET listing_id = %s, pending_ref = NULL, status = 'active'
            WHERE id = %s AND status = 'pending'""",
        (listing_id, watch_id),
    )


def create_search(
    conn: psycopg.Connection,
    source: str,
    query: str,
    category: str,
    exclude: Sequence[str],
    min_minor: int | None,
    max_minor: int | None,
    now: datetime,
) -> int:
    row = _one(
        conn,
        """INSERT INTO discountz.search (source, query, category, exclude_terms,
               min_price_minor, max_price_minor, next_run_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT ON CONSTRAINT search_uq DO UPDATE SET
               category = EXCLUDED.category, exclude_terms = EXCLUDED.exclude_terms,
               min_price_minor = EXCLUDED.min_price_minor,
               max_price_minor = EXCLUDED.max_price_minor, enabled = true
           RETURNING id""",
        (source, query, category, list(exclude), min_minor, max_minor, now),
    )
    return int(_required(row)[0])


def drop_ref(conn: psycopg.Connection, ref: str) -> bool:
    """Borra el `watch` (pN) o la `search` (bN). El listing y su histórico se conservan."""
    table = {"p": "watch", "b": "search"}.get(ref[:1])
    if table is None or not ref[1:].isdigit():
        return False
    cur = conn.execute(f"DELETE FROM discountz.{table} WHERE id = %s", (int(ref[1:]),))
    return cur.rowcount == 1


# ───────────── Plan


def load_plan_input(
    conn: psycopg.Connection,
    now: datetime,
    budgets: Mapping[str, SourceBudget],
    events: Sequence[Event],
    cycle: timedelta,
) -> PlanInput:
    """Lee lo vencido y el estado de las fuentes activas para `plan_run`."""
    del cycle  # el intervalo efectivo se aplica al reprogramar (next_check_at)
    sources = conn.execute(
        """SELECT code, kind, ramp_until, canary_checked_at FROM discountz.source
            WHERE enabled"""
    ).fetchall()
    enabled = [r[0] for r in sources if r[0] in budgets]
    usage: defaultdict[str, int] = defaultdict(int)
    for source, total in conn.execute(
        """SELECT source, sum(requests) FROM discountz.source_hourly_stat
            WHERE hour_start >= %s GROUP BY source""",
        (now - timedelta(hours=24),),
    ).fetchall():
        if source in budgets:
            usage[budgets[source].group] += int(total)
    breakers = {k: v for k, v in load_breakers(conn).items() if k in enabled}
    paused_row = _one(conn, "SELECT discovery_paused FROM discountz.bot_state WHERE id = 1")
    pending = conn.execute(
        """SELECT source, id FROM discountz.listing
            WHERE pending_confirmation_at IS NOT NULL AND source = ANY(%s)""",
        (enabled,),
    ).fetchall()
    hot = conn.execute(
        """SELECT source, id FROM discountz.listing
            WHERE hot_until > %s AND source = ANY(%s) ORDER BY hot_until DESC""",
        (now, enabled),
    ).fetchall()
    watches = conn.execute(
        """SELECT id, source, listing_id, status = 'pending', target_price_minor IS NOT NULL,
                  next_check_at
             FROM discountz.watch
            WHERE status IN ('pending', 'active') AND next_check_at <= %s
              AND source = ANY(%s)""",
        (now, enabled),
    ).fetchall()
    searches = conn.execute(
        """SELECT s.id, s.source, src.kind, s.next_run_at
             FROM discountz.search s JOIN discountz.source src ON src.code = s.source
            WHERE s.enabled AND s.next_run_at <= %s AND s.source = ANY(%s)""",
        (now, enabled),
    ).fetchall()
    canaries = [
        r[0]
        for r in sources
        if r[0] in enabled and r[1] == "store" and (r[3] is None or now - r[3] >= CANARY_EVERY)
    ]
    return PlanInput(
        budgets={k: v for k, v in budgets.items() if k in enabled},
        breakers=breakers,
        usage_24h=dict(usage),
        ramp_until={r[0]: r[2] for r in sources},
        events=tuple(events),
        discovery_paused=bool(paused_row and paused_row[0]),
        pending_confirmations=[(r[0], int(r[1])) for r in pending],
        canaries_due=canaries,
        watches=[
            WatchDue(
                int(r[0]), r[1], r[2], bool(r[3]), bool(r[4]), r[5], budgets[r[1]].watch_interval
            )
            for r in watches
        ],
        hot_listings=[(r[0], int(r[1])) for r in hot],
        searches=[
            SearchDue(
                int(r[0]),
                r[1],
                "feed" if r[2] == "feed" else "search",
                r[3],
                budgets[r[1]].search_interval,
            )
            for r in searches
        ],
    )


def is_unique_violation(exc: BaseException) -> bool:
    return isinstance(exc, errors.UniqueViolation)
