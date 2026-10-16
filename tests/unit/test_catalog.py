"""Reglas de bajada de precio del catálogo (spec del rastreo §3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from discountz.domain.catalog import (
    MIN_READS,
    CatalogObservation,
    CatalogState,
    observe,
)
from discountz.domain.model import AlertKind, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.params import GlobalParams
from discountz.domain.topics import TopicParams

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
GLOB = GlobalParams()
TECH = TopicParams("tecnologia", "📱 Tecnología", 1200, 5000, "tecnologia")
MERCADO = TopicParams("mercado", "🛒 Mercado", 2500, 7000, "otros")


def obs(price: int) -> CatalogObservation:
    return CatalogObservation(
        "123", Money(price, Currency.COP), "/Tecnología/", None, SellerClass.FIRST_PARTY
    )


def history(price: int, days: float, topic: TopicParams = TECH) -> tuple[CatalogState, datetime]:
    """Producto visto a `price` cada 6 h durante `days` días; devuelve el estado y el instante."""
    state: CatalogState | None = None
    t = T0
    steps = int(days * 4)
    for i in range(steps + 1):
        t = T0 + timedelta(hours=6 * i)
        state, cand, _ = observe(state, obs(price), t, topic, GLOB)
        assert cand is None
    assert state is not None
    return state, t


def test_c01_drop_after_warmup() -> None:
    state, t = history(99_900, 5)
    _, cand, changed = observe(state, obs(59_900), t + timedelta(hours=6), TECH, GLOB)
    assert changed
    assert cand is not None
    assert cand.kind is AlertKind.CATALOG_DROP
    assert cand.price == Money(59_900, Currency.COP)
    assert cand.usual == Money(99_900, Currency.COP)
    assert cand.drop_bp == 4004
    assert cand.min_days == 5


def test_c02_no_alert_before_three_days() -> None:
    state, t = history(99_900, 2)
    _, cand, _ = observe(state, obs(59_900), t + timedelta(hours=6), TECH, GLOB)
    assert cand is None


def test_c03_price_error_without_warmup() -> None:
    state, t = history(99_900, 1)
    _, cand, _ = observe(state, obs(19_900), t + timedelta(hours=6), TECH, GLOB)
    assert cand is not None
    assert cand.kind is AlertKind.PRICE_ERROR
    assert cand.drop_bp == 8008


def test_c04_first_observation_never_alerts() -> None:
    state, cand, changed = observe(None, obs(19_900), T0, TECH, GLOB)
    assert cand is None
    assert changed
    assert state.usual_minor is None
    assert state.first_seen_at == T0


def test_c05_single_read_spike_does_not_move_usual_or_min() -> None:
    state, t = history(99_900, 5)
    state, _, _ = observe(state, obs(89_900), t + timedelta(hours=6), TECH, GLOB)
    state, _, _ = observe(state, obs(99_900), t + timedelta(hours=12), TECH, GLOB)
    assert state.usual_minor == 99_900
    assert state.min_seen_minor == 99_900


def test_c06_cooldown_needs_three_percent_improvement() -> None:
    state, t = history(99_900, 5)
    state, cand, _ = observe(state, obs(59_900), t + timedelta(hours=1), TECH, GLOB)
    assert cand is not None
    alerted = t + timedelta(hours=1)
    state = replace(state, last_alert_at=alerted, last_alert_minor=59_900)
    _, cand, _ = observe(state, obs(58_900), alerted + timedelta(hours=10), TECH, GLOB)
    assert cand is None
    _, cand, _ = observe(state, obs(57_900), alerted + timedelta(hours=10), TECH, GLOB)
    assert cand is not None


def test_c08_small_saving_ignored() -> None:
    state, t = history(20_000, 5, MERCADO)
    _, cand, _ = observe(state, obs(18_500), t + timedelta(hours=6), MERCADO, GLOB)
    assert cand is None


def test_drop_must_beat_min_seen() -> None:
    state, t = history(59_900, 4)
    for i in range(1, 6):  # sube y se queda estable a 99.900: el mínimo sigue en 59.900
        state, _, _ = observe(state, obs(99_900), t + timedelta(hours=6 * i), TECH, GLOB)
    _, cand, _ = observe(state, obs(69_900), t + timedelta(hours=40), TECH, GLOB)
    assert cand is None


def test_absolute_saving_floor() -> None:
    state, t = history(50_000, 5)  # 15 % de 50.000 = 7.500 < 10.000
    _, cand, _ = observe(state, obs(42_500), t + timedelta(hours=6), TECH, GLOB)
    assert cand is None


def test_price_rise_updates_usual_only_after_stable() -> None:
    state, t = history(99_900, 5)
    state, _, _ = observe(state, obs(129_900), t + timedelta(hours=6), TECH, GLOB)
    assert state.usual_minor == 99_900
    state, _, _ = observe(state, obs(129_900), t + timedelta(hours=31), TECH, GLOB)
    assert state.usual_minor == 129_900
    assert state.min_seen_minor == 99_900


def test_unchanged_price_is_not_a_change() -> None:
    state, t = history(99_900, 1)
    _, _, changed = observe(state, obs(99_900), t + timedelta(hours=6), TECH, GLOB)
    assert not changed


@given(st.lists(st.integers(10_000, 2_000_000), min_size=1, max_size=MIN_READS))
def test_never_alerts_without_usual(prices: list[int]) -> None:
    """Con menos lecturas estables que `MIN_READS`, nunca hay `usual` y nunca hay alerta."""
    state: CatalogState | None = None
    for i, p in enumerate(prices):
        state, cand, _ = observe(state, obs(p), T0 + timedelta(minutes=i), TECH, GLOB)
        assert cand is None
