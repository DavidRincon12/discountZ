import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from discountz.domain.health import (
    BreakerEvent,
    BreakerState,
    HourStat,
    breaker_transition,
    classify_health,
    effective_state,
    tick_quarantine,
)
from tests.unit.series import T

MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
CLOSED = BreakerState("closed", None, 0, 0, 0, 0)


def step(state: BreakerState, event: BreakerEvent, now: datetime = T) -> BreakerState:
    return breaker_transition(state, event, now, random.Random(0))


def test_hard_block_opens_immediately_with_30_min_backoff() -> None:
    s = step(CLOSED, BreakerEvent.HARD_BLOCK)
    assert s.state == "open" and s.trips == 1 and s.last_trip_kind == "blocked"
    assert s.open_until is not None
    assert 30 * MIN <= s.open_until - T <= 33 * MIN


def test_five_unavailable_in_a_row_open() -> None:
    s = CLOSED
    for _ in range(4):
        s = step(s, BreakerEvent.UNAVAILABLE)
    assert s.state == "closed" and s.consecutive_unavailable == 4
    s = step(s, BreakerEvent.UNAVAILABLE)
    assert s.state == "open" and s.last_trip_kind == "unavailable"


def test_ok_resets_consecutive_counters() -> None:
    s = step(step(CLOSED, BreakerEvent.UNAVAILABLE), BreakerEvent.PARSE_ERROR)
    s = step(s, BreakerEvent.OK)
    assert (s.consecutive_unavailable, s.consecutive_parse) == (0, 0)


def test_three_parse_errors_open() -> None:
    s = CLOSED
    for _ in range(3):
        s = step(s, BreakerEvent.PARSE_ERROR)
    assert s.state == "open" and s.last_trip_kind == "parse"


def test_open_becomes_half_open_after_open_until() -> None:
    s = step(CLOSED, BreakerEvent.HARD_BLOCK)
    assert effective_state(s, T + 10 * MIN) == "open"
    assert s.open_until is not None
    assert effective_state(s, s.open_until) == "half_open"


def test_events_while_open_are_ignored() -> None:
    s = step(CLOSED, BreakerEvent.HARD_BLOCK)
    assert step(s, BreakerEvent.OK, T + MIN) == s


def test_probe_ok_closes_and_parse_trip_sets_quarantine() -> None:
    s = CLOSED
    for _ in range(3):
        s = step(s, BreakerEvent.PARSE_ERROR)
    assert s.open_until is not None
    closed = step(s, BreakerEvent.PROBE_OK, s.open_until)
    assert closed.state == "closed" and closed.open_until is None
    assert closed.quarantine_cycles == 3


def test_probe_ok_after_block_has_no_quarantine() -> None:
    s = step(CLOSED, BreakerEvent.HARD_BLOCK)
    assert s.open_until is not None
    assert step(s, BreakerEvent.PROBE_OK, s.open_until).quarantine_cycles == 0


@pytest.mark.parametrize(
    ("trips", "base"), [(1, 30 * MIN), (2, HOUR), (3, 2 * HOUR), (6, 12 * HOUR), (9, 12 * HOUR)]
)
def test_backoff_doubles_up_to_12h(trips: int, base: timedelta) -> None:
    s = replace(step(CLOSED, BreakerEvent.HARD_BLOCK), trips=trips - 1)
    assert s.open_until is not None
    reopened = step(s, BreakerEvent.PROBE_FAIL, s.open_until)
    assert reopened.state == "open" and reopened.trips == trips
    assert reopened.open_until is not None
    delta = reopened.open_until - s.open_until
    assert base <= delta <= base * 11 / 10


def test_trips_reset_after_24h_without_trips() -> None:
    s = step(CLOSED, BreakerEvent.HARD_BLOCK)
    assert s.open_until is not None
    closed = step(s, BreakerEvent.PROBE_OK, s.open_until)
    assert closed.trips == 1
    assert step(closed, BreakerEvent.OK, T + 25 * HOUR).trips == 0


def test_quarantine_ticks_down_only_on_clean_cycles() -> None:
    q = replace(CLOSED, quarantine_cycles=3)
    assert tick_quarantine(q, parse_errors=0, canary_in_range=True).quarantine_cycles == 2
    assert tick_quarantine(q, parse_errors=1, canary_in_range=True).quarantine_cycles == 3
    assert tick_quarantine(q, parse_errors=0, canary_in_range=False).quarantine_cycles == 3
    assert tick_quarantine(CLOSED, parse_errors=0, canary_in_range=True).quarantine_cycles == 0


def hours(n: int, **kw: int) -> list[HourStat]:
    base = dict(requests=0, ok=0, blocked=0, parse_errors=0, soft_empty=0, confirm_mismatch=0)
    base.update(kw)
    return [HourStat(**base) for _ in range(n)]


def test_healthy() -> None:
    assert classify_health(hours(24, requests=10, ok=10), CLOSED, True, 0) == "healthy"


def test_broken_when_breaker_open_by_parse_or_canary_out_of_range() -> None:
    s = CLOSED
    for _ in range(3):
        s = step(s, BreakerEvent.PARSE_ERROR)
    assert classify_health(hours(24, requests=10, ok=10), s, True, 0) == "broken"
    assert classify_health(hours(24, requests=10, ok=10), CLOSED, False, 0) == "broken"
    assert classify_health(hours(24, requests=10, ok=10), CLOSED, None, 3) == "broken"


def test_three_soft_empty_cycles_is_broken() -> None:
    stats = hours(21, requests=10, ok=10) + hours(3, requests=2, ok=2, soft_empty=1)
    assert classify_health(stats, CLOSED, True, 0) == "broken"


def test_blocked_by_breaker_or_rate_in_last_6h() -> None:
    assert classify_health(hours(24), step(CLOSED, BreakerEvent.HARD_BLOCK), True, 0) == "blocked"
    stats = hours(18, requests=10, ok=10) + hours(6, requests=1, ok=0, blocked=1)
    assert classify_health(stats, CLOSED, True, 0) == "blocked"
    few = hours(21, requests=10, ok=10) + hours(3, requests=1, ok=0, blocked=1)
    assert classify_health(few, CLOSED, True, 0) != "blocked"


def test_degraded_by_success_rate_trip_or_mismatch() -> None:
    low = hours(24, requests=1, ok=0) + hours(1, requests=19, ok=19)
    assert classify_health(low[-24:], CLOSED, True, 0) == "degraded"
    tripped = replace(CLOSED, trips=1, last_trip_at=T - HOUR, last_trip_kind="unavailable")
    assert classify_health(hours(24, requests=10, ok=10), tripped, True, 0) == "degraded"
    mism = hours(21, requests=10, ok=10) + hours(3, requests=10, ok=10, confirm_mismatch=1)
    assert classify_health(mism, CLOSED, True, 0) == "degraded"
