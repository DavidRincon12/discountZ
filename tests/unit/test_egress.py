"""Estimación del egress diario a partir de las filas que leyó el rol (ADR 0020)."""

from __future__ import annotations

from datetime import date, timedelta

from discountz.app.maintenance import (
    billing_cycle_start,
    cycle_target_mb,
    egress_mb_per_day,
    egress_too_high,
    next_pace,
)
from discountz.domain.scheduling import paced_cap
from tests.unit.series import T

DAY = timedelta(days=1)


def test_a_million_rows_a_day_is_about_162_mb() -> None:
    assert egress_mb_per_day(5_000_000, T - DAY, 6_000_000, T) == 162


def test_rate_is_per_day_even_after_a_longer_gap() -> None:
    assert egress_mb_per_day(0, T - 2 * DAY, 2_000_000, T) == 162


def test_without_a_previous_reading_there_is_no_estimate() -> None:
    assert egress_mb_per_day(None, None, 6_000_000, T) is None


def test_reset_statistics_give_no_estimate() -> None:
    assert egress_mb_per_day(6_000_000, T - DAY, 10, T) is None


def test_less_than_12_hours_gives_no_estimate() -> None:
    assert egress_mb_per_day(0, T - timedelta(hours=11), 1_000_000, T) is None


def test_too_high_from_80_percent_of_5_gb_a_month() -> None:
    assert egress_too_high(133)  # 133 × 31 = 4.123 MB ≥ 4.096 (80 % de 5.120)
    assert not egress_too_high(132)


def test_pace_halves_when_egress_doubles_the_target() -> None:
    assert next_pace(10_000, 264, 132) == 5_000


def test_pace_recovers_up_to_full_speed() -> None:
    assert next_pace(5_000, 66, 132) == 10_000


def test_pace_never_drops_below_ten_percent() -> None:
    assert next_pace(1_000, 900, 132) == 1_000


def test_without_an_estimate_the_pace_stays() -> None:
    assert next_pace(7_000, None, 132) == 7_000


def test_zero_egress_does_not_divide_by_zero() -> None:
    assert next_pace(5_000, 0, 132) == 10_000


def test_paced_cap_scales_the_run() -> None:
    assert paced_cap(40, 5_000) == 20


def test_paced_cap_keeps_at_least_one_request() -> None:
    assert paced_cap(3, 1_000) == 1


def test_paced_cap_of_an_empty_run_stays_empty() -> None:
    assert paced_cap(0, 5_000) == 0


def test_billing_cycle_starts_on_the_last_28th() -> None:
    assert billing_cycle_start(date(2026, 11, 25)) == date(2026, 10, 28)
    assert billing_cycle_start(date(2026, 11, 28)) == date(2026, 11, 28)
    assert billing_cycle_start(date(2026, 1, 5)) == date(2025, 12, 28)


def test_a_clean_cycle_spreads_the_budget_evenly() -> None:
    assert cycle_target_mb(0, date(2026, 11, 28)) == 123  # 3.840 MB ÷ 31


def test_a_spent_cycle_slows_the_remaining_days() -> None:
    assert cycle_target_mb(3_000, date(2026, 12, 20)) == 105  # 840 MB ÷ 8 días


def test_the_target_never_exceeds_an_even_share_near_the_end() -> None:
    assert cycle_target_mb(0, date(2026, 12, 27)) == 123


def test_an_exhausted_budget_leaves_no_target() -> None:
    assert cycle_target_mb(3_840, date(2026, 12, 20)) == 0
    assert cycle_target_mb(5_000, date(2026, 12, 20)) == 0


def test_no_target_turns_the_crawl_off() -> None:
    assert next_pace(5_000, 200, 0) == 0


def test_a_new_cycle_restarts_the_crawl_from_the_floor() -> None:
    assert next_pace(0, 50, 123) == 2_460
    assert next_pace(0, None, 123) == 1_000


def test_paced_cap_with_the_crawl_off_is_empty() -> None:
    assert paced_cap(40, 0) == 0
