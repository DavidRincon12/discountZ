from datetime import UTC, datetime, timedelta

from discountz.domain.model import SellerClass
from discountz.domain.params import DEFAULT_CATEGORIES, GlobalParams
from discountz.domain.segments import Segment, SegmentStatus
from discountz.domain.stats import (
    compute_stats,
    is_power10_shift,
    rarity_bp,
    segment_weight_s,
    wq,
)

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
D = timedelta(days=1)
H = timedelta(hours=12)
GLOB = GlobalParams()
COMP = DEFAULT_CATEGORIES["componentes"]


def seg(
    start: datetime,
    end: datetime | None,
    price: int,
    *,
    last_seen: datetime | None = None,
    stock: bool = True,
    status: SegmentStatus = SegmentStatus.OPEN,
    seller: SellerClass = SellerClass.FIRST_PARTY,
    unit: str = "un|1",
) -> Segment:
    ls = last_seen or ((end - timedelta(minutes=30)) if end else T - timedelta(minutes=30))
    return Segment(1, start, ls, end, price, None, stock, seller, unit, status, 10)


def test_wq_lower_weighted_quantile() -> None:
    assert wq([(100, 1), (200, 1)], 5000) == 100
    assert wq([], 5000) is None
    assert wq([(300, 3), (100, 1)], 5000) == 300


def test_rarity_bp() -> None:
    assert rarity_bp([(240, 45), (300, 45)], 240) == 5000
    assert rarity_bp([], 240) == 10_000


def test_is_power10_shift() -> None:
    assert is_power10_shift(2_299_000, 229_900)
    assert is_power10_shift(229_900, 2_299_000)
    assert is_power10_shift(12_999, 1_299)
    assert not is_power10_shift(2_299_000, 1_299_000)


def test_segment_weight_caps_at_last_seen_plus_h_and_s_cur() -> None:
    s = seg(T - 10 * D, T - 5 * D, 100, last_seen=T - 9 * D)
    assert segment_weight_s(s, T - 90 * D, T, T, H, None) == int((D + H).total_seconds())
    open_s = seg(T - 2 * D, None, 100, last_seen=T - D)
    assert segment_weight_s(open_s, T - 90 * D, T, T - D - D / 2, H, None) == int(
        (D / 2).total_seconds()
    )
    assert segment_weight_s(s, T - 90 * D, T, T, H, T - 9 * D - H / 2) == int(
        (H + H / 2).total_seconds()
    )


def test_u02_reference_is_prior_median() -> None:
    segs = [seg(T - 40 * D, T - 21 * D, 300_000), seg(T - 21 * D, T, 400_000)]
    st = compute_stats(segs, T, T, COMP, GLOB, H, None, None, None)
    assert st.m90 == 400_000
    assert st.m_prior == 300_000
    assert st.r == 300_000
    assert rarity_bp(st.points, 300_000) == 4750
    assert st.coverage_s == 40 * 86_400


def test_coverage_excludes_out_of_stock_marketplace_and_other_units() -> None:
    segs = [
        seg(T - 60 * D, T - 30 * D, 500_000),
        seg(T - 30 * D, T - 20 * D, 400_000, stock=False),
        seg(T - 20 * D, T - 10 * D, 300_000, seller=SellerClass.UNKNOWN),
        seg(T - 10 * D, T - 5 * D, 200_000, status=SegmentStatus.GLITCH),
        seg(T - 5 * D, T, 100_000, unit="g|0.5"),
    ]
    st = compute_stats(segs, T, T, COMP, GLOB, H, None, None, None, unit_key="un|1")
    assert st.coverage_s == 30 * 86_400
    assert st.r == st.m90 == 500_000
    assert st.raw_min == st.m_robust == 500_000


def test_flash_seen_excluded_from_m_robust_u07() -> None:
    day = datetime(2026, 9, 3, tzinfo=UTC)
    segs = [
        seg(
            T - 90 * D,
            day.replace(hour=15, minute=41),
            12_999,
            last_seen=day.replace(hour=13, minute=12),
        ),
        seg(
            day.replace(hour=15, minute=41),
            day.replace(hour=17, minute=58),
            8_999,
            last_seen=day.replace(hour=15, minute=41),
        ),
        seg(day.replace(hour=17, minute=58), T, 12_999),
    ]
    tech = DEFAULT_CATEGORIES["tecnologia"]
    st = compute_stats(segs, T, T, tech, GLOB, H, None, None, None)
    assert st.raw_min == 8_999
    assert st.m_robust == 12_999


def test_m_robust_uses_alltime_min_and_ignores_current_segment() -> None:
    segs = [seg(T - 90 * D, T - D, 300_000), seg(T - D, None, 100_000)]
    st = compute_stats(segs, T, T - D, COMP, GLOB, H, None, 280_000, None)
    assert st.m_robust == 280_000
    assert st.raw_min == 300_000


def test_max_charged_requires_24h() -> None:
    segs = [
        seg(T - 90 * D, T - D, 279_900),
        seg(T - D, T - D + timedelta(hours=10), 499_900, last_seen=T - D + timedelta(hours=9)),
    ]
    st = compute_stats(segs, T, T, COMP, GLOB, H, None, None, None)
    assert st.max_charged == 279_900


def test_anchor_prefers_last_alert_then_old_median() -> None:
    segs = [seg(T - 120 * D, T, 4_000_000)]
    tech = DEFAULT_CATEGORIES["tecnologia"]
    assert compute_stats(segs, T, T, tech, GLOB, H, None, None, 3_900_000).anchor == 3_900_000
    assert compute_stats(segs, T, T, tech, GLOB, H, None, None, None).anchor == 4_000_000


def test_days_counts_bogota_calendar_days() -> None:
    # 2026-10-10 04:00Z..06:00Z cruza la medianoche de Bogotá (05:00Z): 2 días.
    start = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)
    segs = [seg(start, start + timedelta(hours=2), 100_000, last_seen=start + timedelta(hours=1))]
    st = compute_stats(segs, T, T, COMP, GLOB, H, None, None, None)
    assert st.days == 2


def test_extended_window_when_recent_coverage_is_short() -> None:
    segs = [
        seg(T - 300 * D, T - 100 * D, 500_000),
        seg(T - 100 * D, T - 5 * D, 450_000, stock=False),
        seg(T - 5 * D, T, 480_000),
    ]
    st = compute_stats(segs, T, T, COMP, GLOB, H, None, None, None)
    assert st.extended
    assert st.r == 500_000


def test_volatility_bp() -> None:
    segs = [seg(T - 90 * D, T - 45 * D, 100_000), seg(T - 45 * D, T, 120_000)]
    st = compute_stats(segs, T, T, COMP, GLOB, H, None, None, None)
    assert st.q10 == 100_000 and st.q90 == 120_000
    assert st.r == 100_000
    assert st.v_bp == 2_000
