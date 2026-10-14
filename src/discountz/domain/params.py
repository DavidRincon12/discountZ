"""Parámetros del algoritmo por categoría y globales (spec §3.9)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType

from discountz.domain.money import Currency

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class CategoryParams:
    window: timedelta  # W
    mature_coverage: timedelta  # M_cat
    t_good_bp: int
    t_floor_bp: int
    d_low_bp: int
    d_floor_bp: int
    epsilon_bp: int
    g_bp: int
    f_max_bp: int
    cooldown: timedelta
    allow_marketplace: bool
    k: timedelta = 14 * DAY
    d_trend_bp: int = 1000


def _min_abs() -> Mapping[Currency, int]:
    return MappingProxyType({Currency.COP: 10_000, Currency.USD: 500})


@dataclass(frozen=True, slots=True)
class GlobalParams:
    h_min: timedelta = 12 * HOUR
    flash_max: timedelta = 6 * HOUR  # relámpago comprobado: cota superior < 6 h
    validity_min: timedelta = 7 * DAY  # validez de M30 y M_prior
    adapt_min_coverage: timedelta = 60 * DAY  # adaptación por volatilidad
    m30_window: timedelta = 30 * DAY
    recent_window: timedelta = 14 * DAY  # M_rec
    rho_bp: int = 800  # pre-inflado
    anchor_window: timedelta = 180 * DAY
    anchor_old_from: timedelta = 90 * DAY
    anchor_old_to: timedelta = 60 * DAY
    extended_window: timedelta = 365 * DAY
    retained_window: timedelta = 365 * DAY
    max_charged_min: timedelta = 24 * HOUR
    power10_tol_bp: int = 300
    confirm_tol_bp: int = 100
    persistence_gap: timedelta = 20 * timedelta(minutes=1)
    peers_min: int = 8
    peers_price_bp: int = 3500
    peers_list_bp: int = 6000
    storm_min: int = 2
    jump_window: timedelta = 7 * DAY
    gap_before: timedelta = 7 * DAY
    min_abs: Mapping[Currency, int] = field(default_factory=_min_abs)
    rarity_tol_bp: int = 50
    improve_bp: int = 300
    rearm: timedelta = 24 * HOUR
    glitch_reclass: timedelta = 7 * DAY
    cold_coverage: timedelta = 3 * DAY
    mature_days: int = 14


DEFAULT_CATEGORIES: Mapping[str, CategoryParams] = MappingProxyType(
    {
        "tecnologia": CategoryParams(
            90 * DAY, 21 * DAY, 1200, 800, 600, 300, 200, 5000, 1000, 72 * HOUR, False
        ),
        "componentes": CategoryParams(
            90 * DAY, 21 * DAY, 1000, 700, 500, 300, 200, 5000, 1000, 72 * HOUR, True
        ),
        "hogar": CategoryParams(
            90 * DAY, 21 * DAY, 2000, 1000, 800, 400, 200, 6000, 1000, 96 * HOUR, True
        ),
        "ropa": CategoryParams(
            60 * DAY,
            14 * DAY,
            3000,
            2000,
            1500,
            1000,
            300,
            7500,
            1500,
            120 * HOUR,
            True,
            k=10 * DAY,
            d_trend_bp=2000,
        ),
        "otros": CategoryParams(
            90 * DAY, 21 * DAY, 1500, 800, 600, 300, 200, 6000, 1000, 72 * HOUR, True
        ),
    }
)
