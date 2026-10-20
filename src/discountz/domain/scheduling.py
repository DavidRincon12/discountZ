"""Planificador puro: presupuestos, prioridades, modo caliente y eventos (spec §4.5 y §6.2)."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from discountz.domain.health import BreakerState, effective_state

TaskKind = Literal["confirm", "canary", "resolve", "hot", "watch_target", "watch", "search", "feed"]
PRIORITY: Mapping[TaskKind, int] = {
    "confirm": 0,
    "canary": 1,
    "resolve": 2,
    "hot": 3,
    "watch_target": 4,
    "watch": 5,
    "search": 6,
    "feed": 6,
}
_BATCHABLE: frozenset[TaskKind] = frozenset({"hot", "watch_target", "watch", "feed"})
NOT_FOUND_RETRY = timedelta(hours=24)
FAIL_BASE = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class SourceBudget:
    group: str
    min_pause_s: float
    jitter_s: float
    max_per_run: int
    max_per_24h: int
    watch_interval: timedelta
    search_interval: timedelta
    max_batch: int
    max_auto_watches: int = 0  # tope de seguimientos automáticos (spec del rastreo §4.4)


@dataclass(frozen=True, slots=True)
class CrawlBudget:
    """Presupuesto del rastreo de catálogo de una fuente (spec del rastreo §2.3)."""

    min_pause_s: float
    jitter_s: float
    max_per_run: int
    max_per_24h: int
    hot_share_bp: int = 2500  # parte de la corrida para la capa caliente

    def run_cap(self, used_today: int, hour: int) -> int:
        """Peticiones de una corrida: lo que queda del día UTC repartido en sus horas.

        Las corridas de respaldo y las manuales no gastan de más: cada una toma su parte de lo que
        queda, así que el tope no se agota antes de la noche.
        """
        left = self.max_per_24h - used_today
        if left <= 0:
            return 0
        hours_left = 24 - hour
        return min(self.max_per_run, -(-left // hours_left))

    def paced(self, group: str, max_per_run: int) -> SourceBudget:
        """El `SourceBudget` que usa `PacedClient` para esta corrida."""
        return SourceBudget(
            group=group,
            min_pause_s=self.min_pause_s,
            jitter_s=self.jitter_s,
            max_per_run=max_per_run,
            max_per_24h=self.max_per_24h,
            watch_interval=timedelta(hours=6),
            search_interval=timedelta(hours=12),
            max_batch=1,
        )


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    sources: frozenset[str]
    starts_at: datetime
    ends_at: datetime

    def active(self, source: str, now: datetime) -> bool:
        return source in self.sources and self.starts_at <= now < self.ends_at


@dataclass(frozen=True, slots=True)
class Task:
    kind: TaskKind
    source: str
    payload_ids: tuple[int, ...]  # listing_id, watch_id o search_id; varios si va en lote


@dataclass(frozen=True, slots=True)
class WatchDue:
    id: int
    source: str
    listing_id: int | None
    pending: bool
    has_target: bool
    next_check_at: datetime
    interval: timedelta


@dataclass(frozen=True, slots=True)
class SearchDue:
    id: int
    source: str
    kind: Literal["search", "feed"]
    next_run_at: datetime
    interval: timedelta


@dataclass(frozen=True, slots=True)
class PlanInput:
    budgets: Mapping[str, SourceBudget]  # fuente → presupuesto (con su grupo)
    breakers: Mapping[str, BreakerState]
    usage_24h: Mapping[str, int]  # grupo → peticiones en 24 h
    ramp_until: Mapping[str, datetime | None]
    events: Sequence[Event]
    discovery_paused: bool
    pending_confirmations: Sequence[tuple[str, int]]  # (fuente, listing_id)
    canaries_due: Sequence[str]
    watches: Sequence[WatchDue]  # solo los vencidos
    hot_listings: Sequence[tuple[str, int]]  # (fuente, listing_id) con hot_until > now
    searches: Sequence[SearchDue]  # solo las vencidas


@dataclass(frozen=True, slots=True)
class _Item:
    kind: TaskKind
    source: str
    payload_id: int | None
    overdue: float


def effective_interval(
    base: timedelta, has_target: bool, hot: bool, events_active: bool, cycle: timedelta
) -> timedelta:
    """Caliente → 1 ciclo; con objetivo → mitad; evento → ÷3; nunca menos de un ciclo."""
    if hot:
        return cycle
    interval = base / 2 if has_target else base
    if events_active:
        interval /= 3
    return max(cycle, interval)


def next_check_at(
    now: datetime, interval: timedelta, failures: int, not_found: bool, rnd: random.Random
) -> datetime:
    if not_found:
        return now + NOT_FOUND_RETRY
    if failures > 0:
        return now + min(interval, FAIL_BASE * (1 << (failures - 1)))
    factor: float = rnd.uniform(0.85, 1.15)
    return now + interval * factor


def plan_run(inp: PlanInput, now: datetime) -> dict[str, list[Task]]:
    items = _items(inp, now)
    by_group: defaultdict[str, list[_Item]] = defaultdict(list)
    for item in items:
        budget = inp.budgets.get(item.source)
        if budget is not None:
            by_group[budget.group].append(item)
    plan: dict[str, list[Task]] = {}
    for group, group_items in by_group.items():
        sources = {i.source for i in group_items}
        open_sources = {s for s in sources if _state(inp, s, now) == "open"}
        half_open = {s for s in sources if _state(inp, s, now) == "half_open"}
        for source in sorted(half_open):
            plan[source] = [Task("canary", source, ())]
        live = [i for i in group_items if i.source not in open_sources | half_open]
        capacity = _capacity(inp, group, live, now) - len(half_open)
        _fill(plan, live, capacity, inp)
    return plan


def _state(inp: PlanInput, source: str, now: datetime) -> str:
    breaker = inp.breakers.get(source)
    return "closed" if breaker is None else effective_state(breaker, now)


def _items(inp: PlanInput, now: datetime) -> list[_Item]:
    items: list[_Item] = [_Item("confirm", s, lid, 0.0) for s, lid in inp.pending_confirmations]
    items += [_Item("canary", s, None, 0.0) for s in inp.canaries_due]
    hot_ids = {lid for _, lid in inp.hot_listings}
    items += [_Item("hot", s, lid, 0.0) for s, lid in inp.hot_listings]
    for w in inp.watches:
        overdue = (now - w.next_check_at) / w.interval
        if w.pending:
            items.append(_Item("resolve", w.source, w.id, overdue))
        elif w.listing_id is not None and w.listing_id not in hot_ids:
            kind: TaskKind = "watch_target" if w.has_target else "watch"
            items.append(_Item(kind, w.source, w.listing_id, overdue))
    if not inp.discovery_paused:
        items += [
            _Item(s.kind, s.source, s.id, (now - s.next_run_at) / s.interval) for s in inp.searches
        ]
    return sorted(items, key=lambda i: (PRIORITY[i.kind], -i.overdue))


def _capacity(inp: PlanInput, group: str, items: Sequence[_Item], now: datetime) -> int:
    """Peticiones planificables en el grupo: por ejecución y por 24 h, menos la reserva."""
    sources = sorted({i.source for i in items})
    if not sources:
        return 0
    budget = inp.budgets[sources[0]]
    ramp = any((r := inp.ramp_until.get(s)) is not None and now < r for s in sources)
    event = any(e.active(s, now) for e in inp.events for s in sources)
    per_run, per_24h = budget.max_per_run, budget.max_per_24h
    if ramp:
        per_run, per_24h = per_run // 2, per_24h // 2
    if event:
        per_run = per_run * 3 // 2
    effective = max(0, min(per_run, per_24h - inp.usage_24h.get(group, 0)))
    # La reserva es para lecturas de confirmación; un grupo que solo tiene feeds no confirma.
    feeds_only = all(i.kind == "feed" for i in items)
    reserve = 0 if feeds_only else min(effective, max(2, per_run * 20 // 100))
    return effective - reserve


def _fill(
    plan: dict[str, list[Task]], items: Sequence[_Item], capacity: int, inp: PlanInput
) -> None:
    used = 0
    open_batch: dict[tuple[str, TaskKind], list[int]] = {}
    tasks: list[tuple[str, TaskKind, list[int]]] = []
    for item in items:
        ids = [] if item.payload_id is None else [item.payload_id]
        key = (item.source, item.kind)
        # Un feed se baja una vez y sirve a todas sus búsquedas: va en un solo task.
        max_batch = len(items) if item.kind == "feed" else inp.budgets[item.source].max_batch
        batch = open_batch.get(key)
        if item.kind in _BATCHABLE and batch is not None and len(batch) < max_batch:
            batch.extend(ids)
            continue
        if used >= capacity:
            continue
        used += 1
        ids_list = list(ids)
        tasks.append((item.source, item.kind, ids_list))
        if item.kind in _BATCHABLE and max_batch > 1:
            open_batch[key] = ids_list
    for source, kind, ids in tasks:
        plan.setdefault(source, []).append(Task(kind, source, tuple(ids)))
