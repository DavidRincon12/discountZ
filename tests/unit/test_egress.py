"""Estimación del egress diario a partir de las filas que leyó el rol (ADR 0020)."""

from __future__ import annotations

from datetime import timedelta

from discountz.app.maintenance import egress_mb_per_day, egress_too_high
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
