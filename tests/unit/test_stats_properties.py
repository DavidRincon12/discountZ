"""Propiedades del spec §3.11 sobre los estadísticos (Hypothesis)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from discountz.domain.model import SellerClass
from discountz.domain.params import DEFAULT_CATEGORIES, GlobalParams
from discountz.domain.segments import Segment, SegmentStatus
from discountz.domain.stats import (
    compute_stats,
    is_power10_shift,
    rarity_bp,
    real_saving_bp,
    wq,
)

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
H = timedelta(hours=12)
GLOB = GlobalParams()
CAT = DEFAULT_CATEGORIES["componentes"]

prices = st.integers(min_value=1_000, max_value=5_000_000)
points_st = st.lists(
    st.tuples(prices, st.integers(min_value=1, max_value=10**7)), min_size=1, max_size=20
)


@st.composite
def series(draw: st.DrawFn) -> list[Segment]:
    """Segmentos contiguos de ≥ 6 h que terminan en T (el actual empieza en T)."""
    n = draw(st.integers(min_value=1, max_value=8))
    hours = draw(st.lists(st.integers(min_value=6, max_value=600), min_size=n, max_size=n))
    ps = draw(st.lists(prices, min_size=n, max_size=n))
    out: list[Segment] = []
    end = T
    for h, p in zip(reversed(hours), reversed(ps), strict=True):
        start = end - timedelta(hours=h)
        out.append(
            Segment(
                1,
                start,
                end - timedelta(minutes=30),
                end,
                p,
                None,
                True,
                SellerClass.FIRST_PARTY,
                "un|1",
                SegmentStatus.OPEN,
                3,
            )
        )
        end = start
    return list(reversed(out))


def stats_of(segs: list[Segment]) -> tuple[object, ...]:
    s = compute_stats(segs, T, T, CAT, GLOB, H, None, None, None)
    return (
        s.r,
        s.m90,
        s.m30,
        s.m_prior,
        s.m_rec,
        s.q10,
        s.q90,
        s.v_bp,
        s.coverage_s,
        s.days,
        s.m_robust,
        s.raw_min,
        s.max_charged,
        s.anchor,
        sorted_points(s.points),
    )


def sorted_points(points: tuple[tuple[int, int], ...]) -> list[tuple[int, int]]:
    merged: dict[int, int] = {}
    for p, w in points:
        merged[p] = merged.get(p, 0) + w
    return sorted(merged.items())


@given(series(), st.data())
def test_p1_splitting_a_segment_changes_nothing(segs: list[Segment], data: st.DataObject) -> None:
    i = data.draw(st.integers(min_value=0, max_value=len(segs) - 1))
    s = segs[i]
    assert s.ended_at is not None
    length_h = int((s.ended_at - s.started_at).total_seconds() // 3600)
    if length_h < 12:
        return
    cut_h = data.draw(st.integers(min_value=6, max_value=length_h - 6))
    cut = s.started_at + timedelta(hours=cut_h)
    first = replace(s, last_seen_at=cut - timedelta(minutes=30), ended_at=cut)
    second = replace(s, started_at=cut)
    split = [*segs[:i], first, second, *segs[i + 1 :]]
    assert stats_of(split) == stats_of(segs)


@given(
    points_st,
    st.integers(min_value=1, max_value=10_000),
    st.integers(min_value=1, max_value=10_000),
)
def test_p2_quantile_is_a_price_and_monotonic(pts: list[tuple[int, int]], q1: int, q2: int) -> None:
    lo, hi = sorted((q1, q2))
    a, b = wq(pts, lo), wq(pts, hi)
    assert a in {p for p, _ in pts}
    assert a is not None and b is not None and a <= b


@given(series())
def test_p3_reference_never_exceeds_m90(segs: list[Segment]) -> None:
    s = compute_stats(segs, T, T, CAT, GLOB, H, None, None, None)
    if s.r is not None:
        assert s.m90 is not None and s.r <= s.m90


@given(points_st, prices, prices)
def test_p4_lower_price_never_lowers_saving_nor_raises_rarity(
    pts: list[tuple[int, int]], p1: int, p2: int
) -> None:
    lo, hi = sorted((p1, p2))
    r = wq(pts, 5000)
    assert r is not None
    assert real_saving_bp(r, lo) >= real_saving_bp(r, hi)
    assert rarity_bp(pts, lo) <= rarity_bp(pts, hi)


@given(
    points_st,
    st.integers(min_value=1, max_value=1000),
    st.integers(min_value=1, max_value=10_000),
    prices,
)
def test_p5_scaling_weights_changes_neither_quantile_nor_rarity(
    pts: list[tuple[int, int]], c: int, q: int, p: int
) -> None:
    scaled = [(price, w * c) for price, w in pts]
    assert wq(scaled, q) == wq(pts, q)
    assert rarity_bp(scaled, p) == rarity_bp(pts, p)


@given(series(), st.data())
def test_p6_suspect_or_glitch_segments_are_ignored(
    segs: list[Segment], data: st.DataObject
) -> None:
    status = data.draw(st.sampled_from([SegmentStatus.SUSPECT, SegmentStatus.GLITCH]))
    price = data.draw(prices)
    i = data.draw(st.integers(min_value=0, max_value=len(segs) - 1))
    s = segs[i]
    noisy = [*segs[:i], replace(s, price_minor=price, status=status), *segs[i + 1 :]]
    clean = [*segs[:i], *segs[i + 1 :]]
    a = compute_stats(noisy, T, T, CAT, GLOB, H, None, None, None)
    b = compute_stats(clean, T, T, CAT, GLOB, H, None, None, None)
    assert (a.r, a.m_robust, a.points) == (b.r, b.m_robust, b.points)


@given(prices, prices)
def test_p8_power10_shift_is_symmetric(a: int, b: int) -> None:
    assert is_power10_shift(a, b) == is_power10_shift(b, a)
