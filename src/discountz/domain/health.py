"""Circuit breaker persistido y clasificación de salud de fuentes (spec §4.6-§4.7). Puro."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

BreakerName = Literal["closed", "open", "half_open"]
TripKind = Literal["blocked", "unavailable", "parse"]
Health = Literal["healthy", "degraded", "blocked", "broken"]

UNAVAILABLE_TRIP = 5
PARSE_TRIP = 3
BASE_BACKOFF = timedelta(minutes=30)
MAX_BACKOFF = timedelta(hours=12)
TRIPS_RESET = timedelta(hours=24)
QUARANTINE_CYCLES = 3


class BreakerEvent(StrEnum):
    OK = "ok"
    HARD_BLOCK = "hard_block"
    UNAVAILABLE = "unavailable"
    PARSE_ERROR = "parse_error"
    PROBE_OK = "probe_ok"
    PROBE_FAIL = "probe_fail"


@dataclass(frozen=True, slots=True)
class BreakerState:
    state: BreakerName  # persistido como 'closed' u 'open'; half_open = open ∧ now ≥ open_until
    open_until: datetime | None
    trips: int
    consecutive_unavailable: int
    consecutive_parse: int
    quarantine_cycles: int
    last_trip_at: datetime | None = None
    last_trip_kind: TripKind | None = None


def effective_state(state: BreakerState, now: datetime) -> BreakerName:
    if state.state == "open" and state.open_until is not None and now >= state.open_until:
        return "half_open"
    return state.state


def breaker_transition(
    state: BreakerState, event: BreakerEvent, now: datetime, rnd: random.Random
) -> BreakerState:
    """Transición pura del breaker; el llamador la persiste en la transacción de la tarea."""
    if (
        state.trips
        and state.last_trip_at is not None
        and now - state.last_trip_at >= TRIPS_RESET
        and state.state == "closed"
    ):
        state = replace(state, trips=0)
    current = effective_state(state, now)
    if current == "open":
        return state
    if current == "half_open":
        if event in (BreakerEvent.PROBE_OK, BreakerEvent.OK):
            quarantine = QUARANTINE_CYCLES if state.last_trip_kind == "parse" else 0
            return replace(
                state,
                state="closed",
                open_until=None,
                consecutive_unavailable=0,
                consecutive_parse=0,
                quarantine_cycles=max(state.quarantine_cycles, quarantine),
            )
        return _trip(state, state.last_trip_kind or "unavailable", now, rnd)
    if event in (BreakerEvent.OK, BreakerEvent.PROBE_OK):
        return replace(state, consecutive_unavailable=0, consecutive_parse=0)
    if event is BreakerEvent.HARD_BLOCK:
        return _trip(state, "blocked", now, rnd)
    if event is BreakerEvent.UNAVAILABLE:
        state = replace(state, consecutive_unavailable=state.consecutive_unavailable + 1)
        if state.consecutive_unavailable >= UNAVAILABLE_TRIP:
            return _trip(state, "unavailable", now, rnd)
        return state
    if event is BreakerEvent.PARSE_ERROR:
        state = replace(state, consecutive_parse=state.consecutive_parse + 1)
        if state.consecutive_parse >= PARSE_TRIP:
            return _trip(state, "parse", now, rnd)
        return state
    return state


def _trip(state: BreakerState, kind: TripKind, now: datetime, rnd: random.Random) -> BreakerState:
    trips = min(state.trips + 1, 20)
    backoff = min(MAX_BACKOFF, BASE_BACKOFF * 2 ** (trips - 1))
    return replace(
        state,
        state="open",
        open_until=now + backoff * rnd.uniform(1.0, 1.1),
        trips=trips,
        last_trip_at=now,
        last_trip_kind=kind,
    )


def tick_quarantine(
    state: BreakerState, *, parse_errors: int, canary_in_range: bool | None
) -> BreakerState:
    """Un ciclo sin errores de parseo y con canario en rango descuenta 1 de la cuarentena."""
    if state.quarantine_cycles and parse_errors == 0 and canary_in_range is not False:
        return replace(state, quarantine_cycles=state.quarantine_cycles - 1)
    return state


@dataclass(frozen=True, slots=True)
class HourStat:
    requests: int
    ok: int
    blocked: int
    parse_errors: int
    soft_empty: int
    confirm_mismatch: int


def classify_health(
    stats_24h: Sequence[HourStat],
    breaker: BreakerState,
    canary_in_range: bool | None,
    consecutive_empty_searches: int,
) -> Health:
    """Salud de una fuente. `stats_24h` va en orden por hora; las 6 últimas son las últimas 6 h."""
    if (
        (breaker.state == "open" and breaker.last_trip_kind == "parse")
        or sum(h.soft_empty for h in stats_24h) >= 3
        or consecutive_empty_searches >= 3
        or canary_in_range is False
    ):
        return "broken"
    last6 = stats_24h[-6:]
    req6, blocked6 = sum(h.requests for h in last6), sum(h.blocked for h in last6)
    if (breaker.state == "open" and breaker.last_trip_kind == "blocked") or (
        req6 >= 4 and blocked6 * 2 >= req6
    ):
        return "blocked"
    requests, ok = sum(h.requests for h in stats_24h), sum(h.ok for h in stats_24h)
    if (
        (requests >= 20 and ok * 10 < requests * 8)
        or breaker.trips >= 1
        or sum(h.confirm_mismatch for h in stats_24h) >= 3
    ):
        return "degraded"
    return "healthy"
