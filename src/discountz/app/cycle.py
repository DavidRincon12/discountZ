"""El ciclo completo (spec §1.3): lease, bandeja, plan, recolección, confirmación y entrega.

Códigos de salida: 0 `ok`/`partial`/`skipped_*`; 1 bug, Telegram 403 o 3 `skipped_locked`
seguidos; 2 configuración (en `__main__`); 3 BD inaccesible (en `__main__`).
"""

from __future__ import annotations

import logging
import random
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import psycopg

from discountz.app.channels import due_channels, run_channels
from discountz.app.collect import SYSTEM_TTL, Collector, SourceRun
from discountz.app.compose import compose_alerts
from discountz.app.confirm import confirm_candidates
from discountz.app.deliver import deliver, make_photo_fetcher
from discountz.app.held import digest_due, send_digest
from discountz.app.inbox import process_inbox
from discountz.app.interests import active_interests
from discountz.app.links import make_link_resolver
from discountz.app.maintenance import (
    MB,
    maintenance_due,
    run_maintenance,
    send_daily_summary,
    summary_due,
)
from discountz.app.render import local, source_name
from discountz.config import (
    AppParams,
    Canary,
    InterestsConfig,
    Settings,
    Topics,
    load_interests,
    load_topics,
)
from discountz.domain.errors import TelegramError
from discountz.domain.health import BreakerState, classify_health, effective_state, tick_quarantine
from discountz.domain.scheduling import PlanInput, Task, plan_run
from discountz.infra import db
from discountz.infra.healthchecks import Healthchecks
from discountz.infra.http import HttpClient, PacedClient
from discountz.infra.log import event
from discountz.infra.telegram import TelegramClient
from discountz.sources.base import CredentialStore, NoCredentials, Source
from discountz.sources.telegram_channels import channel_link_hosts

log = logging.getLogger(__name__)

CYCLE = timedelta(minutes=15)  # intervalo nominal entre ciclos (disparo externo 5 min + cola)
INBOX_SECONDS = 20
LOCKED_ALERT_AFTER = 3
_CLOSED = BreakerState("closed", None, 0, 0, 0, 0)
_NOTICES = {
    "healthy": "{name}: recuperada.",
    "degraded": "{name}: degradada (muchos errores o confirmaciones que no cuadran).",
    "blocked": "{name}: bloqueada por la tienda; pauso sus lecturas.",
    "broken": "{name}: posible cambio de formato; pongo la fuente en cuarentena.",
}

CycleStatus = Literal["ok", "partial", "skipped_locked", "skipped_schema", "failed"]


@dataclass(frozen=True, slots=True)
class CycleDeps:
    conn: psycopg.Connection
    sources: Mapping[str, Source]
    http: Mapping[
        str, HttpClient
    ]  # transporte crudo por fuente; el ciclo lo envuelve en PacedClient
    telegram: TelegramClient
    params: AppParams
    canaries: Mapping[str, Sequence[Canary]]
    settings: Settings
    holder: str
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    rnd: random.Random
    healthchecks: Healthchecks
    credentials: CredentialStore = field(default_factory=NoCredentials)
    # Transporte sin redirecciones para las fotos de las tarjetas y el enlace real de los canales.
    photos: HttpClient | None = None
    topics: Topics = field(default_factory=load_topics)  # tema de las ofertas de canales
    interests: InterestsConfig = field(default_factory=load_interests)


@dataclass(frozen=True, slots=True)
class CycleResult:
    status: CycleStatus
    exit_code: int
    run_id: int | None = None


def _trigger(settings: Settings) -> tuple[str, int | None, int | None]:
    kind = settings.trigger if settings.trigger in ("schedule", "workflow_dispatch") else "local"
    if kind == "local" or settings.gh_run_id is None:
        return "local", None, None
    return kind, settings.gh_run_id, settings.gh_run_attempt or 1


def run_cycle(deps: CycleDeps, now: datetime) -> CycleResult:
    conn, s = deps.conn, deps.settings
    t0 = deps.monotonic()
    deadline = t0 + s.soft_deadline_s
    trigger, gh_id, gh_attempt = _trigger(s)
    deps.healthchecks.start()
    if db.schema_version(conn) != db.SCHEMA_VERSION:
        schema_run = db.start_run(conn, now, trigger, gh_id, gh_attempt)
        with conn.transaction():
            db.finish_run(conn, schema_run, now, "skipped_schema", db.RunCounters())
            db.enqueue(
                conn,
                "system",
                "discountZ: hay una migración pendiente; no proceso hasta aplicarla.",
                priority=2,
                dedup_key=f"schema:{local(now).date().isoformat()}",
                now=now,
                ttl=SYSTEM_TTL,
            )
        event(log, "cycle_finished", status="skipped_schema", schema_run=schema_run)
        return CycleResult("skipped_schema", 0, schema_run)
    if not db.acquire_lease(conn, deps.holder):
        return _skipped_locked(deps, now, trigger, gh_id, gh_attempt)
    run_id: int | None = None
    try:
        run_id = db.start_run(conn, now, trigger, gh_id, gh_attempt)
        result = _work(deps, now, run_id, deadline)
    except Exception as exc:
        event(log, "cycle_failed", logging.ERROR, run_id=run_id, error=type(exc).__name__)
        if run_id is not None:
            db.finish_run(conn, run_id, now, "failed", db.RunCounters(error_code="bug"))
        deps.healthchecks.fail()
        return CycleResult("failed", 1, run_id)
    finally:
        db.release_lease(conn, deps.holder)
    if result.exit_code == 0:
        deps.healthchecks.success()
    else:
        deps.healthchecks.fail()
    return result


def _skipped_locked(
    deps: CycleDeps, now: datetime, trigger: str, gh_id: int | None, gh_attempt: int | None
) -> CycleResult:
    conn = deps.conn
    run_id = db.start_run(conn, now, trigger, gh_id, gh_attempt)
    db.finish_run(conn, run_id, now, "skipped_locked", db.RunCounters())
    streak = db.recent_run_statuses(conn, LOCKED_ALERT_AFTER)
    stuck = len(streak) == LOCKED_ALERT_AFTER and all(x == "skipped_locked" for x in streak)
    if stuck:
        db.enqueue(
            conn,
            "system",
            "discountZ: 3 ciclos seguidos encontraron el lease ocupado; revisa si hay uno colgado.",
            priority=2,
            dedup_key=f"locked:{local(now):%Y-%m-%dT%H}",
            now=now,
            ttl=SYSTEM_TTL,
        )
    event(log, "cycle_finished", status="skipped_locked", run_id=run_id)
    return CycleResult("skipped_locked", 1 if stuck else 0, run_id)


def _inbox(deps: CycleDeps, now: datetime) -> int:
    s = deps.settings
    try:
        return process_inbox(
            deps.conn,
            deps.telegram,
            s.telegram_owner_id,
            s.telegram_chat_id,
            now,
            INBOX_SECONDS,
            sources=deps.sources,
            monotonic=deps.monotonic,
            topics=deps.topics,
            interests=deps.interests,
        )
    except TelegramError as exc:  # Telegram caído: el ciclo sigue sin comandos
        event(log, "inbox_unavailable", code=str(exc))
        return 0


def _work(deps: CycleDeps, now: datetime, run_id: int, deadline: float) -> CycleResult:
    conn, params = deps.conn, deps.params
    db.recover_outbox(conn, now)
    updates = _inbox(deps, now)
    inp = db.load_plan_input(conn, now, params.budgets, params.events, CYCLE)
    plan = plan_run(inp, now)
    stores = {
        code: src
        for code, src in deps.sources.items()
        if code in inp.budgets and src.descriptor.kind == "store"
    }
    collector = Collector(
        conn,
        now,
        params,
        deps.canaries,
        deps.rnd,
        deps.monotonic,
        deadline,
        CYCLE,
        stores=stores,
        spawn=lambda code: _spawn_run(deps, inp, code, now, deadline),
        credentials=deps.credentials,
    )
    runs = _source_runs(deps, plan, inp.breakers, deadline)
    collector.run(runs)
    for r in runs:
        try:
            confirm_candidates(collector, r)
        except Exception as exc:
            r.failed = True
            event(log, "confirm_failed", logging.ERROR, source=r.code, error=type(exc).__name__)
        _close_source(conn, r, now)
    compose_alerts(conn, collector.views, now, params)
    channel_runs = _channel_runs(deps, inp, now, deadline)
    resolve = make_link_resolver(deps.photos, channel_link_hosts()) if deps.photos else None
    run_channels(
        conn,
        channel_runs,
        now,
        params,
        deps.topics,
        deps.rnd,
        deadline,
        resolve=resolve,
        interests=active_interests(conn, deps.interests),
        interest_cap=deps.interests.daily_cards_per_interest,
    )
    for r in channel_runs:
        _close_source(conn, r, now)
    runs += channel_runs
    _daily(conn, now, params)
    updates += _inbox(deps, now)
    delivery = deliver(
        conn,
        deps.telegram,
        deps.settings.telegram_chat_id,
        now,
        sleep=deps.sleep,
        monotonic=deps.monotonic,
        deadline=deadline + 120,  # la entrega puede usar el margen hasta el plazo duro
        photos=make_photo_fetcher(deps.photos, deps.sources, sleep=deps.sleep)
        if deps.photos
        else None,
        channel_id=deps.settings.telegram_channel_id,
    )
    status: CycleStatus = "partial" if any(r.failed for r in runs) else "ok"
    counters = db.RunCounters(
        requests=sum(r.http.used for r in runs),
        alerts_created=collector.alerts_created,
        messages_sent=delivery.sent,
        updates_processed=updates,
        error_code="telegram_forbidden" if delivery.forbidden else None,
    )
    db.finish_run(conn, run_id, now, status, counters)
    exit_code = 1 if delivery.forbidden else 0
    event(
        log,
        "cycle_finished",
        status=status,
        run_id=run_id,
        requests=counters.requests,
        alerts=counters.alerts_created,
        sent=delivery.sent,
        updates=updates,
        exit=exit_code,
    )
    return CycleResult(status, exit_code, run_id)


def _daily(conn: psycopg.Connection, now: datetime, params: AppParams) -> None:
    """Paso 14: mantenimiento, resumen diario y resumen de la noche (ADR 0012); errores
    registrados, el ciclo sigue (los resúmenes salen en esta misma entrega)."""
    try:
        if maintenance_due(conn, now):
            report = run_maintenance(conn, now, params)
            event(
                log,
                "maintenance",
                count=report.segments_deleted + report.retention_deleted,
                db_mb=report.db_bytes // MB,  # el peso de la BD se vigila desde Actions
            )
        if summary_due(conn, now):
            send_daily_summary(conn, now)
        if digest_due(conn, now):
            send_digest(conn, now, params)
    except Exception as exc:
        event(log, "maintenance_failed", logging.ERROR, error=type(exc).__name__)


def _source_runs(
    deps: CycleDeps,
    plan: Mapping[str, Sequence[Task]],
    breakers: Mapping[str, BreakerState],
    deadline: float,
) -> list[SourceRun]:
    runs = []
    for code, tasks in plan.items():
        if tasks and (run := _make_run(deps, code, tasks, breakers, deadline)) is not None:
            runs.append(run)
    return runs


def _make_run(
    deps: CycleDeps,
    code: str,
    tasks: Sequence[Task],
    breakers: Mapping[str, BreakerState],
    deadline: float,
) -> SourceRun | None:
    source, transport = deps.sources.get(code), deps.http.get(code)
    budget = deps.params.budgets.get(code)
    if source is None or transport is None or budget is None:
        return None
    paced = PacedClient(
        transport,
        budget,
        deps.sleep,
        deps.monotonic,
        deps.rnd,
        source.detect_block,
        source=code,
        deadline=deadline,
    )
    return SourceRun(source, paced, budget, breakers.get(code, _CLOSED), deque(tasks))


def _spawn_run(
    deps: CycleDeps, inp: PlanInput, code: str, now: datetime, deadline: float
) -> SourceRun | None:
    """SourceRun para la tienda de un lead que no tenía tareas: activa, con el breaker
    cerrado y con presupuesto de 24 h disponible."""
    budget = inp.budgets.get(code)
    if budget is None:
        return None
    breaker = inp.breakers.get(code, _CLOSED)
    if effective_state(breaker, now) != "closed":
        return None
    if inp.usage_24h.get(budget.group, 0) >= budget.max_per_24h:
        return None
    return _make_run(deps, code, (), inp.breakers, deadline)


def _channel_runs(
    deps: CycleDeps, inp: PlanInput, now: datetime, deadline: float
) -> list[SourceRun]:
    """SourceRun de cada canal que toca leer y aún tiene presupuesto de 24 h."""
    runs = []
    for code in due_channels(
        deps.conn, deps.sources, inp.budgets, inp.breakers, now, deps.params.channel_interval
    ):
        budget = inp.budgets[code]
        if inp.usage_24h.get(budget.group, 0) >= budget.max_per_24h:
            continue
        if (run := _make_run(deps, code, (), inp.breakers, deadline)) is not None:
            runs.append(run)
    return runs


def _close_source(conn: psycopg.Connection, r: SourceRun, now: datetime) -> None:
    """Contadores de la hora, breaker, cuarentena y salud de la fuente; avisa si cambió."""
    stat, extra = r.counters.hour_stat(r.http.used)
    breaker = tick_quarantine(
        r.breaker, parse_errors=r.counters.parse_errors, canary_in_range=r.canary_ok
    )
    with conn.transaction():
        db.add_hour_stat(conn, r.code, now, stat, offers=r.counters.offers, **extra)
        db.save_breaker(conn, r.code, breaker)
        row = db.source_health(conn, r.code)
        health = classify_health(
            db.load_hour_stats(conn, r.code, now),
            breaker,
            row.canary_ok,
            db.max_consecutive_empty(conn, r.code),
        )
        notify = health != row.notified_health
        db.save_health(conn, r.code, health, now, notified=notify)
        if notify:
            db.enqueue(
                conn,
                "system",
                _NOTICES[health].format(name=f"{source_name(r.code)} ({r.code})"),
                priority=2,
                dedup_key=f"health:{r.code}:{health}:{now:%Y%m%dT%H%M}",
                now=now,
                ttl=SYSTEM_TTL,
            )
    event(log, "source_done", source=r.code, requests=r.http.used, health=health)
