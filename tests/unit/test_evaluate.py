"""Tabla de casos U01–U37 del spec §3.11 sobre `evaluate()`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.evaluate import (
    AlertRecord,
    EvalContext,
    Evaluation,
    ListingState,
    WatchTerms,
    evaluate,
)
from discountz.domain.guards import check_offer
from discountz.domain.model import AlertKind, Flag, Maturity, SellerClass
from discountz.domain.money import Currency
from discountz.domain.params import DEFAULT_CATEGORIES, GlobalParams
from discountz.domain.segments import Segment
from tests.factories import offer
from tests.unit.series import T, seg

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
GLOB = GlobalParams()
Origin = Literal["detail", "search", "lead"]


def ctx(
    segments: Sequence[Segment],
    price: int | None,
    *,
    category: str = "componentes",
    currency: Currency = Currency.COP,
    origin: Origin = "detail",
    target: int | None = None,
    watched: bool = False,
    list_price: int | None = None,
    seller: SellerClass = SellerClass.FIRST_PARTY,
    unit: str = "un|1",
    in_stock: bool = True,
    peers: Sequence[int] | None = None,
    lead_price: int | None = None,
    has_route: bool = True,
    nominal: timedelta = HOUR,
    now: datetime = T,
    hot_until: datetime | None = None,
    last_alert: AlertRecord | None = None,
) -> EvalContext:
    watch = WatchTerms(target, now - DAY if target else None) if (watched or target) else None
    return EvalContext(
        now=now,
        offer=offer(
            price=price,
            list_price=list_price,
            currency=currency,
            seller_class=seller,
            unit_key=unit,
            in_stock=in_stock,
            observed_at=now,
        ),
        origin=origin,
        segments=tuple(segments),
        current_started_at=now,
        listing=ListingState(1, category, None, None, None, hot_until),
        cat=DEFAULT_CATEGORIES[category],
        glob=GLOB,
        nominal_interval=nominal,
        watch=watch,
        peers=None if peers is None else tuple(peers),
        lead_price=lead_price,
        last_alert=last_alert,
        has_independent_route=has_route,
    )


@dataclass(frozen=True)
class Case:
    ctx: EvalContext
    verdict: AlertKind | None
    flags: frozenset[Flag] = frozenset()
    labels: frozenset[AlertKind] = frozenset()
    confirmation: str | None = None
    maturity: Maturity | None = None
    reject: str | None = None
    absent: frozenset[Flag] = field(default_factory=frozenset)


U01_SERIES = [seg("d-90", "d-21", 300_000), seg("d-21", "t", 400_000, list_price=400_000)]
U02_SERIES = [seg("d-40", "d-21", 300_000), seg("d-21", "t", 400_000)]
U20_SERIES = [seg("d-60", "d-10", 500_000), seg("d-10", "d-1", 350_000, in_stock=False)]
U29_SERIES = [
    seg("d-120", "d-40", 4_000_000),
    seg("d-40", "d-30", 3_920_000),
    seg("d-30", "d-20", 3_840_000),
    seg("d-20", "d-10", 3_760_000),
    seg("d-10", "t", 3_680_000),
]
SEP3 = datetime(2026, 9, 3, tzinfo=UTC)


def _hm(h: int, m: int) -> datetime:
    return SEP3.replace(hour=h, minute=m)


U07_SERIES = [
    seg("d-90", _hm(15, 41), 12_999, last_seen=_hm(13, 12)),
    seg(_hm(15, 41), _hm(17, 58), 8_999, last_seen=_hm(15, 41)),
    seg(_hm(17, 58), "t", 12_999),
]
U08_SERIES = [
    seg("d-90", _hm(15, 41), 12_999, last_seen=_hm(9, 30)),
    seg(_hm(15, 41), _hm(21, 50), 8_999, last_seen=_hm(15, 41)),
    seg(_hm(21, 50), "t", 12_999),
]


def alternating(a: int, ha: int, b: int, hb: int, start: datetime, end: datetime) -> list[Segment]:
    out: list[Segment] = []
    t, use_a = start, True
    while t < end:
        hours = ha if use_a else hb
        nxt = min(t + timedelta(hours=hours), end)
        out.append(seg(t, nxt, a if use_a else b))
        t, use_a = nxt, not use_a
    return out


PEERS_12 = [
    309_900,
    299_900,
    319_900,
    289_900,
    329_900,
    309_900,
    279_900,
    339_900,
    309_900,
    299_900,
    319_900,
    349_900,
]

CASES: dict[str, Case] = {
    "U01": Case(
        ctx(U01_SERIES, 300_000, list_price=400_000, watched=True),
        None,
        frozenset({Flag.PRE_INFLATED, Flag.ADVERTISED_GAP}),
    ),
    "U02": Case(ctx(U02_SERIES, 300_000, watched=True), None),
    "U03": Case(
        ctx(U01_SERIES, 255_000, list_price=400_000, watched=True),
        AlertKind.HISTORIC_LOW,
        frozenset({Flag.PRE_INFLATED}),
        labels=frozenset({AlertKind.GOOD_DEAL}),
    ),
    "U04": Case(ctx([seg("d-20", "t", 400_000)], 300_000), None, maturity=Maturity.WARM),
    "U05": Case(
        ctx([seg("d-20", "t", 400_000)], 300_000, watched=True),
        AlertKind.GOOD_DEAL,
        frozenset({Flag.SHORT_HISTORY}),
    ),
    "U06": Case(
        ctx([seg("d-90", "t", 279_900)], 279_900, list_price=499_900),
        None,
        frozenset({Flag.INFLATED_LIST, Flag.ADVERTISED_GAP}),
    ),
    "U07": Case(
        ctx(
            U07_SERIES,
            9_999,
            category="tecnologia",
            currency=Currency.USD,
            watched=True,
            nominal=3 * HOUR,
        ),
        AlertKind.HISTORIC_LOW,
        frozenset({Flag.FLASH_SEEN}),
    ),
    "U08": Case(
        ctx(
            U08_SERIES,
            9_999,
            category="tecnologia",
            currency=Currency.USD,
            origin="search",
            nominal=6 * HOUR,
        ),
        AlertKind.GOOD_DEAL,
        confirmation="independent",
        absent=frozenset({Flag.FLASH_SEEN}),
    ),
    "U09": Case(
        ctx(
            [seg("d-90", "t", 2_299_000, list_price=2_499_000)],
            229_900,
            category="tecnologia",
            list_price=2_499_000,
        ),
        AlertKind.PRICE_ERROR,
        confirmation="independent",
    ),
    "U10": Case(
        ctx(
            [seg("d-90", "t", 2_299_000, list_price=2_499_000)],
            229_900,
            category="tecnologia",
            list_price=2_499_000,
        ),
        AlertKind.PRICE_ERROR,
        confirmation="independent",
    ),
    "U12": Case(
        ctx(
            [seg("d-90", "t", 12_999)],
            1_299,
            category="tecnologia",
            currency=Currency.USD,
            has_route=False,
        ),
        AlertKind.PRICE_ERROR,
        confirmation="forbidden",
    ),
    "U13": Case(
        ctx([seg("d-90", "t", 1_599_000)], 159_900, category="tecnologia"),
        AlertKind.PRICE_ERROR,
        confirmation="independent",
    ),
    "U13b": Case(
        ctx(
            [seg("d-90", "t", 8_999)],
            899,
            category="tecnologia",
            currency=Currency.USD,
            has_route=False,
        ),
        AlertKind.PRICE_ERROR,
        confirmation="forbidden",
    ),
    "U15": Case(
        ctx([], 39_900, list_price=49_900, origin="search", peers=PEERS_12),
        None,
        maturity=Maturity.COLD,
    ),
    "U16": Case(
        ctx([], 99_900, list_price=319_900, origin="search", peers=PEERS_12),
        AlertKind.PRICE_ERROR,
        frozenset({Flag.SHORT_HISTORY}),
        confirmation="independent",
    ),
    "U17": Case(
        ctx([], 219_900, category="tecnologia", list_price=2_499_000, target=1_000_000),
        AlertKind.TARGET_REACHED,
        confirmation="independent",
    ),
    "U18": Case(
        ctx([seg("d-90", "d-30", 300_000)], 240_000),
        AlertKind.HISTORIC_LOW,
        frozenset({Flag.GAP_BEFORE}),
    ),
    "U20": Case(ctx(U20_SERIES, 499_000), None),
    "U21": Case(ctx(U20_SERIES, 350_000), AlertKind.HISTORIC_LOW),
    "U22": Case(ctx(U20_SERIES, 350_000, in_stock=False), None, reject="out_of_stock"),
    "U23": Case(
        ctx(
            [seg("d-90", "t", 12_999)],
            8_999,
            category="tecnologia",
            currency=Currency.USD,
            seller=SellerClass.MARKETPLACE,
            watched=True,
        ),
        None,
        frozenset({Flag.MARKETPLACE}),
    ),
    "U24": Case(
        ctx(
            [seg("d-90", "t", 12_999)],
            8_999,
            category="tecnologia",
            currency=Currency.USD,
            seller=SellerClass.MARKETPLACE,
            target=10_000,
        ),
        AlertKind.TARGET_REACHED,
        frozenset({Flag.MARKETPLACE}),
    ),
    "U25a": Case(
        ctx([seg("d-90", "t", 400_000)], None, in_stock=False), None, reject="not_buyable"
    ),
    "U25b": Case(ctx([seg("d-90", "t", 250_000)], 250_000), None),
    "U26": Case(
        ctx([seg("d-90", "t", 25_000)], 12_500, category="otros", unit="g|0.5"),
        None,
        frozenset({Flag.UNIT_CHANGED}),
        reject="unit_changed",
    ),
    "U27": Case(
        ctx(alternating(300_000, 24, 240_000, 24, T - 90 * DAY - 10 * HOUR, T), 240_000), None
    ),
    "U28": Case(ctx(alternating(300_000, 26, 240_000, 22, T - 90 * DAY, T), 240_000), None),
    "U29": Case(ctx(U29_SERIES, 3_600_000, category="tecnologia"), AlertKind.HISTORIC_LOW),
    "U30": Case(ctx(U29_SERIES[:4], 3_680_000, category="tecnologia", now=T - 10 * DAY), None),
    "U31": Case(
        ctx([seg("d-365", "t", 4_999_000)], 4_799_000, category="tecnologia"),
        AlertKind.HISTORIC_LOW,
    ),
    "U32": Case(ctx([seg("d-365", "t", 4_999_000)], 4_899_000, category="tecnologia"), None),
    "U33": Case(ctx(alternating(300_000, 42, 255_000, 6, T - 90 * DAY, T), 255_000), None),
    "U34": Case(
        ctx([], 7_499, currency=Currency.USD, origin="lead", lead_price=7_499),
        AlertKind.COMMUNITY_DEAL,
        frozenset({Flag.SHORT_HISTORY, Flag.COMMUNITY}),
        confirmation="none",
    ),
    "U34b": Case(ctx([], 8_999, currency=Currency.USD, origin="lead", lead_price=7_499), None),
    "U35": Case(
        ctx([seg("d-90", "t", 1_899_000)], 189_900, category="tecnologia"),
        AlertKind.PRICE_ERROR,
        confirmation="independent",
    ),
    "U36": Case(
        ctx(
            [seg("d-90", "t", 22_900)],
            2_400,
            category="otros",
            currency=Currency.USD,
            has_route=False,
        ),
        AlertKind.PRICE_ERROR,
        confirmation="persistence",
    ),
}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_evaluate_table(case_id: str) -> None:
    case = CASES[case_id]
    ev: Evaluation = evaluate(case.ctx)
    assert ev.verdict == case.verdict, ev
    assert case.flags <= ev.flags, ev.flags
    assert not (case.absent & ev.flags), ev.flags
    assert case.labels <= ev.labels, ev.labels
    if case.confirmation is not None:
        assert ev.confirmation == case.confirmation
    if case.maturity is not None:
        assert ev.maturity == case.maturity
    if case.reject is not None:
        assert ev.reject_code == case.reject


def test_u11_list_price_shift_is_rejected_before_evaluation() -> None:
    o = offer(price=229_900, list_price=249_900)
    with pytest.raises(ParseError) as exc:
        check_offer(o, Currency.COP, 2_499_000)
    assert exc.value.code == "parse_shift"


def test_u14_cop_text_read_as_usd_is_rejected() -> None:
    with pytest.raises(ParseError):
        check_offer(offer(price=52_000_000, currency=Currency.USD), Currency.USD, None)


def test_u21_label_days_and_coverage() -> None:
    ev = evaluate(CASES["U21"].ctx)
    assert ev.stats.coverage_s == 50 * 86_400
    assert ev.maturity == Maturity.MATURE


def test_u29_uses_trend_anchor() -> None:
    ev = evaluate(CASES["U29"].ctx)
    assert ev.stats.r == 3_760_000
    assert ev.stats.anchor == 4_000_000


def test_u30_reference_one_step_before() -> None:
    ev = evaluate(CASES["U30"].ctx)
    assert ev.stats.r == 3_840_000


def test_hot_flag_when_listing_is_hot() -> None:
    ev = evaluate(ctx([seg("d-90", "t", 300_000)], 200_000, hot_until=T + HOUR))
    assert Flag.HOT in ev.flags


def test_anchor_uses_last_non_target_alert() -> None:
    last = AlertRecord(AlertKind.GOOD_DEAL, 3_700_000, 4_000_000, T - 30 * DAY, None, "queued")
    ev = evaluate(ctx(U29_SERIES, 3_600_000, category="tecnologia", last_alert=last))
    assert ev.stats.anchor == 3_700_000


def test_target_above_typical_and_target_also() -> None:
    ev = evaluate(ctx(U01_SERIES, 255_000, target=350_000))
    assert ev.verdict == AlertKind.HISTORIC_LOW
    assert AlertKind.TARGET_REACHED in ev.labels
    assert {Flag.TARGET_ALSO, Flag.TARGET_ABOVE_TYPICAL} <= ev.flags


def test_jump_requires_confirmation_for_any_verdict() -> None:
    ev = evaluate(ctx([seg("d-2", "t", 300_000)], 140_000, target=200_000, has_route=False))
    assert ev.verdict == AlertKind.TARGET_REACHED
    assert ev.confirmation == "persistence"


def test_extended_window_lowers_maturity_and_flags_ref_antigua() -> None:
    series = [
        seg("d-300", "d-100", 500_000),
        seg("d-100", "d-5", 450_000, in_stock=False),
        seg("d-5", "t", 480_000),
    ]
    ev = evaluate(ctx(series, 380_000, watched=True))
    assert ev.maturity == Maturity.WARM
    assert Flag.REF_ANTIGUA in ev.flags
