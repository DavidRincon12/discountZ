from datetime import UTC, datetime, timedelta

from discountz.domain.model import SellerClass
from discountz.domain.segments import (
    CloseAndOpen,
    Extend,
    Ignore,
    Observation,
    Segment,
    SegmentStatus,
    apply_sample,
    gap_h,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=12)


def obs(
    t: datetime,
    price: int | None = 100_000,
    *,
    in_stock: bool = True,
    lp: int | None = None,
    seller: SellerClass = SellerClass.FIRST_PARTY,
    unit: str = "un|1",
) -> Observation:
    return Observation(t, price, lp, in_stock, seller, unit)


def open_seg(
    price: int | None = 100_000,
    *,
    last_seen: datetime = T0,
    lp: int | None = None,
    status: SegmentStatus = SegmentStatus.OPEN,
) -> Segment:
    return Segment(
        1,
        T0 - timedelta(days=1),
        last_seen,
        None,
        price,
        lp,
        True,
        SellerClass.FIRST_PARTY,
        "un|1",
        status,
        5,
    )


def test_gap_h_is_max_of_12h_and_one_and_a_half_interval() -> None:
    assert gap_h(timedelta(minutes=60)) == timedelta(hours=12)
    assert gap_h(timedelta(hours=12)) == timedelta(hours=18)


def test_first_sample_opens_segment() -> None:
    action = apply_sample(None, obs(T0), H, SegmentStatus.OPEN)
    assert isinstance(action, CloseAndOpen)
    assert action.closed is None
    assert action.opened.started_at == T0 == action.opened.last_seen_at
    assert action.opened.sample_count == 1


def test_same_state_within_h_extends_and_keeps_max_list_price() -> None:
    action = apply_sample(
        open_seg(lp=150_000), obs(T0 + timedelta(hours=1), lp=180_000), H, SegmentStatus.OPEN
    )
    assert isinstance(action, Extend)
    assert action.segment.last_seen_at == T0 + timedelta(hours=1)
    assert action.segment.sample_count == 6
    assert action.segment.list_price_minor == 180_000


def test_price_change_closes_and_opens() -> None:
    t = T0 + timedelta(hours=1)
    action = apply_sample(open_seg(), obs(t, 90_000), H, SegmentStatus.OPEN)
    assert isinstance(action, CloseAndOpen)
    assert action.closed is not None and action.closed.ended_at == t
    assert action.opened.price_minor == 90_000 and action.opened.started_at == t


def test_gap_longer_than_h_closes_and_opens() -> None:
    action = apply_sample(open_seg(), obs(T0 + H + timedelta(seconds=1)), H, SegmentStatus.OPEN)
    assert isinstance(action, CloseAndOpen)


def test_status_change_closes_and_opens() -> None:
    action = apply_sample(
        open_seg(status=SegmentStatus.SUSPECT), obs(T0 + timedelta(hours=1)), H, SegmentStatus.OPEN
    )
    assert isinstance(action, CloseAndOpen)


def test_old_or_equal_observation_is_ignored() -> None:
    assert isinstance(apply_sample(open_seg(), obs(T0), H, SegmentStatus.OPEN), Ignore)
    assert isinstance(
        apply_sample(open_seg(), obs(T0 - timedelta(1)), H, SegmentStatus.OPEN), Ignore
    )


def test_u19_walmart_search_every_12h_is_one_segment() -> None:
    h = gap_h(timedelta(hours=12))
    gaps = [10.2, 13.8, 11.5, 12.9, 13.8, 10.2]
    seg: Segment | None = None
    t = T0
    opened = 0
    for i in range(60):  # ~30 d
        delay = timedelta(minutes=(i * 17) % 55)  # retraso del cron
        action = apply_sample(seg, obs(t + delay, 1_990), h, SegmentStatus.OPEN)
        if isinstance(action, CloseAndOpen):
            opened += 1
            seg = action.opened
        elif isinstance(action, Extend):
            seg = action.segment
        t += timedelta(hours=gaps[i % len(gaps)])
    assert opened == 1
