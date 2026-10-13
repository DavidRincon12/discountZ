from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import psycopg
import pytest

from discountz.domain.health import BreakerState, HourStat
from discountz.domain.model import AlertKind, Maturity, SellerClass
from discountz.domain.segments import Observation, SegmentStatus, gap_h
from discountz.infra import db as repo
from tests.factories import offer
from tests.unit.series import T

pytestmark = pytest.mark.integration
HOUR = timedelta(hours=1)
URL = "https://www.exito.com/tv/p"


def test_two_cycles_only_one_gets_lease(
    db: psycopg.Connection, connect_extra: Callable[[], psycopg.Connection]
) -> None:
    other = connect_extra()
    assert repo.acquire_lease(db, "gh:1:1")
    assert not repo.acquire_lease(other, "gh:2:1")
    repo.release_lease(db, "gh:1:1")
    assert repo.acquire_lease(other, "gh:2:1")


def test_expired_lease_is_reassigned(db: psycopg.Connection) -> None:
    assert repo.acquire_lease(db, "gh:1:1")
    db.execute("UPDATE discountz.run_lease SET expires_at = now() - interval '1 second'")
    assert repo.acquire_lease(db, "gh:2:1")
    repo.release_lease(db, "gh:1:1")  # el viejo dueño ya no puede liberarlo
    assert db.execute("SELECT holder FROM discountz.run_lease").fetchone() == ("gh:2:1",)


def test_runs_track_gap_and_orphans(db: psycopg.Connection) -> None:
    first = repo.start_run(db, T - 20 * timedelta(minutes=1) - 16 * HOUR, "schedule", 10, 1)
    second = repo.start_run(db, T, "workflow_dispatch", 11, 1)
    status = db.execute("SELECT status FROM discountz.run WHERE id = %s", (first,)).fetchone()
    assert status == ("failed",)
    repo.finish_run(db, second, T + HOUR / 60, "ok", repo.RunCounters(requests=5, alerts_created=1))
    row = db.execute("SELECT status, gap_seconds, requests FROM discountz.run WHERE id = %s",
                     (second,)).fetchone()  # fmt: skip
    assert row is not None and row[0] == "ok" and row[2] == 5 and row[1] > 0


def _sample(t_hours: float, price: int = 1_990) -> Observation:
    return Observation(T + timedelta(hours=t_hours), price, None, True, SellerClass.FIRST_PARTY,
                       "un|1")  # fmt: skip


def test_write_sample_extends_and_closes(db: psycopg.Connection) -> None:
    state = repo.upsert_listing(db, offer(external_id="555"), "otros", URL, T)
    h = gap_h(timedelta(hours=12))
    t = 0.0
    for i in range(20):  # U19: lecturas cada 10,2-13,8 h
        repo.write_sample(db, state.id, _sample(t), SegmentStatus.OPEN, h)
        t += 10.2 if i % 2 else 13.8
    segs = repo.load_history(db, [state.id], T - 30 * 24 * HOUR)[state.id]
    assert len(segs) == 1 and segs[0].sample_count == 20
    repo.write_sample(db, state.id, _sample(t, 1_490), SegmentStatus.OPEN, h)
    segs = repo.load_history(db, [state.id], T - 30 * 24 * HOUR)[state.id]
    assert [s.price_minor for s in segs] == [1_990, 1_490]
    assert segs[0].ended_at == segs[1].started_at and segs[1].ended_at is None


def test_upsert_listing_returns_previous_price(db: psycopg.Connection) -> None:
    first = repo.upsert_listing(db, offer(price=100_000), "componentes", URL, T)
    assert first.last_price_minor is None and first.category == "componentes"
    again = repo.upsert_listing(db, offer(price=90_000), "otros", URL, T + HOUR)
    assert again.id == first.id and again.last_price_minor == 100_000
    assert again.category == "componentes"  # la categoría no cambia al releer


def test_insert_alert_duplicate_returns_none(db: psycopg.Connection) -> None:
    state = repo.upsert_listing(db, offer(), "otros", URL, T)
    seg = repo.write_sample(db, state.id, _sample(0, 100_000), SegmentStatus.OPEN, 12 * HOUR)
    alert = repo.NewAlert(state.id, seg.started_at, None, AlertKind.GOOD_DEAL, Maturity.MATURE,
                          100_000, 150_000, 3_333, None, ("short_history",), "new", T)  # fmt: skip
    assert repo.insert_alert(db, alert) is not None
    assert repo.insert_alert(db, alert) is None


def test_claim_outbox_never_delivers_twice(db: psycopg.Connection) -> None:
    first = repo.enqueue(db, "reply", "<b>hola</b>", priority=1, dedup_key="reply:1", now=T,
                         ttl=24 * HOUR)  # fmt: skip
    assert repo.enqueue(db, "reply", "<b>hola</b>", priority=1, dedup_key="reply:1", now=T,
                        ttl=24 * HOUR) == first  # fmt: skip
    repo.enqueue(
        db, "alert", "<b>oferta</b>", priority=0, dedup_key="alert:9", now=T, ttl=12 * HOUR
    )
    claimed = repo.claim_outbox(db, 10, T)
    assert [r.dedup_key for r in claimed] == ["alert:9", "reply:1"]
    assert repo.claim_outbox(db, 10, T) == []
    repo.mark_sent(db, claimed[0].id, 4242, T)
    repo.mark_retry(db, claimed[1].id, "net_error")
    assert [r.dedup_key for r in repo.claim_outbox(db, 10, T)] == ["reply:1"]


def test_recover_stuck_sending(db: psycopg.Connection) -> None:
    repo.enqueue(db, "reply", "x", priority=1, dedup_key="reply:2", now=T, ttl=24 * HOUR)
    assert len(repo.claim_outbox(db, 10, T)) == 1
    assert repo.recover_outbox(db, T + 11 * timedelta(minutes=1)) == 1
    assert len(repo.claim_outbox(db, 10, T + 12 * timedelta(minutes=1))) == 1


def test_breaker_roundtrip_and_hour_stats(db: psycopg.Connection) -> None:
    b = BreakerState("open", T + HOUR, 2, 0, 0, 0, T, "blocked")
    repo.save_breaker(db, "amazon_us", b)
    assert repo.load_breakers(db)["amazon_us"] == b
    hour = T.replace(minute=0)
    repo.add_hour_stat(db, "amazon_us", hour, HourStat(3, 2, 1, 0, 0, 0))
    repo.add_hour_stat(db, "amazon_us", hour, HourStat(2, 2, 0, 0, 1, 0))
    assert repo.load_hour_stats(db, "amazon_us", T)[-1] == HourStat(5, 4, 1, 0, 1, 0)


def test_watch_and_search_crud(db: psycopg.Connection) -> None:
    w = repo.create_watch(db, "exito", "id:123", "componentes", None, T)
    assert w.created and w.id > 0
    again = repo.create_watch(db, "exito", "id:123", "componentes", None, T)
    assert not again.created and again.id == w.id
    s = repo.create_search(db, "exito", "ssd 1tb", "componentes", ("carcasa",), None, 400_000, T)
    assert s > 0
    assert repo.drop_ref(db, f"b{s}") and repo.drop_ref(db, f"p{w.id}")
    assert not repo.drop_ref(db, "p999")


def test_load_plan_input(db: psycopg.Connection) -> None:
    from discountz.domain.scheduling import SourceBudget

    budgets = {"exito": SourceBudget("grupo_exito", 2, 2, 30, 1500, HOUR, 3 * HOUR, 50)}
    repo.create_watch(db, "exito", "id:1", "otros", None, T - HOUR)
    state = repo.upsert_listing(db, offer(external_id="9"), "otros", URL, T)
    repo.bump_hot(db, state.id, T + HOUR)
    inp = repo.load_plan_input(db, T, budgets, (), 5 * timedelta(minutes=1))
    assert [w.pending for w in inp.watches] == [True]
    assert inp.hot_listings == [("exito", state.id)]
    assert "exito" in inp.breakers and inp.discovery_paused is False
