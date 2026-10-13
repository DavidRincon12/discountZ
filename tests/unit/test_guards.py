from dataclasses import replace
from datetime import timedelta

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.guards import (
    GuardResult,
    batch_is_constant,
    check_batch,
    check_currency_text,
    check_offer,
    check_storm,
    guard_flags,
    hard_tokens_changed,
)
from discountz.domain.model import ListingRef, Offer, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.params import DEFAULT_CATEGORIES, GlobalParams
from discountz.domain.segments import Segment, SegmentStatus
from discountz.domain.stats import HistoryStats, compute_stats
from tests.factories import T, offer

COP, USD = Currency.COP, Currency.USD
D = timedelta(days=1)


def prev_seg(
    price: int = 300_000,
    *,
    last_seen_ago: timedelta = timedelta(hours=1),
    in_stock: bool = True,
    unit: str = "un|1",
) -> Segment:
    return Segment(
        1,
        T - 30 * D,
        T - last_seen_ago,
        None,
        price,
        None,
        in_stock,
        SellerClass.FIRST_PARTY,
        unit,
        SegmentStatus.OPEN,
        10,
    )


def stats_with_r(r: int) -> HistoryStats:
    seg = Segment(
        1,
        T - 90 * D,
        T - timedelta(minutes=30),
        T,
        r,
        None,
        True,
        SellerClass.FIRST_PARTY,
        "un|1",
        SegmentStatus.OPEN,
        10,
    )
    return compute_stats(
        [seg],
        T,
        T,
        DEFAULT_CATEGORIES["componentes"],
        GlobalParams(),
        timedelta(hours=12),
        None,
        None,
        None,
    )


# G1: esquema / precio positivo
def test_g1_non_positive_price_raises() -> None:
    with pytest.raises(ParseError):
        check_offer(offer(price=0), COP, None)
    assert check_offer(offer(price=50_000), COP, None).price == Money(50_000, COP)


# G2: moneda
def test_g2_offer_currency_must_match_source() -> None:
    with pytest.raises(ParseError):
        check_offer(offer(price=50_000, currency=COP, source="amazon_us"), USD, None)
    assert check_offer(offer(price=9_999, currency=USD), USD, None).price is not None


def test_g2_cop_text_in_usd_source_raises() -> None:
    with pytest.raises(ParseError):
        check_currency_text("COP 520,000", USD)
    with pytest.raises(ParseError):
        check_currency_text("US$ 99.99", COP)
    check_currency_text("$74.99", USD)
    check_currency_text("$ 1.299.900", COP)


# G3: cotas absolutas
@pytest.mark.parametrize(
    ("price", "cur"), [(999, COP), (100_000_001, COP), (49, USD), (5_000_001, USD)]
)
def test_g3_out_of_bounds_raises(price: int, cur: Currency) -> None:
    with pytest.raises(ParseError):
        check_offer(offer(price=price, currency=cur), cur, None)


@pytest.mark.parametrize(
    ("price", "cur"), [(1_000, COP), (100_000_000, COP), (50, USD), (5_000_000, USD)]
)
def test_g3_bounds_are_inclusive(price: int, cur: Currency) -> None:
    assert check_offer(offer(price=price, currency=cur), cur, None).price is not None


def test_g3_u14_cop_amount_read_as_usd_cents_raises() -> None:
    with pytest.raises(ParseError):
        check_offer(offer(price=52_000_000, currency=USD), USD, None)


# G4: tachado incoherente
@pytest.mark.parametrize("lp", [100_000, 90_000, 2_000_001])
def test_g4_incoherent_list_price_is_dropped(lp: int) -> None:
    checked = check_offer(offer(price=100_000, list_price=lp), COP, None)
    assert checked.list_price is None
    assert checked.price == Money(100_000, COP)


def test_g4_coherent_list_price_is_kept() -> None:
    assert check_offer(offer(price=100_000, list_price=150_000), COP, None).list_price is not None


# G5: desplazamiento del tachado (U11)
def test_g5_list_price_shift_raises_parse_shift() -> None:
    with pytest.raises(ParseError) as exc:
        check_offer(offer(price=229_900, list_price=249_900), COP, 2_499_000)
    assert exc.value.code == "parse_shift"


def test_g5_normal_list_price_change_passes() -> None:
    assert check_offer(offer(price=229_900, list_price=249_900), COP, 260_000).list_price


# G6: unidad
def test_g6_unit_change_flag() -> None:
    o = offer(price=12_500, unit_key="g|0.5")
    assert GuardResult.UNIT_CHANGED in guard_flags(o, prev_seg(25_000), None, True)
    assert GuardResult.UNIT_CHANGED not in guard_flags(offer(), prev_seg(), None, True)


# G7: identidad por tokens duros
def test_g7_hard_tokens_changed() -> None:
    assert hard_tokens_changed("SSD Kingston NV2 1TB", "SSD Kingston NV2 2TB")
    assert hard_tokens_changed("Monitor LG 27GP850", "Monitor LG 27GN800")
    assert not hard_tokens_changed("SSD Kingston NV2 1TB", "Disco SSD Kingston NV2 1TB NVMe nuevo")


# G8: precio no comprable
def test_g8_missing_price_is_not_buyable() -> None:
    assert GuardResult.NOT_BUYABLE in guard_flags(
        offer(price=None, in_stock=False), None, None, True
    )
    assert GuardResult.NOT_BUYABLE not in guard_flags(offer(), None, None, True)


# G9: stock
def test_g9_out_of_stock() -> None:
    assert GuardResult.OUT_OF_STOCK in guard_flags(offer(in_stock=False), None, None, True)
    assert GuardResult.OUT_OF_STOCK not in guard_flags(offer(), None, None, True)


# G10: lote constante
def _batch(prices: list[int]) -> list[Offer]:
    return [offer(price=p, external_id=str(100 + i)) for i, p in enumerate(prices)]


def test_g10_constant_banner_price_discards_batch() -> None:
    offers = _batch([20_000] * 8)
    r = {o.ref: 1_500_000 for o in offers[:4]}
    assert batch_is_constant(offers)
    assert check_batch(offers, r)


def test_g10_constant_batch_without_history_is_kept() -> None:
    offers = _batch([20_000] * 8)
    assert batch_is_constant(offers)
    assert not check_batch(offers, {o.ref: None for o in offers})


def test_g10_varied_or_small_batches_are_kept() -> None:
    varied = _batch([20_000, 30_000, 45_000, 60_000, 20_000, 99_000])
    assert not batch_is_constant(varied)
    assert not check_batch(varied, {o.ref: 1_500_000 for o in varied})
    small = _batch([20_000] * 4)
    assert not check_batch(small, {o.ref: 1_500_000 for o in small})


def test_g10_constant_price_matching_history_is_kept() -> None:
    offers = _batch([20_000] * 6)
    assert not check_batch(offers, {o.ref: 21_000 for o in offers})


# G11: patrón decimal
def test_g11_decimal_shift() -> None:
    st = stats_with_r(2_299_000)
    assert GuardResult.DECIMAL_SHIFT in guard_flags(
        offer(price=229_900), prev_seg(2_299_000), st, True
    )
    assert GuardResult.DECIMAL_SHIFT not in guard_flags(
        offer(price=1_999_000), prev_seg(2_299_000), st, True
    )


# G12: tormenta
def test_g12_storm_needs_two_candidates() -> None:
    assert check_storm(2)
    assert not check_storm(1)


# G13: salto brusco
def test_g13_jump_against_recent_in_stock_price() -> None:
    assert GuardResult.JUMP in guard_flags(offer(price=150_000), prev_seg(300_000), None, True)
    assert GuardResult.JUMP in guard_flags(offer(price=600_000), prev_seg(300_000), None, True)
    assert GuardResult.JUMP not in guard_flags(offer(price=160_000), prev_seg(300_000), None, True)


def test_g13_ignores_old_or_out_of_stock_previous() -> None:
    old = prev_seg(300_000, last_seen_ago=8 * D)
    assert GuardResult.JUMP not in guard_flags(offer(price=100_000), old, None, True)
    oos = prev_seg(300_000, in_stock=False)
    assert GuardResult.JUMP not in guard_flags(offer(price=100_000), oos, None, True)


# G14: vendedor
def test_g14_seller_restrictions() -> None:
    unknown = offer(seller_class=SellerClass.UNKNOWN)
    market = offer(seller_class=SellerClass.MARKETPLACE)
    assert GuardResult.SELLER_RESTRICTED in guard_flags(unknown, None, None, True)
    assert GuardResult.SELLER_RESTRICTED in guard_flags(market, None, None, False)
    assert GuardResult.SELLER_RESTRICTED not in guard_flags(market, None, None, True)
    assert GuardResult.SELLER_RESTRICTED not in guard_flags(offer(), None, None, False)


def test_clean_offer_is_ok() -> None:
    assert guard_flags(offer(), prev_seg(100_000), None, True) == frozenset({GuardResult.OK})


def test_ref_type_is_listing_ref() -> None:
    assert replace(offer(), ref=ListingRef("exito", "1")).ref.external_id == "1"
