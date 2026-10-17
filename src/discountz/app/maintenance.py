"""Mantenimiento diario y resumen diario (spec §2.4, §1.3 paso 14 y §5.5).

El mantenimiento corre en el primer ciclo después de las 03:00 de Bogotá y el resumen en el
primero después de las 08:00. Los canarios se leen dentro del plan (tarea `canary`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import psycopg

from discountz.app.render import DailyView, local, render_daily
from discountz.config import AppParams
from discountz.infra import db

MB = 1024 * 1024
WARN_BYTES = 300 * MB
PRUNE_BYTES = 400 * MB
PAUSE_BYTES = 450 * MB
SEGMENT_KEEP = timedelta(days=400)
GLITCH_RECLASS = timedelta(days=7)
CATALOG_CHANGES_KEEP = timedelta(days=90)  # spec del rastreo §7.2
CATALOG_CHANGES_KEEP_TIGHT = timedelta(days=45)  # con la BD en ≥ 400 MB
CATALOG_UNSEEN = timedelta(days=30)
CHANNEL_POSTS_KEEP = timedelta(days=7)  # ventana de duplicados (24 h) más margen para el resumen
MAINTENANCE_AT = time(3, 0)
SUMMARY_AT = time(8, 0)
TOKEN_WARN_DAYS = 14
SYSTEM_TTL = timedelta(hours=48)
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
    """Los 4 pasos del spec §2.4 en orden y las guardas de 300/400/450 MB.

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
    retention += db.delete_old_catalog_changes(conn, now - CATALOG_CHANGES_KEEP)
    retention += db.delete_unseen_catalog_items(conn, now - CATALOG_UNSEEN)
    retention += db.delete_old_channel_posts(conn, now - CHANNEL_POSTS_KEEP)
    size = db.db_size(conn) if db_bytes is None else db_bytes
    week = local(now).strftime("%G-W%V")
    listings_pruned = segments_pruned = 0
    if size >= WARN_BYTES:
        top = ", ".join(f"{name} {b // MB} MB" for name, b in db.top_tables(conn))
        _notice(conn, f"BD en {size // MB} MB de 500. Más grandes: {top}.", f"db300:{week}", now, 2)
    if size >= PRUNE_BYTES:
        listings_pruned = db.prune_discoveries(conn, now - timedelta(days=60), None)
        segments_pruned = db.prune_unwatched_segments(conn, now - timedelta(days=180))
        retention += db.delete_old_catalog_changes(conn, now - CATALOG_CHANGES_KEEP_TIGHT)
    if size >= PAUSE_BYTES:
        paused = True
        _notice(
            conn,
            f"⚠️ BD en {size // MB} MB (≥ 450 de 500): pauso las búsquedas y el rastreo de "
            "Éxito y Carulla; siguen los seguimientos.",
            f"db450:{local(now).date().isoformat()}",
            now,
            0,
        )
    elif size < PRUNE_BYTES:
        paused = False  # por debajo de 400 MB se reanuda el descubrimiento
    else:
        paused = db.bot_state(conn).discovery_paused  # entre 400 y 450 se mantiene
    with conn.transaction():
        db.set_maintenance_state(conn, local(now).date(), size, paused)
    return MaintenanceReport(
        deleted, glitches, retention, listings_pruned, segments_pruned, size, paused
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
