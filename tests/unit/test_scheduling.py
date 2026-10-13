import random
from dataclasses import replace
from datetime import timedelta

from discountz.domain.health import BreakerState
from discountz.domain.scheduling import (
    Event,
    PlanInput,
    SearchDue,
    SourceBudget,
    Task,
    WatchDue,
    effective_interval,
    next_check_at,
    plan_run,
)
from tests.unit.series import T

MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
CLOSED = BreakerState("closed", None, 0, 0, 0, 0)
OPEN = BreakerState("open", T + HOUR, 1, 0, 0, 0, T, "blocked")
HALF = BreakerState("open", T - MIN, 1, 0, 0, 0, T - HOUR, "blocked")
AMAZON = SourceBudget("amazon_us", 6, 6, 12, 400, 3 * HOUR, 6 * HOUR, 1)
EXITO = SourceBudget("grupo_exito", 2, 2, 30, 1500, HOUR, 3 * HOUR, 50)
CARULLA = replace(EXITO)


def watch(
    i: int,
    source: str = "amazon_us",
    *,
    overdue: timedelta = MIN,
    pending: bool = False,
    target: bool = False,
    interval: timedelta = 3 * HOUR,
) -> WatchDue:
    return WatchDue(
        i, source, None if pending else 1000 + i, pending, target, T - overdue, interval
    )


def inp(**kw) -> PlanInput:  # type: ignore[no-untyped-def]
    base: dict[str, object] = dict(
        budgets={"amazon_us": AMAZON, "exito": EXITO, "carulla": CARULLA},
        breakers={"amazon_us": CLOSED, "exito": CLOSED, "carulla": CLOSED},
        usage_24h={},
        ramp_until={},
        events=(),
        discovery_paused=False,
        pending_confirmations=(),
        canaries_due=(),
        watches=(),
        hot_listings=(),
        searches=(),
    )
    base.update(kw)
    return PlanInput(**base)  # type: ignore[arg-type]


def kinds(tasks: list[Task]) -> list[str]:
    return [t.kind for t in tasks]


def test_priority_order() -> None:
    plan = plan_run(
        inp(
            pending_confirmations=(("amazon_us", 1),),
            canaries_due=("amazon_us",),
            watches=(watch(1, pending=True), watch(2, target=True), watch(3)),
            hot_listings=(("amazon_us", 77),),
            searches=(SearchDue(5, "amazon_us", "search", T - MIN, 6 * HOUR),),
        ),
        T,
    )
    assert kinds(plan["amazon_us"]) == [
        "confirm",
        "canary",
        "resolve",
        "hot",
        "watch_target",
        "watch",
        "search",
    ]


def test_overdue_ordering_within_priority() -> None:
    plan = plan_run(
        inp(watches=(watch(1, overdue=MIN), watch(2, overdue=2 * HOUR), watch(3, overdue=HOUR))), T
    )
    assert [t.payload_ids for t in plan["amazon_us"]] == [(1002,), (1003,), (1001,)]


def test_budget_per_run_with_confirmation_reserve_and_no_burst() -> None:
    many = tuple(watch(i, overdue=i * HOUR) for i in range(1, 60))
    plan = plan_run(inp(watches=many), T)
    reserve = max(2, AMAZON.max_per_run * 20 // 100)
    assert len(plan["amazon_us"]) == AMAZON.max_per_run - reserve


def test_no_burst_after_cron_delay() -> None:
    late = tuple(watch(i, overdue=10 * HOUR) for i in range(1, 200))
    assert len(plan_run(inp(watches=late), T)["amazon_us"]) <= AMAZON.max_per_run


def test_24h_budget_is_never_exceeded() -> None:
    plan = plan_run(
        inp(watches=tuple(watch(i) for i in range(1, 30)), usage_24h={"amazon_us": 397}), T
    )
    assert len(plan["amazon_us"]) == 3 - 2  # quedan 3 en 24 h y 2 se reservan para confirmar


def test_ramp_halves_budgets() -> None:
    plan = plan_run(
        inp(watches=tuple(watch(i) for i in range(1, 30)), ramp_until={"amazon_us": T + HOUR}), T
    )
    assert len(plan["amazon_us"]) == 6 - 2


def test_open_breaker_gets_nothing_and_half_open_exactly_one_canary() -> None:
    w = tuple(watch(i) for i in range(1, 5))
    assert plan_run(inp(watches=w, breakers={"amazon_us": OPEN}), T).get("amazon_us", []) == []
    half = plan_run(inp(watches=w, breakers={"amazon_us": HALF}), T)["amazon_us"]
    assert kinds(half) == ["canary"]


def test_event_raises_run_budget_by_half_within_24h() -> None:
    ev = Event("Black Friday", frozenset({"amazon_us"}), T - HOUR, T + HOUR)
    many = tuple(watch(i) for i in range(1, 60))
    plan = plan_run(inp(watches=many, events=(ev,)), T)
    assert len(plan["amazon_us"]) == 18 - max(2, 18 * 20 // 100)
    capped = plan_run(inp(watches=many, events=(ev,), usage_24h={"amazon_us": 395}), T)
    assert len(capped["amazon_us"]) == 5 - 3


def test_vtex_watches_are_batched() -> None:
    w = tuple(watch(i, "exito", interval=HOUR) for i in range(1, 121))
    tasks = plan_run(inp(watches=w), T)["exito"]
    assert [len(t.payload_ids) for t in tasks] == [50, 50, 20]
    assert {t.kind for t in tasks} == {"watch"}


def test_group_budget_is_shared() -> None:
    w = tuple(watch(i, "exito", interval=HOUR) for i in range(1, 3))
    s = tuple(
        SearchDue(i, src, "search", T - MIN, 3 * HOUR)
        for i in range(40)
        for src in ("exito", "carulla")
    )
    plan = plan_run(inp(watches=w, searches=s), T)
    total = sum(len(plan.get(src, [])) for src in ("exito", "carulla"))
    assert total == 30 - 6


def test_discovery_paused_removes_search() -> None:
    s = (SearchDue(1, "amazon_us", "search", T - MIN, 6 * HOUR),)
    assert plan_run(inp(searches=s, discovery_paused=True), T).get("amazon_us", []) == []


def test_effective_interval() -> None:
    base, cycle = 3 * HOUR, 5 * MIN
    assert effective_interval(base, False, False, False, cycle) == base
    assert effective_interval(base, True, False, False, cycle) == base / 2
    assert effective_interval(base, False, True, False, cycle) == cycle
    assert effective_interval(base, False, False, True, cycle) == HOUR
    assert effective_interval(6 * MIN, True, False, True, cycle) == cycle


def test_next_check_at() -> None:
    rnd = random.Random(42)
    t = next_check_at(T, HOUR, 0, False, rnd)
    assert T + 51 * MIN <= t <= T + 69 * MIN
    assert next_check_at(T, 3 * HOUR, 1, False, rnd) == T + 30 * MIN
    assert next_check_at(T, 3 * HOUR, 3, False, rnd) == T + 2 * HOUR
    assert next_check_at(T, HOUR, 5, False, rnd) == T + HOUR
    assert next_check_at(T, HOUR, 0, True, rnd) == T + 24 * HOUR
