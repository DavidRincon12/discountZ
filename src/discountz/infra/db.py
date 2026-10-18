"""Acceso a Postgres: conexión, lease y funciones SQL (Repository como módulo de funciones).

Reglas: transacciones cortas con `with conn.transaction()` y nunca HTTP dentro de una.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import psycopg
from psycopg import errors, sql
from psycopg.types.json import Jsonb

from discountz.domain.catalog import CatalogState
from discountz.domain.evaluate import AlertRecord, ListingState
from discountz.domain.health import BreakerState, HourStat
from discountz.domain.model import AlertKind, ListingRef, Maturity, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.scheduling import Event, PlanInput, SearchDue, SourceBudget, WatchDue
from discountz.domain.segments import (
    CloseAndOpen,
    Extend,
    Observation,
    Segment,
    SegmentStatus,
    apply_sample,
)

SCHEMA_VERSION = 5
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


def listing_last_seen(conn: psycopg.Connection, source: str, external_id: str) -> datetime | None:
    row = _one(
        conn,
        "SELECT last_seen_at FROM discountz.listing WHERE source = %s AND external_id = %s",
        (source, external_id),
    )
    if row is None:
        return None
    seen: datetime = row[0]
    return seen


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
    topic: str | None = None
    card: dict[str, Any] | None = None


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
    topic: str | None = None,
    card: Mapping[str, Any] | None = None,
) -> int:
    """Encola un mensaje; si el `dedup_key` ya existe devuelve el id existente.

    `topic` y `card` (migración 0002) solo se nombran si vienen: el aviso de migración
    pendiente se encola contra el esquema anterior.
    """
    cols = [
        "purpose",
        "dedup_key",
        "priority",
        "body_html",
        "reply_markup",
        "silent",
        "reply_to_message_id",
        "not_before",
        "expires_at",
    ]
    values: list[object] = [
        purpose,
        dedup_key,
        priority,
        body_html,
        None if reply_markup is None else Jsonb(dict(reply_markup)),
        silent,
        reply_to_message_id,
        now,
        now + ttl,
    ]
    if topic is not None:
        cols.append("topic")
        values.append(topic)
    if card is not None:
        cols.append("card")
        values.append(Jsonb(dict(card)))
    query = sql.SQL(
        "INSERT INTO discountz.outbox ({}) VALUES ({}) ON CONFLICT (dedup_key) DO NOTHING"
        " RETURNING id"
    ).format(
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)),
    )
    row = conn.execute(query, values).fetchone()
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
                      o.silent, o.reply_to_message_id, o.attempts, o.topic, o.card""",
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


def load_breaker_for_update(conn: psycopg.Connection, source: str) -> BreakerState | None:
    """Breaker de una fuente bloqueando su fila (dentro de una transacción) para no pisar al
    otro proceso que lo escribe."""
    r = _one(
        conn,
        """SELECT breaker, breaker_open_until, breaker_trips, consecutive_unavailable,
                  consecutive_parse_failures, quarantine_cycles, last_trip_at, last_trip_kind
             FROM discountz.source WHERE code = %s FOR UPDATE""",
        (source,),
    )
    return None if r is None else BreakerState(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])


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
    wid = int(_required(existing)[0])
    _make_manual(conn, wid)
    return WatchCreated(wid, False)


def _make_manual(conn: psycopg.Connection, watch_id: int) -> None:
    """Seguir a mano un producto que ya se seguía solo lo vuelve manual y sin vencimiento."""
    conn.execute(
        """UPDATE discountz.watch SET origin = 'manual', expires_at = NULL
            WHERE id = %s AND origin = 'auto'""",
        (watch_id,),
    )


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
    wid = int(_required(existing)[0])
    _make_manual(conn, wid)
    return WatchCreated(wid, False)


def create_auto_watch(
    conn: psycopg.Connection,
    source: str,
    ref: ListingRef,
    now: datetime,
    ttl: timedelta,
    *,
    cap: int,
) -> int | None:
    """Seguimiento automático (spec del rastreo §4.4); `None` si ya hay uno o el tope es 0.

    Con el tope lleno se quita el automático más viejo de la fuente: quedan los más nuevos.
    """
    if cap <= 0:
        return None
    pending = f"id:{ref.external_id}"
    taken = _one(
        conn,
        """SELECT 1 FROM discountz.watch w
             LEFT JOIN discountz.listing l ON l.id = w.listing_id
            WHERE w.source = %s
              AND (w.pending_ref = %s OR l.external_id = %s)""",
        (source, pending, ref.external_id),
    )
    if taken is not None:
        return None
    conn.execute(
        """DELETE FROM discountz.watch WHERE id IN (
               SELECT id FROM discountz.watch
                WHERE source = %s AND origin = 'auto'
                ORDER BY created_at DESC, id DESC OFFSET %s)""",
        (source, cap - 1),
    )
    found = _one(
        conn,
        "SELECT id FROM discountz.listing WHERE source = %s AND external_id = %s",
        (source, ref.external_id),
    )
    row = _one(
        conn,
        """INSERT INTO discountz.watch (source, listing_id, pending_ref, status, origin,
               expires_at, next_check_at, created_at)
           VALUES (%s, %s, %s, %s, 'auto', %s, %s, %s)
           RETURNING id""",
        (
            source,
            None if found is None else found[0],
            pending if found is None else None,
            "pending" if found is None else "active",
            now + ttl,
            now,
            now,
        ),
    )
    return int(_required(row)[0])


def extend_auto_watch(conn: psycopg.Connection, listing_id: int, until: datetime) -> None:
    """Una alerta del producto alarga su seguimiento automático (también si sigue pendiente)."""
    conn.execute(
        """UPDATE discountz.watch w SET expires_at = greatest(w.expires_at, %s)
             FROM discountz.listing l
            WHERE l.id = %s AND w.origin = 'auto' AND w.source = l.source
              AND (w.listing_id = l.id OR w.pending_ref = 'id:' || l.external_id)""",
        (until, listing_id),
    )


def expire_auto_watches(conn: psycopg.Connection, now: datetime) -> int:
    """Quita los automáticos vencidos; el listing y su histórico se conservan."""
    return conn.execute(
        "DELETE FROM discountz.watch WHERE origin = 'auto' AND expires_at < %s", (now,)
    ).rowcount


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
    """Borra el `watch` manual (pN), el automático (aN) o la `search` (bN).

    El listing y su histórico se conservan.
    """
    sql = {
        "p": "DELETE FROM discountz.watch WHERE id = %s AND origin = 'manual'",
        "a": "DELETE FROM discountz.watch WHERE id = %s AND origin = 'auto'",
        "b": "DELETE FROM discountz.search WHERE id = %s",
    }.get(ref[:1])
    if sql is None or not ref[1:].isdigit():
        return False
    return conn.execute(sql, (int(ref[1:]),)).rowcount == 1


def drop_auto_watches(conn: psycopg.Connection) -> int:
    """`/dejar auto`: quita todos los seguimientos automáticos; devuelve cuántos."""
    return conn.execute("DELETE FROM discountz.watch WHERE origin = 'auto'").rowcount


# ───────────── Telegram: offset y comandos


def telegram_offset(conn: psycopg.Connection) -> int:
    """Último `update_id` aplicado."""
    row = _one(conn, "SELECT telegram_offset FROM discountz.bot_state WHERE id = 1")
    return int(_required(row)[0])


def advance_offset(conn: psycopg.Connection, update_id: int) -> bool:
    """Avanza el offset solo hacia adelante; `False` = ese update ya se aplicó (no-op)."""
    cur = conn.execute(
        """UPDATE discountz.bot_state SET telegram_offset = %s
            WHERE id = 1 AND telegram_offset < %s""",
        (update_id, update_id),
    )
    return cur.rowcount == 1


def set_watch_reply_to(conn: psycopg.Connection, watch_id: int, message_id: int) -> None:
    """Mensaje de David al que responderá el ciclo tras la primera lectura."""
    conn.execute(
        "UPDATE discountz.watch SET reply_to_message_id = %s WHERE id = %s",
        (message_id, watch_id),
    )


def source_currency(conn: psycopg.Connection, code: str) -> Currency | None:
    row = _one(conn, "SELECT currency FROM discountz.source WHERE code = %s", (code,))
    return None if row is None else Currency(row[0].strip())


def watch_currency(conn: psycopg.Connection, watch_id: int) -> Currency | None:
    row = _one(
        conn,
        """SELECT s.currency FROM discountz.watch w JOIN discountz.source s ON s.code = w.source
            WHERE w.id = %s""",
        (watch_id,),
    )
    return None if row is None else Currency(row[0].strip())


def listing_source(conn: psycopg.Connection, listing_id: int) -> str | None:
    row = _one(conn, "SELECT source FROM discountz.listing WHERE id = %s", (listing_id,))
    return None if row is None else str(row[0])


def set_target(
    conn: psycopg.Connection, watch_id: int, target_minor: int | None, now: datetime
) -> bool:
    """Fija o quita el objetivo y fuerza la evaluación en el próximo plan."""
    cur = conn.execute(
        """UPDATE discountz.watch
              SET target_price_minor = %s, target_set_at = %s, next_check_at = %s
            WHERE id = %s""",
        (target_minor, None if target_minor is None else now, now, watch_id),
    )
    return cur.rowcount == 1


def set_category(conn: psycopg.Connection, ref: str, category: str) -> bool:
    """`L1234` → `listing.category`; `p12` → la del listing o, si está pendiente, la pedida."""
    num = int(ref[1:])
    if ref.startswith("L"):
        cur = conn.execute(
            "UPDATE discountz.listing SET category = %s WHERE id = %s", (category, num)
        )
        return cur.rowcount == 1
    row = _one(conn, "SELECT listing_id FROM discountz.watch WHERE id = %s", (num,))
    if row is None:
        return False
    if row[0] is None:
        conn.execute(
            "UPDATE discountz.watch SET requested_category = %s WHERE id = %s", (category, num)
        )
    else:
        conn.execute("UPDATE discountz.listing SET category = %s WHERE id = %s", (category, row[0]))
    return True


def mute(conn: psycopg.Connection, ref: str, until: datetime) -> bool:
    """`todo` → `bot_state`; `p12`/`L1234` → `listing.muted_until`. `False` si no existe."""
    if ref == "todo":
        conn.execute("UPDATE discountz.bot_state SET muted_until = %s WHERE id = 1", (until,))
        return True
    num = int(ref[1:])
    if ref.startswith("p"):
        row = _one(conn, "SELECT listing_id FROM discountz.watch WHERE id = %s", (num,))
        if row is None or row[0] is None:
            return False
        num = int(row[0])
    cur = conn.execute("UPDATE discountz.listing SET muted_until = %s WHERE id = %s", (until, num))
    return cur.rowcount == 1


def set_source_enabled(
    conn: psycopg.Connection, code: str, enabled: bool, ramp_until: datetime | None
) -> bool | None:
    """`/fuente on|off`; devuelve si estaba activa antes, o `None` si la fuente no existe.

    Al activar se cierra el breaker y se ponen en cero sus rachas: el dueño ya revisó la
    fuente y no tiene que esperar el backoff (hasta 12 h). `breaker_trips` se conserva, así
    que si sigue caída el siguiente disparo espera más. La rampa (`ramp_until`) solo se fija
    si la fuente venía apagada.
    """
    row = _one(
        conn,
        """WITH old AS (
             SELECT code, enabled FROM discountz.source WHERE code = %(code)s FOR UPDATE
           )
           UPDATE discountz.source s
              SET enabled = %(on)s,
                  ramp_until = CASE WHEN %(on)s AND NOT old.enabled THEN %(ramp)s
                                    ELSE s.ramp_until END,
                  breaker = CASE WHEN %(on)s THEN 'closed' ELSE s.breaker END,
                  breaker_open_until = CASE WHEN %(on)s THEN NULL ELSE s.breaker_open_until END,
                  consecutive_unavailable = CASE WHEN %(on)s THEN 0
                                                 ELSE s.consecutive_unavailable END,
                  consecutive_parse_failures = CASE WHEN %(on)s THEN 0
                                                    ELSE s.consecutive_parse_failures END
             FROM old
            WHERE s.code = old.code
        RETURNING old.enabled""",
        {"code": code, "on": enabled, "ramp": ramp_until},
    )
    return None if row is None else bool(row[0])


def set_token_expiry(conn: psycopg.Connection, day: date) -> None:
    conn.execute(
        "UPDATE discountz.bot_state SET dispatch_token_expires_on = %s WHERE id = 1", (day,)
    )


@dataclass(frozen=True, slots=True)
class WatchItem:
    id: int
    source: str
    title: str
    category: str
    price: Money | None
    target: Money | None
    pending: bool
    auto: bool = False


@dataclass(frozen=True, slots=True)
class SearchItem:
    id: int
    source: str
    query: str
    category: str
    enabled: bool


def list_watches(conn: psycopg.Connection) -> list[WatchItem]:
    rows = conn.execute(
        """SELECT w.id, w.source, coalesce(l.title, w.pending_ref),
                  coalesce(l.category, w.requested_category), l.last_price_minor,
                  w.target_price_minor, w.listing_id IS NULL, s.currency, w.origin = 'auto'
             FROM discountz.watch w
             JOIN discountz.source s ON s.code = w.source
             LEFT JOIN discountz.listing l ON l.id = w.listing_id
            ORDER BY w.id"""
    ).fetchall()
    out = []
    for wid, source, title, category, price, target, pending, currency, auto in rows:
        cur = Currency(currency.strip())
        out.append(
            WatchItem(
                int(wid),
                source,
                title,
                category,
                None if price is None else Money(int(price), cur),
                None if target is None else Money(int(target), cur),
                bool(pending),
                bool(auto),
            )
        )
    return out


def list_searches(conn: psycopg.Connection) -> list[SearchItem]:
    rows = conn.execute(
        "SELECT id, source, query, category, enabled FROM discountz.search ORDER BY id"
    ).fetchall()
    return [SearchItem(int(r[0]), r[1], r[2], r[3], bool(r[4])) for r in rows]


@dataclass(frozen=True, slots=True)
class SourceStatus:
    code: str
    enabled: bool
    health: str
    breaker: str
    breaker_open_until: datetime | None
    breaker_trips: int
    ok_24h: int
    requests_24h: int


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    last_run_started_at: datetime | None
    last_run_finished_at: datetime | None
    last_run_status: str | None
    gap_p50_s: int | None
    gap_p95_s: int | None
    sources: Sequence[SourceStatus]
    db_bytes: int
    overdue: int


def status_snapshot(conn: psycopg.Connection, now: datetime) -> StatusSnapshot:
    """Datos de `/estado`: último ciclo, intervalos de 24 h, fuentes, tamaño y atrasos."""
    last = _one(
        conn,
        """SELECT started_at, finished_at, status FROM discountz.run
            WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1""",
    )
    gaps = _required(
        _one(
            conn,
            """SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY gap_seconds),
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY gap_seconds)
                 FROM discountz.run
                WHERE started_at > %s - interval '24 hours' AND gap_seconds IS NOT NULL""",
            (now,),
        )
    )
    sources = conn.execute(
        """SELECT s.code, s.enabled, s.health, s.breaker, s.breaker_open_until, s.breaker_trips,
                  coalesce(sum(h.ok), 0), coalesce(sum(h.requests), 0)
             FROM discountz.source s
             LEFT JOIN discountz.source_hourly_stat h
               ON h.source = s.code AND h.hour_start > %s - interval '24 hours'
            GROUP BY s.code ORDER BY s.code""",
        (now,),
    ).fetchall()
    size = _required(_one(conn, "SELECT pg_database_size(current_database())"))
    overdue = _required(
        _one(
            conn,
            """SELECT count(*) FROM discountz.watch
                WHERE status IN ('pending','active') AND next_check_at < %s - interval '1 hour'""",
            (now,),
        )
    )
    return StatusSnapshot(
        None if last is None else last[0],
        None if last is None else last[1],
        None if last is None else last[2],
        None if gaps[0] is None else int(gaps[0]),
        None if gaps[1] is None else int(gaps[1]),
        [
            SourceStatus(r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]), int(r[7]))
            for r in sources
        ],
        int(size[0]),
        int(overdue[0]),
    )


# ───────────── Ciclo: seguimientos, búsquedas, alertas y fuentes


@dataclass(frozen=True, slots=True)
class WatchRow:
    id: int
    source: str
    listing_id: int | None
    pending_ref: str | None
    requested_category: str
    target_minor: int | None
    target_set_at: datetime | None
    reply_to_message_id: int | None
    consecutive_failures: int
    consecutive_not_found: int


_WATCH_COLS = (
    "id, source, listing_id, pending_ref, requested_category, target_price_minor, target_set_at,"
    " reply_to_message_id, consecutive_failures, consecutive_not_found"
)


def _watch(row: tuple[Any, ...]) -> WatchRow:
    return WatchRow(
        int(row[0]), row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]
    )


def get_watch(conn: psycopg.Connection, watch_id: int) -> WatchRow | None:
    row = _one(conn, f"SELECT {_WATCH_COLS} FROM discountz.watch WHERE id = %s", (watch_id,))
    return None if row is None else _watch(row)


def watch_for_listing(conn: psycopg.Connection, listing_id: int) -> WatchRow | None:
    row = _one(
        conn, f"SELECT {_WATCH_COLS} FROM discountz.watch WHERE listing_id = %s", (listing_id,)
    )
    return None if row is None else _watch(row)


def record_watch_check(
    conn: psycopg.Connection,
    watch_id: int,
    now: datetime,
    next_at: datetime,
    *,
    failures: int,
    not_found: int,
    gone: bool = False,
) -> None:
    """Reprograma el seguimiento; `gone` solo aplica a watches ya resueltos."""
    conn.execute(
        """UPDATE discountz.watch
              SET last_checked_at = %s, next_check_at = %s, consecutive_failures = %s,
                  consecutive_not_found = %s,
                  status = CASE WHEN %s AND listing_id IS NOT NULL THEN 'gone' ELSE status END
            WHERE id = %s""",
        (now, next_at, min(failures, 1000), min(not_found, 1000), gone, watch_id),
    )


def delete_watch(conn: psycopg.Connection, watch_id: int) -> None:
    conn.execute("DELETE FROM discountz.watch WHERE id = %s", (watch_id,))


def mark_listing_gone(conn: psycopg.Connection, listing_id: int, now: datetime) -> None:
    conn.execute("UPDATE discountz.listing SET gone_at = %s WHERE id = %s", (now, listing_id))


def listing_refs(conn: psycopg.Connection, listing_ids: Sequence[int]) -> dict[int, ListingRef]:
    rows = conn.execute(
        "SELECT id, source, external_id FROM discountz.listing WHERE id = ANY(%s)",
        (list(listing_ids),),
    ).fetchall()
    return {int(r[0]): ListingRef(r[1], r[2]) for r in rows}


def prev_list_price(conn: psycopg.Connection, source: str, external_id: str) -> int | None:
    row = _one(
        conn,
        """SELECT last_list_price_minor FROM discountz.listing
            WHERE source = %s AND external_id = %s""",
        (source, external_id),
    )
    return None if row is None or row[0] is None else int(row[0])


def global_muted_until(conn: psycopg.Connection) -> datetime | None:
    row = _one(conn, "SELECT muted_until FROM discountz.bot_state WHERE id = 1")
    return None if row is None else row[0]


def pending_confirmation_at(conn: psycopg.Connection, listing_id: int) -> datetime | None:
    row = _one(
        conn, "SELECT pending_confirmation_at FROM discountz.listing WHERE id = %s", (listing_id,)
    )
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class SearchRow:
    id: int
    spec: SearchSpec
    last_result_count: int | None
    consecutive_failures: int
    consecutive_empty: int


def get_search(conn: psycopg.Connection, search_id: int) -> SearchRow | None:
    row = _one(
        conn,
        """SELECT id, source, query, exclude_terms, min_price_minor, max_price_minor, category,
                  last_result_count, consecutive_failures, consecutive_empty
             FROM discountz.search WHERE id = %s AND enabled""",
        (search_id,),
    )
    if row is None:
        return None
    spec = SearchSpec(row[1], row[2], tuple(row[3]), row[4], row[5], row[6])
    return SearchRow(int(row[0]), spec, row[7], int(row[8]), int(row[9]))


def record_search_run(
    conn: psycopg.Connection,
    search_id: int,
    now: datetime,
    next_at: datetime,
    *,
    result_count: int | None,
) -> None:
    """`result_count=None` = la búsqueda falló. Un 0 tras resultados previos cuenta como vacío."""
    conn.execute(
        """UPDATE discountz.search
              SET last_run_at = %(now)s, next_run_at = %(next)s,
                  consecutive_failures = CASE WHEN %(n)s::int IS NULL
                                              THEN consecutive_failures + 1 ELSE 0 END,
                  consecutive_empty = CASE
                      WHEN %(n)s::int IS NULL THEN consecutive_empty
                      WHEN %(n)s::int = 0 AND coalesce(last_result_count, 0) > 0
                           OR %(n)s::int = 0 AND consecutive_empty > 0
                      THEN consecutive_empty + 1
                      ELSE 0 END,
                  last_result_count = coalesce(least(%(n)s::int, 32767), last_result_count)
            WHERE id = %(id)s""",
        {"now": now, "next": next_at, "n": result_count, "id": search_id},
    )


def max_consecutive_empty(conn: psycopg.Connection, source: str) -> int:
    row = _one(
        conn,
        "SELECT coalesce(max(consecutive_empty), 0) FROM discountz.search WHERE source = %s",
        (source,),
    )
    return int(_required(row)[0])


def last_alert(conn: psycopg.Connection, listing_id: int) -> AlertRecord | None:
    row = _one(
        conn,
        """SELECT kind, price_minor, reference_minor, created_at, segment_started_at, delivery
             FROM discountz.alert WHERE listing_id = %s AND delivery <> 'suppressed'
            ORDER BY created_at DESC, id DESC LIMIT 1""",
        (listing_id,),
    )
    if row is None:
        return None
    return AlertRecord(AlertKind(row[0]), int(row[1]), row[2], row[3], row[4], row[5])


@dataclass(frozen=True, slots=True)
class UnqueuedAlert:
    id: int
    listing_id: int
    watch_id: int | None
    kind: AlertKind
    price_minor: int
    reference_minor: int | None
    real_saving_bp: int | None
    advertised_saving_bp: int | None
    flags: tuple[str, ...]
    observed_at: datetime
    source: str
    title: str
    url: str
    category: str
    currency: Currency
    list_price_minor: int | None


def unqueued_alerts(conn: psycopg.Connection) -> list[UnqueuedAlert]:
    """Alertas `new` sin mensaje en el outbox (incluye las de un ciclo que murió)."""
    rows = conn.execute(
        """SELECT a.id, a.listing_id, coalesce(a.watch_id, w.id), a.kind, a.price_minor,
                  a.reference_minor, a.real_saving_bp, a.advertised_saving_bp, a.flags,
                  a.observed_at, l.source, l.title, l.canonical_url, l.category, s.currency,
                  l.last_list_price_minor
             FROM discountz.alert a
             JOIN discountz.listing l ON l.id = a.listing_id
             JOIN discountz.source s ON s.code = l.source
             LEFT JOIN discountz.watch w ON w.listing_id = a.listing_id
            WHERE a.delivery = 'new'
            ORDER BY a.id"""
    ).fetchall()
    return [
        UnqueuedAlert(
            int(r[0]),
            int(r[1]),
            r[2],
            AlertKind(r[3]),
            int(r[4]),
            r[5],
            r[6],
            r[7],
            tuple(r[8]),
            r[9],
            r[10],
            r[11],
            r[12],
            r[13],
            Currency(r[14].strip()),
            r[15],
        )
        for r in rows
    ]


def mark_alerts_queued(conn: psycopg.Connection, alert_ids: Sequence[int], outbox_id: int) -> None:
    conn.execute(
        """UPDATE discountz.alert SET delivery = 'queued', outbox_id = %s
            WHERE id = ANY(%s) AND delivery = 'new'""",
        (outbox_id, list(alert_ids)),
    )


def release_outbox(conn: psycopg.Connection, outbox_ids: Sequence[int]) -> None:
    """Devuelve a `pending` mensajes reclamados que no se intentaron (sin sumar intentos)."""
    conn.execute(
        """UPDATE discountz.outbox SET status = 'pending', sending_since = NULL
            WHERE id = ANY(%s) AND status = 'sending'""",
        (list(outbox_ids),),
    )


def record_canary(conn: psycopg.Connection, source: str, ok: bool, now: datetime) -> None:
    conn.execute(
        "UPDATE discountz.source SET canary_checked_at = %s, canary_ok = %s WHERE code = %s",
        (now, ok, source),
    )


@dataclass(frozen=True, slots=True)
class SourceHealthRow:
    health: str
    notified_health: str
    canary_ok: bool | None


def source_health(conn: psycopg.Connection, source: str) -> SourceHealthRow:
    row = _required(
        _one(
            conn,
            "SELECT health, notified_health, canary_ok FROM discountz.source WHERE code = %s",
            (source,),
        )
    )
    return SourceHealthRow(row[0], row[1], row[2])


def save_health(
    conn: psycopg.Connection, source: str, health: str, now: datetime, *, notified: bool
) -> None:
    conn.execute(
        """UPDATE discountz.source
              SET health_since = CASE WHEN health <> %(h)s THEN %(now)s ELSE health_since END,
                  health = %(h)s,
                  notified_health = CASE WHEN %(notified)s THEN %(h)s ELSE notified_health END,
                  last_notice_at = CASE WHEN %(notified)s THEN %(now)s ELSE last_notice_at END,
                  last_success_at = CASE WHEN %(h)s = 'healthy' THEN %(now)s
                                         ELSE last_success_at END
            WHERE code = %(code)s""",
        {"h": health, "now": now, "notified": notified, "code": source},
    )


def recent_run_statuses(conn: psycopg.Connection, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT status FROM discountz.run ORDER BY started_at DESC, id DESC LIMIT %s", (n,)
    ).fetchall()
    return [str(r[0]) for r in rows]


# ───────────── Mantenimiento (spec §2.4)

BATCH = 5000


def _delete_batched(conn: psycopg.Connection, table: str, where: str, params: Any) -> int:
    """Borra por lotes de 5.000 filas (cada lote en su transacción) y devuelve el total."""
    total = 0
    while True:
        with conn.transaction():
            cur = conn.execute(
                f"""DELETE FROM discountz.{table} WHERE ctid IN (
                        SELECT ctid FROM discountz.{table} WHERE {where} LIMIT {BATCH})""",
                params,
            )
        total += cur.rowcount
        if cur.rowcount < BATCH:
            return total


def keep_alltime_min(conn: psycopg.Connection, cutoff: datetime, flash_max: timedelta) -> int:
    """Lleva a `listing.alltime_min_*` el mínimo de los segmentos que van a salir.

    Excluye `s`/`g`, los sin stock y los relámpagos (duración observada < `flash_max`).
    """
    cur = conn.execute(
        """WITH outgoing AS (
               SELECT DISTINCT ON (listing_id) listing_id, price_minor, started_at
                 FROM discountz.price_segment
                WHERE ended_at < %(cutoff)s AND status = 'o' AND in_stock
                  AND price_minor IS NOT NULL AND last_seen_at - started_at >= %(flash)s
                ORDER BY listing_id, price_minor, started_at)
           UPDATE discountz.listing l
              SET alltime_min_minor = o.price_minor, alltime_min_at = o.started_at
             FROM outgoing o
            WHERE l.id = o.listing_id
              AND (l.alltime_min_minor IS NULL OR o.price_minor < l.alltime_min_minor)""",
        {"cutoff": cutoff, "flash": flash_max},
    )
    return cur.rowcount


def delete_old_segments(conn: psycopg.Connection, cutoff: datetime) -> int:
    return _delete_batched(conn, "price_segment", "ended_at < %(cutoff)s", {"cutoff": cutoff})


def reclassify_glitches(conn: psycopg.Connection, before: datetime) -> int:
    """`g` con más de 7 d pasa a `o`: un «error» que dura una semana es el precio real."""
    cur = conn.execute(
        "UPDATE discountz.price_segment SET status = 'o' WHERE status = 'g' AND started_at < %s",
        (before,),
    )
    return cur.rowcount


def apply_retention(conn: psycopg.Connection, now: datetime) -> int:
    total = _delete_batched(
        conn, "run", "started_at < %(t)s AND status <> 'running'", {"t": now - timedelta(days=30)}
    )
    total += _delete_batched(
        conn,
        "outbox",
        "created_at < %(t)s AND status <> 'sending'",  # a los 30 d todo TTL ya venció
        {"t": now - timedelta(days=30)},
    )
    total += _delete_batched(
        conn, "source_hourly_stat", "hour_start < %(t)s", {"t": now - timedelta(days=90)}
    )
    total += _delete_batched(conn, "alert", "created_at < %(t)s", {"t": now - timedelta(days=400)})
    return total


def prune_discoveries(
    conn: psycopg.Connection, not_seen_since: datetime, alerts_since: datetime | None
) -> int:
    """Listings descubiertos (sin seguimiento) sin verse desde `not_seen_since` y sin alertas
    (desde `alerts_since`, o nunca si es `None`); sus segmentos caen en cascada."""
    alert_cond = "" if alerts_since is None else "AND a.created_at >= %(alerts)s"
    where = f"""last_seen_at < %(seen)s
        AND NOT EXISTS (SELECT 1 FROM discountz.watch w WHERE w.listing_id = listing.id)
        AND NOT EXISTS (SELECT 1 FROM discountz.alert a WHERE a.listing_id = listing.id
                        {alert_cond})"""
    return _delete_batched(conn, "listing", where, {"seen": not_seen_since, "alerts": alerts_since})


def prune_unwatched_segments(conn: psycopg.Connection, before: datetime) -> int:
    where = """ended_at < %(t)s AND NOT EXISTS (
        SELECT 1 FROM discountz.watch w WHERE w.listing_id = price_segment.listing_id)"""
    return _delete_batched(conn, "price_segment", where, {"t": before})


def db_size(conn: psycopg.Connection) -> int:
    return int(_required(_one(conn, "SELECT pg_database_size(current_database())"))[0])


def top_tables(conn: psycopg.Connection, n: int = 5) -> list[tuple[str, int]]:
    rows = conn.execute(
        """SELECT c.relname, pg_total_relation_size(c.oid)
             FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
            WHERE ns.nspname = 'discountz' AND c.relkind = 'r'
            ORDER BY 2 DESC LIMIT %s""",
        (n,),
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


@dataclass(frozen=True, slots=True)
class BotState:
    last_maintenance_on: date | None
    last_summary_on: date | None
    discovery_paused: bool
    dispatch_token_expires_on: date | None


def bot_state(conn: psycopg.Connection) -> BotState:
    row = _required(
        _one(
            conn,
            """SELECT last_maintenance_on, last_summary_on, discovery_paused,
                      dispatch_token_expires_on FROM discountz.bot_state WHERE id = 1""",
        )
    )
    return BotState(row[0], row[1], bool(row[2]), row[3])


def set_maintenance_state(
    conn: psycopg.Connection, day: date, size_bytes: int, discovery_paused: bool
) -> None:
    conn.execute(
        """UPDATE discountz.bot_state
              SET last_maintenance_on = %s, last_db_size_bytes = %s, discovery_paused = %s
            WHERE id = 1""",
        (day, size_bytes, discovery_paused),
    )


def set_summary_on(conn: psycopg.Connection, day: date) -> None:
    conn.execute("UPDATE discountz.bot_state SET last_summary_on = %s WHERE id = 1", (day,))


@dataclass(frozen=True, slots=True)
class DayActivity:
    by_trigger: dict[str, int]
    last_cycle_at: datetime | None
    alerts: int
    sources: list[tuple[str, str, int]]  # (código, salud, bloqueos 24 h)


def day_activity(conn: psycopg.Connection, now: datetime) -> DayActivity:
    since = now - timedelta(hours=24)
    trig = conn.execute(
        """SELECT trigger_kind, count(*) FROM discountz.run
            WHERE started_at > %s AND status IN ('ok','partial') GROUP BY 1""",
        (since,),
    ).fetchall()
    last = _one(
        conn,
        "SELECT max(started_at) FROM discountz.run WHERE status IN ('ok','partial')",
    )
    alerts = _required(
        _one(
            conn,
            """SELECT count(*) FROM discountz.alert
                WHERE created_at > %s AND delivery IN ('new','queued')""",
            (since,),
        )
    )
    sources = conn.execute(
        """SELECT s.code, s.health, coalesce(sum(h.blocked), 0)
             FROM discountz.source s
             LEFT JOIN discountz.source_hourly_stat h
               ON h.source = s.code AND h.hour_start > %s
            WHERE s.enabled GROUP BY s.code, s.health ORDER BY s.code""",
        (since,),
    ).fetchall()
    return DayActivity(
        {str(r[0]): int(r[1]) for r in trig},
        None if last is None else last[0],
        int(alerts[0]),
        [(str(r[0]), str(r[1]), int(r[2])) for r in sources],
    )


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


# ───────────── Catálogo (spec del rastreo §7.1)

_CAT_COLS = (
    "price_minor, usual_minor, min_seen_minor, first_seen_at, stable_since, reads_at_price,"
    " last_changed_at, last_alert_at, last_alert_minor"
)


@dataclass(frozen=True, slots=True)
class CatalogRow:
    external_id: str
    ean: str | None
    topic: str
    currency: str
    state: CatalogState
    price_changed: bool
    model_key: str | None = None  # `epid` de eBay (ADR 0008)


@dataclass(frozen=True, slots=True)
class CrawlCursor:
    source: str
    sweep_no: int
    partition_idx: int
    page: int
    sweep_started_at: datetime | None
    last_sweep_s: int | None
    last_run_at: datetime | None
    changed_since: datetime | None
    partitions: tuple[str, ...] = ()  # particiones de la vuelta en curso (con las ya partidas)
    day: date | None = None  # día UTC de `day_requests`
    day_requests: int = 0  # peticiones del rastreo en ese día (tope de 24 h)


def load_catalog_states(
    conn: psycopg.Connection, source: str, external_ids: Sequence[str]
) -> dict[str, CatalogState]:
    rows = conn.execute(
        f"""SELECT external_id, {_CAT_COLS} FROM discountz.catalog_item
             WHERE source = %s AND external_id = ANY(%s)""",
        (source, list(external_ids)),
    ).fetchall()
    return {r[0]: CatalogState(*r[1:]) for r in rows}


def save_catalog_page(
    conn: psycopg.Connection, source: str, rows: Sequence[CatalogRow], now: datetime
) -> None:
    """Guarda una página en lote y registra los cambios de precio (una transacción)."""
    if not rows:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO discountz.catalog_item (source, external_id, ean, topic, currency,
                    {_CAT_COLS}, last_seen_at, model_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, external_id) DO UPDATE SET
                    ean = coalesce(EXCLUDED.ean, catalog_item.ean), topic = EXCLUDED.topic,
                    model_key = coalesce(EXCLUDED.model_key, catalog_item.model_key),
                    price_minor = EXCLUDED.price_minor, usual_minor = EXCLUDED.usual_minor,
                    min_seen_minor = EXCLUDED.min_seen_minor,
                    stable_since = EXCLUDED.stable_since,
                    reads_at_price = EXCLUDED.reads_at_price,
                    last_changed_at = EXCLUDED.last_changed_at,
                    last_seen_at = greatest(catalog_item.last_seen_at, EXCLUDED.last_seen_at)""",
            [
                (
                    source,
                    r.external_id,
                    r.ean,
                    r.topic,
                    r.currency,
                    r.state.price_minor,
                    r.state.usual_minor,
                    r.state.min_seen_minor,
                    r.state.first_seen_at,
                    r.state.stable_since,
                    r.state.reads_at_price,
                    r.state.last_changed_at,
                    r.state.last_alert_at,
                    r.state.last_alert_minor,
                    now,
                    r.model_key,
                )
                for r in rows
            ],
        )
        changed = [(r.external_id, r.state.price_minor) for r in rows if r.price_changed]
        if changed:
            cur.executemany(
                """INSERT INTO discountz.catalog_price_change (item_id, at, price_minor)
                   SELECT id, %s, %s FROM discountz.catalog_item
                    WHERE source = %s AND external_id = %s
                   ON CONFLICT DO NOTHING""",
                [(now, price, source, ext) for ext, price in changed],
            )


def model_peer_prices(
    conn: psycopg.Connection,
    source: str,
    keys: Sequence[str],
    since: datetime,
    per_key: int = 200,
) -> dict[str, list[tuple[str, int]]]:
    """Precios de las publicaciones de cada modelo vistas desde `since` (las más recientes)."""
    if not keys:
        return {}
    rows = conn.execute(
        """SELECT model_key, external_id, price_minor FROM (
               SELECT model_key, external_id, price_minor,
                      row_number() OVER (PARTITION BY model_key ORDER BY last_seen_at DESC) AS n
                 FROM discountz.catalog_item
                WHERE source = %s AND model_key = ANY(%s) AND last_seen_at >= %s) t
            WHERE n <= %s""",
        (source, list(keys), since, per_key),
    ).fetchall()
    out: dict[str, list[tuple[str, int]]] = {}
    for key, ext, price in rows:
        out.setdefault(key, []).append((ext, int(price)))
    return out


def count_catalog_items(conn: psycopg.Connection, source: str) -> int:
    row = _required(
        _one(conn, "SELECT count(*) FROM discountz.catalog_item WHERE source = %s", (source,))
    )
    return int(row[0])


def recent_alert_for_model(
    conn: psycopg.Connection,
    source: str,
    model_key: str,
    price_minor: int,
    since: datetime,
    exclude_external_id: str,
) -> bool:
    """¿Otra publicación del mismo modelo alertó a ±1 % desde `since`? (spec de eBay §4.2)."""
    row = _one(
        conn,
        """SELECT 1 FROM discountz.catalog_item
            WHERE source = %s AND model_key = %s AND external_id <> %s
              AND last_alert_at >= %s
              AND abs(last_alert_minor - %s) * 100 <= last_alert_minor
            LIMIT 1""",
        (source, model_key, exclude_external_id, since, price_minor),
    )
    return row is not None


def hot_catalog_ids(
    conn: psycopg.Connection, source: str, since: datetime, limit: int
) -> list[str]:
    rows = conn.execute(
        """SELECT external_id FROM discountz.catalog_item
            WHERE source = %s AND last_changed_at >= %s
            ORDER BY last_changed_at DESC LIMIT %s""",
        (source, since, limit),
    ).fetchall()
    return [r[0] for r in rows]


def get_cursor(conn: psycopg.Connection, source: str) -> CrawlCursor:
    row = _one(
        conn,
        """SELECT source, sweep_no, partition_idx, page, sweep_started_at, last_sweep_s,
                  last_run_at, changed_since, partitions, day_start, day_requests
             FROM discountz.crawl_cursor WHERE source = %s""",
        (source,),
    )
    if row is None:
        return CrawlCursor(source, 0, 0, 0, None, None, None, None)
    return CrawlCursor(
        row[0],
        int(row[1]),
        int(row[2]),
        int(row[3]),
        row[4],
        row[5],
        row[6],
        row[7],
        partitions=tuple(str(p) for p in row[8]),
        day=row[9],
        day_requests=int(row[10]),
    )


def save_cursor(conn: psycopg.Connection, c: CrawlCursor) -> None:
    conn.execute(
        """INSERT INTO discountz.crawl_cursor (source, sweep_no, partition_idx, page,
               sweep_started_at, last_sweep_s, last_run_at, changed_since, partitions,
               day_start, day_requests)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (source) DO UPDATE SET sweep_no = EXCLUDED.sweep_no,
               partition_idx = EXCLUDED.partition_idx, page = EXCLUDED.page,
               sweep_started_at = EXCLUDED.sweep_started_at,
               last_sweep_s = EXCLUDED.last_sweep_s, last_run_at = EXCLUDED.last_run_at,
               changed_since = EXCLUDED.changed_since, partitions = EXCLUDED.partitions,
               day_start = EXCLUDED.day_start, day_requests = EXCLUDED.day_requests""",
        (
            c.source,
            c.sweep_no,
            c.partition_idx,
            c.page,
            c.sweep_started_at,
            c.last_sweep_s,
            c.last_run_at,
            c.changed_since,
            Jsonb(list(c.partitions)),
            c.day,
            c.day_requests,
        ),
    )


def acquire_crawl_lease(
    conn: psycopg.Connection, source: str, holder: str, now: datetime, ttl: timedelta
) -> bool:
    row = _one(
        conn,
        """INSERT INTO discountz.crawl_lease (source, holder, expires_at) VALUES (%s, %s, %s)
           ON CONFLICT (source) DO UPDATE SET holder = EXCLUDED.holder,
               expires_at = EXCLUDED.expires_at
            WHERE crawl_lease.expires_at < %s OR crawl_lease.holder = EXCLUDED.holder
           RETURNING holder""",
        (source, holder, now + ttl, now),
    )
    return row is not None


def release_crawl_lease(conn: psycopg.Connection, source: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM discountz.crawl_lease WHERE source = %s AND holder = %s", (source, holder)
    )


def recent_alert_for_ean(
    conn: psycopg.Connection,
    ean: str,
    currency: str,
    price_minor: int,
    since: datetime,
    *,
    exclude_source: str,
) -> bool:
    """¿Otra tienda alertó el mismo EAN a ±1 % desde `since`? (duplicados, spec §3.2)."""
    row = _one(
        conn,
        """SELECT 1 FROM discountz.catalog_item
            WHERE ean = %s AND currency = %s AND source <> %s AND last_alert_at >= %s
              AND abs(last_alert_minor - %s) * 100 <= last_alert_minor
            LIMIT 1""",
        (ean, currency, exclude_source, since, price_minor),
    )
    return row is not None


def mark_catalog_alert(
    conn: psycopg.Connection, source: str, external_id: str, price_minor: int, now: datetime
) -> None:
    conn.execute(
        """UPDATE discountz.catalog_item SET last_alert_at = %s, last_alert_minor = %s
            WHERE source = %s AND external_id = %s""",
        (now, price_minor, source, external_id),
    )


def topic_threads(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT code, thread_id FROM discountz.topic WHERE thread_id IS NOT NULL"
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def forget_topic_thread(conn: psycopg.Connection, code: str) -> None:
    conn.execute("UPDATE discountz.topic SET thread_id = NULL WHERE code = %s", (code,))


def save_topic(conn: psycopg.Connection, code: str, title: str, thread_id: int | None) -> None:
    conn.execute(
        """INSERT INTO discountz.topic (code, title, thread_id) VALUES (%s, %s, %s)
           ON CONFLICT (code) DO UPDATE
              SET title = EXCLUDED.title, thread_id = EXCLUDED.thread_id""",
        (code, title, thread_id),
    )


# ───────────── Catálogo: retención y resumen diario (spec del rastreo §7.2 y §8)


def delete_old_catalog_changes(conn: psycopg.Connection, cutoff: datetime) -> int:
    return conn.execute(
        "DELETE FROM discountz.catalog_price_change WHERE at < %s", (cutoff,)
    ).rowcount


def delete_unseen_catalog_items(conn: psycopg.Connection, cutoff: datetime) -> int:
    """Productos que el rastreo dejó de ver (sin stock o retirados); sus cambios caen en cascada."""
    return conn.execute(
        "DELETE FROM discountz.catalog_item WHERE last_seen_at < %s", (cutoff,)
    ).rowcount


@dataclass(frozen=True, slots=True)
class CatalogDaily:
    items_by_source: Mapping[str, int]
    changes_today: int
    alerts_by_topic: Mapping[str, int]
    last_sweep_s_by_source: Mapping[str, int]
    channel_posts: int = 0  # ofertas de canales de las últimas 24 h, sin repetidas ni marcadas
    channel_duplicates: int = 0


def catalog_daily_stats(conn: psycopg.Connection, now: datetime) -> CatalogDaily:
    since = now - timedelta(hours=24)
    items = conn.execute(
        "SELECT source, count(*) FROM discountz.catalog_item GROUP BY source"
    ).fetchall()
    changes = _one(
        conn, "SELECT count(*) FROM discountz.catalog_price_change WHERE at >= %s", (since,)
    )
    alerts = conn.execute(
        """SELECT topic, count(*) FROM discountz.outbox
            WHERE purpose IN ('alert', 'digest') AND topic IS NOT NULL AND created_at >= %s
            GROUP BY topic""",
        (since,),
    ).fetchall()
    sweeps = conn.execute(
        "SELECT source, last_sweep_s FROM discountz.crawl_cursor WHERE last_sweep_s IS NOT NULL"
    ).fetchall()
    channels = _required(
        _one(
            conn,
            """SELECT count(*) FILTER (WHERE duplicate_of IS NULL AND NOT baseline),
                      count(*) FILTER (WHERE duplicate_of IS NOT NULL)
                 FROM discountz.channel_post WHERE seen_at >= %s""",
            (since,),
        )
    )
    return CatalogDaily(
        items_by_source={r[0]: int(r[1]) for r in items},
        changes_today=int(_required(changes)[0]),
        alerts_by_topic={r[0]: int(r[1]) for r in alerts},
        last_sweep_s_by_source={r[0]: int(r[1]) for r in sweeps},
        channel_posts=int(channels[0]),
        channel_duplicates=int(channels[1]),
    )


# ───────────── Canales de Telegram (spec de canales §6)


@dataclass(frozen=True, slots=True)
class ChannelCursor:
    last_id: int
    updated_at: datetime
    unparsed_streak: int = 0  # publicaciones con precio y sin oferta desde la última oferta


@dataclass(frozen=True, slots=True)
class RecentPost:
    key: str  # «<código>:<post_id>»
    title_norm: str
    price_minor: int
    currency: str
    coupon: str | None


def get_channel_cursor(conn: psycopg.Connection, code: str) -> ChannelCursor | None:
    row = _one(
        conn,
        """SELECT last_id, updated_at, unparsed_streak
             FROM discountz.channel_cursor WHERE channel = %s""",
        (code,),
    )
    return None if row is None else ChannelCursor(int(row[0]), row[1], int(row[2]))


def save_channel_cursor(
    conn: psycopg.Connection,
    code: str,
    last_id: int,
    now: datetime,
    *,
    unparsed_streak: int | None = None,
) -> None:
    """Guarda el cursor; `updated_at` marca la última lectura aunque el id no cambie.

    `unparsed_streak=None` conserva la racha guardada.
    """
    conn.execute(
        """INSERT INTO discountz.channel_cursor (channel, last_id, updated_at, unparsed_streak)
           VALUES (%s, %s, %s, coalesce(%s, 0))
           ON CONFLICT (channel) DO UPDATE SET
               last_id = greatest(channel_cursor.last_id, EXCLUDED.last_id),
               updated_at = EXCLUDED.updated_at,
               unparsed_streak = coalesce(%s, channel_cursor.unparsed_streak)""",
        (code, last_id, now, unparsed_streak, unparsed_streak),
    )


def recent_channel_posts(conn: psycopg.Connection, since: datetime) -> list[RecentPost]:
    """Publicaciones enviadas desde `since` (ni los duplicados ni las marcadas cuentan)."""
    rows = conn.execute(
        """SELECT channel || ':' || post_id, title_norm, price_minor, currency, coupon
             FROM discountz.channel_post
            WHERE seen_at >= %s AND duplicate_of IS NULL AND NOT baseline
            ORDER BY seen_at, channel, post_id""",
        (since,),
    ).fetchall()
    return [RecentPost(r[0], r[1], int(r[2]), r[3], r[4]) for r in rows]


def channel_post_ids(conn: psycopg.Connection, code: str, post_ids: Sequence[int]) -> set[int]:
    """Los ids de `post_ids` que ya se guardaron para el canal (feeds sin orden de ids)."""
    if not post_ids:
        return set()
    rows = conn.execute(
        """SELECT post_id FROM discountz.channel_post
            WHERE channel = %s AND post_id = ANY(%s)""",
        (code, list(post_ids)),
    ).fetchall()
    return {int(r[0]) for r in rows}


def insert_channel_post(
    conn: psycopg.Connection,
    code: str,
    post_id: int,
    *,
    published_at: datetime,
    title_norm: str,
    price_minor: int,
    currency: str,
    coupon: str | None,
    topic: str,
    duplicate_of: str | None,
    now: datetime,
    baseline: bool = False,
) -> bool:
    """Registra la publicación; `False` si ya estaba (relectura de una página).

    `baseline`: vista en la primera lectura de un feed sin orden de ids y nunca enviada.
    """
    cur = conn.execute(
        """INSERT INTO discountz.channel_post (channel, post_id, published_at, title_norm,
               price_minor, currency, coupon, topic, duplicate_of, seen_at, baseline)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (channel, post_id) DO NOTHING""",
        (
            code,
            post_id,
            published_at,
            title_norm,
            price_minor,
            currency,
            coupon,
            topic,
            duplicate_of,
            now,
            baseline,
        ),
    )
    return cur.rowcount == 1


def delete_old_channel_posts(conn: psycopg.Connection, before: datetime) -> int:
    cur = conn.execute("DELETE FROM discountz.channel_post WHERE seen_at < %s", (before,))
    return cur.rowcount
