"""Mantenimiento diario y resumen diario (spec §2.4, §1.3 paso 14 y §5.5).

El mantenimiento corre en el primer ciclo después de las 03:00 de Bogotá y el resumen en el
primero después de las 08:00. Los canarios se leen dentro del plan (tarea `canary`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import psycopg

from discountz.app.render import DailyView, local, render_daily, short_date
from discountz.config import AppParams
from discountz.infra import db

MB = 1024 * 1024
WARN_BYTES = 300 * MB
PRUNE_BYTES = 400 * MB
PAUSE_BYTES = 480 * MB  # último recurso (ADR 0021): antes van los escalones de limpieza
SEGMENT_KEEP = timedelta(days=400)
GLITCH_RECLASS = timedelta(days=7)
CATALOG_UNSEEN = timedelta(days=30)
CATALOG_UNSEEN_TIGHT = timedelta(days=14)  # desde PRUNE_BYTES
CHANNEL_POSTS_KEEP = timedelta(days=7)  # ventana de duplicados (24 h) más margen para el resumen
OFFER_LOG_KEEP = timedelta(days=7)  # tope del día, promociones de 24 h y resumen de la noche
EAN_LOOKUP_KEEP = timedelta(days=30)  # un EAN buscado no se repite en ese plazo (ADR 0018)
MAINTENANCE_AT = time(3, 0)
SUMMARY_AT = time(8, 0)
TOKEN_WARN_DAYS = 14
SYSTEM_TTL = timedelta(hours=48)
# Egress (ADR 0020): el plan gratis da 5 GB al mes; 170 bytes por fila salió de 11,48 GB del
# ciclo 28-oct/21-nov entre las filas de los SELECT del rol (`pg_stat_statements`).
EGRESS_QUOTA_MB = 5 * 1024
BYTES_PER_ROW = 170
EGRESS_MIN_SPAN = timedelta(hours=12)
# Presupuesto mensual (ADR 0022): el 75 % del plan para todo lo que devuelve la BD; el 25 %
# restante cubre el ciclo de cada 2 min con el rastreo apagado. Supabase reinicia el día 28.
EGRESS_BUDGET_MB = EGRESS_QUOTA_MB * 3 // 4
EGRESS_CYCLE_DAY = 28
PACE_MIN_BP = 1000
_HEALTH = {"healthy": "sana", "degraded": "degradada", "blocked": "bloqueada", "broken": "rota"}


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    segments_deleted: int
    glitches_reclassified: int
    retention_deleted: int
    listings_pruned: int
    segments_pruned: int
    db_bytes: int
    discovery_paused: bool
    egress_mb: int | None = None  # MB al día estimados desde el mantenimiento anterior
    egress_cycle_mb: int | None = None  # MB estimados del ciclo de facturación (ADR 0022)


def egress_mb_per_day(
    prev: int | None, prev_at: datetime | None, total: int, now: datetime
) -> int | None:
    """MB al día desde la lectura anterior; `None` sin ella, con las estadísticas reiniciadas
    (el total bajó) o con menos de 12 h entre lecturas."""
    if prev is None or prev_at is None or total < prev or now - prev_at < EGRESS_MIN_SPAN:
        return None
    per_day = (total - prev) * BYTES_PER_ROW * 86_400 / (now - prev_at).total_seconds()
    return int(per_day // MB)


def egress_too_high(mb_day: int) -> bool:
    """¿A este ritmo un mes de 31 días pasa del 80 % del plan gratis?"""
    return mb_day * 31 * 10 >= EGRESS_QUOTA_MB * 8


def billing_cycle_start(day: date) -> date:
    """Día 28 en que empezó el ciclo de facturación de Supabase que contiene `day`."""
    if day.day >= EGRESS_CYCLE_DAY:
        return day.replace(day=EGRESS_CYCLE_DAY)
    if day.month == 1:
        return date(day.year - 1, 12, EGRESS_CYCLE_DAY)
    return date(day.year, day.month - 1, EGRESS_CYCLE_DAY)


def _next_billing_cycle(day: date) -> date:
    start = billing_cycle_start(day)
    if start.month == 12:
        return date(start.year + 1, 1, EGRESS_CYCLE_DAY)
    return date(start.year, start.month + 1, EGRESS_CYCLE_DAY)


def cycle_target_mb(used_mb: int, day: date) -> int:
    """MB al día que caben en lo que queda del presupuesto, nunca más que un reparto parejo."""
    days_left = max(1, (_next_billing_cycle(day) - day).days)
    return min(EGRESS_BUDGET_MB // 31, max(0, EGRESS_BUDGET_MB - used_mb) // days_left)


def next_pace(pace_bp: int, mb_day: int | None, target_mb: int) -> int:
    """Ritmo del rastreo para mañana: lo lleva hacia la meta en proporción; 0 sin presupuesto.
    Desde 0 (ciclo nuevo) vuelve a arrancar en el piso."""
    if target_mb <= 0:
        return 0
    pace_bp = max(pace_bp, PACE_MIN_BP)
    if mb_day is None:
        return pace_bp
    return max(PACE_MIN_BP, min(10_000, pace_bp * target_mb // max(mb_day, 1)))


def _measure_egress(conn: psycopg.Connection, now: datetime) -> tuple[int | None, int | None]:
    """MB al día y MB del ciclo de facturación; ajusta el ritmo del rastreo al presupuesto del
    mes (ADR 0022) y lo apaga al agotarlo."""
    total = db.select_rows_total(conn)
    if total is None:
        return None, None
    prev, prev_at = db.egress_mark(conn)
    mb_day = egress_mb_per_day(prev, prev_at, total, now)
    today = local(now).date()
    start = billing_cycle_start(today)
    added = total - prev if prev is not None and total >= prev else 0  # estadísticas reiniciadas
    cycle_on, cycle_rows = db.egress_cycle(conn)
    rows = cycle_rows + added if cycle_on == start else added
    used_mb = rows * BYTES_PER_ROW // MB
    target = cycle_target_mb(used_mb, today)
    with conn.transaction():
        pace = next_pace(db.bot_state(conn).crawl_pace_bp, mb_day, target)
        if mb_day is not None and egress_too_high(mb_day):
            month = mb_day * 31 / 1024
            _notice(
                conn,
                f"⚠️ Tráfico de la BD alto: ~{mb_day} MB al día (~{month:.1f} GB al mes; el plan "
                f"gratis de Supabase da 5) y van ~{used_mb / 1024:.1f} GB del ciclo. Bajo el "
                f"rastreo al {pace // 100} % de su ritmo; vuelve a subir solo cuando el tráfico "
                "baje.",
                f"egress:{today.isoformat()}",
                now,
                0,
            )
        if target == 0:
            back = short_date(now + timedelta(days=(_next_billing_cycle(today) - today).days))
            _notice(
                conn,
                f"⛔ El ciclo de Supabase va en ~{used_mb / 1024:.1f} GB de egress (presupuesto "
                f"{EGRESS_BUDGET_MB / 1024:.2f} de 5): apago el rastreo hasta el {back}. Siguen "
                "los canales, los seguimientos y las búsquedas.",
                f"egress-cut:{start.isoformat()}",
                now,
                0,
            )
        if used_mb * 10 >= EGRESS_QUOTA_MB * 9:
            _notice(
                conn,
                f"🚨 El ciclo de Supabase va en ~{used_mb / 1024:.1f} GB de 5 de egress (más del "
                "90 %) aun con el rastreo apagado: revisa qué consulta gasta antes de que "
                "Supabase restrinja la BD.",
                f"egress-90:{start.isoformat()}",
                now,
                0,
            )
        db.set_egress_mark(conn, total, now)
        db.set_egress_cycle(conn, start, rows)
        db.set_crawl_pace(conn, pace)
    return mb_day, used_mb


def maintenance_due(conn: psycopg.Connection, now: datetime) -> bool:
    n = local(now)
    last = db.bot_state(conn).last_maintenance_on
    return n.time() >= MAINTENANCE_AT and (last is None or last < n.date())


def summary_due(conn: psycopg.Connection, now: datetime) -> bool:
    n = local(now)
    last = db.bot_state(conn).last_summary_on
    return n.time() >= SUMMARY_AT and (last is None or last < n.date())


def _notice(conn: psycopg.Connection, text: str, key: str, now: datetime, priority: int) -> None:
    db.enqueue(conn, "system", text, priority=priority, dedup_key=key, now=now, ttl=SYSTEM_TTL)


def run_maintenance(
    conn: psycopg.Connection, now: datetime, params: AppParams, *, db_bytes: int | None = None
) -> MaintenanceReport:
    """Los 4 pasos del spec §2.4 en orden y las guardas de 300/400/480 MB (ADR 0021).

    `db_bytes` permite simular el tamaño en pruebas; por defecto se mide.
    """
    cutoff = now - SEGMENT_KEEP
    with conn.transaction():
        db.keep_alltime_min(conn, cutoff, params.glob.flash_max)  # 1
    deleted = db.delete_old_segments(conn, cutoff)  # 2
    with conn.transaction():
        glitches = db.reclassify_glitches(conn, now - GLITCH_RECLASS)  # 3
    retention = db.apply_retention(conn, now)  # 4
    retention += db.prune_discoveries(conn, now - timedelta(days=120), now - timedelta(days=365))
    with conn.transaction():
        retention += db.expire_auto_watches(conn, now)
    size = db.db_size(conn) if db_bytes is None else db_bytes
    unseen = CATALOG_UNSEEN_TIGHT if size >= PRUNE_BYTES else CATALOG_UNSEEN
    retention += db.delete_unseen_catalog_items(conn, unseen)
    retention += db.delete_old_channel_posts(conn, now - CHANNEL_POSTS_KEEP)
    retention += db.delete_old_offer_log(conn, now - OFFER_LOG_KEEP)
    retention += db.delete_old_ean_lookups(conn, now - EAN_LOOKUP_KEEP)
    week = local(now).strftime("%G-W%V")
    listings_pruned = segments_pruned = 0
    if size >= WARN_BYTES:
        top = ", ".join(f"{name} {b // MB} MB" for name, b in db.top_tables(conn))
        _notice(conn, f"BD en {size // MB} MB de 500. Más grandes: {top}.", f"db300:{week}", now, 2)
    if size >= PRUNE_BYTES:
        listings_pruned = db.prune_discoveries(conn, now - timedelta(days=60), None)
        segments_pruned = db.prune_unwatched_segments(conn, now - timedelta(days=180))
    if size >= PAUSE_BYTES:
        paused = True
        _notice(
            conn,
            f"⚠️ BD en {size // MB} MB (≥ 480 de 500): pauso las búsquedas y el rastreo de "
            "Éxito, Alkosto, eBay y Jumbo; siguen los seguimientos.",
            f"db480:{local(now).date().isoformat()}",
            now,
            0,
        )
    elif size < PRUNE_BYTES:
        paused = False  # por debajo de 400 MB se reanuda el descubrimiento
    else:
        paused = db.bot_state(conn).discovery_paused  # entre 400 y 480 se mantiene
    with conn.transaction():
        db.set_maintenance_state(conn, local(now).date(), size, paused)
    egress, cycle_mb = _measure_egress(conn, now)
    return MaintenanceReport(
        deleted,
        glitches,
        retention,
        listings_pruned,
        segments_pruned,
        size,
        paused,
        egress,
        cycle_mb,
    )


def daily_summary(conn: psycopg.Connection, now: datetime) -> DailyView:
    act = db.day_activity(conn, now)
    notes = []
    for code, health, blocked in act.sources:
        note = f"{code} {_HEALTH.get(health, health)}"
        if blocked:
            note += f" ({blocked} bloqueos)"
        notes.append(note)
    warnings = []
    expires = db.bot_state(conn).dispatch_token_expires_on
    if expires is not None and (expires - local(now).date()).days <= TOKEN_WARN_DAYS:
        warnings.append(f"⚠️ el token de disparo vence el {expires.isoformat()}: rótalo")
    return DailyView(
        cycles=sum(act.by_trigger.values()),
        last_cycle_at=act.last_cycle_at,
        alerts=act.alerts,
        source_notes=tuple(notes),
        db_bytes=db.db_size(conn),
        by_trigger=act.by_trigger,
        warnings=tuple(warnings),
        catalog=db.catalog_daily_stats(conn, now),
    )


def send_daily_summary(conn: psycopg.Connection, now: datetime) -> None:
    msg = render_daily(daily_summary(conn, now))
    day = local(now).date()
    with conn.transaction():
        db.enqueue(
            conn,
            "system",
            msg.html,
            priority=2,
            dedup_key=f"daily:{day.isoformat()}",
            now=now,
            ttl=SYSTEM_TTL,
            silent=True,
        )
        db.set_summary_on(conn, day)
