"""Reglas de bajada de precio del catálogo (spec del rastreo §3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from hypothesis import given
from hypothesis import strategies as st

from discountz.domain.catalog import (
    MIN_PEERS,
    MIN_READS,
    CatalogObservation,
    CatalogState,
    median_low,
    model_candidate,
    observe,
    split_price_band,
)
from discountz.domain.model import AlertKind, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.params import GlobalParams
from discountz.domain.topics import TopicParams

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
PROD = GlobalParams()
# Las reglas de siempre se prueban sin el precio mínimo de la calidad (2026-10-22): tiene las suyas.
GLOB = replace(PROD, min_price=MappingProxyType({Currency.COP: 0, Currency.USD: 0}))
TECH = TopicParams("tecnologia", "📱 Tecnología", 1200, 5000, "tecnologia")
MERCADO = TopicParams("mercado", "🛒 Mercado", 2500, 7000, "otros")


def obs(price: int, seller: SellerClass = SellerClass.FIRST_PARTY) -> CatalogObservation:
    return CatalogObservation("123", Money(price, Currency.COP), "/Tecnología/", None, seller)


def history(
    price: int, days: float, topic: TopicParams = TECH, glob: GlobalParams = GLOB
) -> tuple[CatalogState, datetime]:
    """Producto visto a `price` cada 6 h durante `days` días; devuelve el estado y el instante."""
    state: CatalogState | None = None
    t = T0
    steps = int(days * 4)
    for i in range(steps + 1):
        t = T0 + timedelta(hours=6 * i)
        state, cand, _ = observe(state, obs(price), t, topic, glob)
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


def test_c03_price_error_needs_warmup_too() -> None:
    """Sin los 3 días, el «antes» puede ser un precio de lanzamiento inflado."""
    state, t = history(99_900, 1)
    _, cand, _ = observe(state, obs(19_900), t + timedelta(hours=6), TECH, GLOB)
    assert cand is None
    state, t = history(99_900, 5)
    _, cand, _ = observe(state, obs(19_900), t + timedelta(hours=6), TECH, GLOB)
    assert cand is not None
    assert cand.kind is AlertKind.PRICE_ERROR
    assert cand.drop_bp == 8008


def test_marketplace_drop_past_the_error_threshold_never_alerts() -> None:
    """En el marketplace es un vendedor que juega con el precio (perfumes al −78 % en Carulla)."""
    state, t = history(99_900, 5)
    later = t + timedelta(hours=6)
    _, cand, _ = observe(state, obs(19_900, SellerClass.MARKETPLACE), later, TECH, GLOB)
    assert cand is None
    _, cand, _ = observe(state, obs(59_900, SellerClass.MARKETPLACE), later, TECH, GLOB)
    assert cand is not None
    assert cand.kind is AlertKind.CATALOG_DROP


def test_product_cheaper_than_min_price_never_alerts() -> None:
    """Precio mínimo de 100.000 sobre el «antes»: lo barato es mercado y bagatelas."""
    state, t = history(99_900, 5, glob=PROD)
    later = t + timedelta(hours=6)
    assert observe(state, obs(59_900), later, TECH, PROD)[1] is None
    assert observe(state, obs(19_900), later, TECH, PROD)[1] is None
    state, t = history(100_000, 5, glob=PROD)
    cand = observe(state, obs(60_000), t + timedelta(hours=6), TECH, PROD)[1]
    assert cand is not None
    assert cand.kind is AlertKind.CATALOG_DROP


def test_saving_under_30000_cop_never_alerts() -> None:
    state, t = history(150_000, 5, glob=PROD)
    later = t + timedelta(hours=6)
    assert observe(state, obs(120_001), later, TECH, PROD)[1] is None  # 29.999 (20 %)
    cand = observe(state, obs(120_000), later, TECH, PROD)[1]
    assert cand is not None
    assert cand.kind is AlertKind.CATALOG_DROP


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


def test_min_seen_older_than_90_days_no_longer_blocks_a_drop() -> None:
    """El mínimo es de 90 días (spec del rastreo §3.1): uno de hace meses no tapa la bajada."""
    state, t = history(59_900, 4)
    for i in range(1, 6):
        state, _, _ = observe(state, obs(99_900), t + timedelta(hours=6 * i), TECH, GLOB)
    later = t + timedelta(days=95)
    state, _, _ = observe(state, obs(99_900), later, TECH, GLOB)
    assert state.min_seen_minor == 99_900
    _, cand, _ = observe(state, obs(69_900), later + timedelta(hours=6), TECH, GLOB)
    assert cand is not None
    assert cand.kind is AlertKind.CATALOG_DROP


def test_min_seen_is_refreshed_while_the_price_stays_there() -> None:
    state, t = history(59_900, 100)  # 100 días estable al mínimo: sigue vigente
    assert state.min_seen_minor == 59_900
    assert state.min_seen_at == t


def test_absolute_saving_floor() -> None:
    state, t = history(50_000, 5)  # 15 % de 50.000 = 7.500 < 30.000
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


# ───────────── Precio bajo frente al mismo modelo (spec de eBay §4.2)


def usd(price: int) -> CatalogObservation:
    return CatalogObservation(
        "123456789", Money(price, Currency.USD), "/x/", None, SellerClass.FIRST_PARTY, "999"
    )


def alerted(minor: int, at: datetime) -> CatalogState:
    return CatalogState(minor, None, None, T0, T0, 1, None, at, minor)


def test_min_peers_is_five() -> None:
    assert MIN_PEERS == 5


def test_model_candidate_needs_five_peers() -> None:
    assert model_candidate(None, T0, usd(8_000), [10_000] * 4, TECH, GLOB) is None


def test_model_candidate_equal_to_median_is_none() -> None:
    assert model_candidate(None, T0, usd(10_000), [10_000] * 5, TECH, GLOB) is None


def test_model_candidate_below_threshold() -> None:
    cand = model_candidate(None, T0, usd(8_700), [10_000] * 5, TECH, GLOB)
    assert cand is not None
    assert cand.kind is AlertKind.MODEL_LOW
    assert cand.drop_bp == 1300
    assert cand.usual == Money(10_000, Currency.USD)
    assert cand.price == Money(8_700, Currency.USD)
    assert cand.min_days == 0


def test_model_candidate_above_threshold_is_none() -> None:
    assert model_candidate(None, T0, usd(8_900), [10_000] * 5, TECH, GLOB) is None


def test_model_candidate_min_abs() -> None:
    assert model_candidate(None, T0, usd(600), [1_000] * 5, TECH, GLOB) is None


def test_model_candidate_price_error() -> None:
    cand = model_candidate(None, T0, usd(4_000), [10_000] * 5, TECH, GLOB)
    assert cand is not None
    assert cand.kind is AlertKind.PRICE_ERROR


def test_model_candidate_marketplace_price_error_is_none() -> None:
    mkt = replace(usd(4_000), seller_class=SellerClass.MARKETPLACE)
    assert model_candidate(None, T0, mkt, [10_000] * 5, TECH, GLOB) is None


def test_model_candidate_median_below_min_price_is_none() -> None:
    assert model_candidate(None, T0, usd(1_600), [2_400] * 5, TECH, PROD) is None  # US$24
    assert model_candidate(None, T0, usd(1_600), [2_500] * 5, TECH, PROD) is not None


def test_model_candidate_no_threshold_topic() -> None:
    sin = TopicParams("errores", "Errores", None, None, "otros")
    assert model_candidate(None, T0, usd(4_000), [10_000] * 5, sin, GLOB) is None


def test_model_candidate_cooldown() -> None:
    state = alerted(8_700, T0 - timedelta(hours=1))
    assert model_candidate(state, T0, usd(8_650), [10_000] * 5, TECH, GLOB) is None
    assert model_candidate(state, T0, usd(8_300), [10_000] * 5, TECH, GLOB) is not None


@given(st.lists(st.integers(min_value=1, max_value=10**12), min_size=1, max_size=60))
def test_median_low_is_an_element_and_splits(values: list[int]) -> None:
    m = median_low(values)
    assert m in values
    assert sum(v < m for v in values) <= len(values) // 2
    assert sum(v > m for v in values) <= len(values) // 2 + 1


def test_crawl_max_items_only_for_ebay() -> None:
    from discountz.app.crawl import crawl_max_items
    from discountz.config import load_params

    params = load_params()
    assert crawl_max_items("ebay_us", params) == 150_000
    assert crawl_max_items("exito", params) is None


def test_model_candidate_same_price_after_cooldown_does_not_realert() -> None:
    """La mediana no converge como el «antes»: sin bajar más, no vuelve a alertar (spec §4.2)."""
    state = alerted(8_000, T0 - timedelta(hours=73))
    assert model_candidate(state, T0, usd(8_000), [10_000] * 5, TECH, GLOB) is None
    assert model_candidate(state, T0, usd(7_700), [10_000] * 5, TECH, GLOB) is not None
    old = alerted(8_000, T0 - timedelta(days=31))
    assert model_candidate(old, T0, usd(8_000), [10_000] * 5, TECH, GLOB) is not None


def test_cycle_quota_does_not_trip_breaker() -> None:
    import random as _random
    from collections import deque
    from typing import Any

    from discountz.app.collect import Collector, SourceRun
    from discountz.config import load_params
    from discountz.domain.errors import Unavailable
    from discountz.domain.health import BreakerState
    from discountz.sources.registry import load_all

    closed = BreakerState("closed", None, 0, 0, 0, 0)
    no_db: Any = None  # on_error no toca la BD
    c = Collector(
        no_db, T0, load_params(), {}, _random.Random(0), lambda: 0.0, 1e9, timedelta(minutes=5)
    )
    run = SourceRun(load_all()["ebay_us"], None, None, closed, deque())  # type: ignore[arg-type]
    c.on_error(run, Unavailable("ebay_us", "quota", 429))
    assert run.breaker.consecutive_unavailable == 0
    assert run.stopped
    c.on_error(run, Unavailable("ebay_us", "http_error", 500))
    assert run.breaker.consecutive_unavailable == 1


def test_alkosto_pauses_with_the_db_near_full() -> None:
    from discountz.app.crawl import PAUSED_AT_450

    assert "alkosto" in PAUSED_AT_450


def test_jumbo_pauses_with_the_db_near_full_but_not_pepeganga() -> None:
    from discountz.app.crawl import PAUSED_AT_450

    assert "jumbo" in PAUSED_AT_450
    assert "pepeganga" not in PAUSED_AT_450


def test_split_price_band_at_the_geometric_midpoint() -> None:
    assert split_price_band(1, 999_999_999) == [(1, 31_622), (31_623, 999_999_999)]
    assert split_price_band(100, 10_000) == [(100, 1_000), (1_001, 10_000)]
    assert split_price_band(5, 6) == [(5, 5), (6, 6)]


def test_split_price_band_does_not_split_a_single_price() -> None:
    assert split_price_band(5, 5) == []
