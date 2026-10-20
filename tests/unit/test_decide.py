"""Filas D01–D06 del spec §3.11, horas de silencio y resúmenes por categoría."""

from datetime import datetime, timedelta

from discountz.domain.decide import (
    PendingAlert,
    RearmEvidence,
    Send,
    Skip,
    decide,
    group_summaries,
)
from discountz.domain.evaluate import AlertRecord, Evaluation, ListingState
from discountz.domain.model import AlertKind, Maturity
from discountz.domain.params import DEFAULT_CATEGORIES, GlobalParams
from discountz.domain.stats import compute_stats
from tests.unit.series import T

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
COMP = DEFAULT_CATEGORIES["componentes"]
SEG = T - 2 * HOUR  # inicio del segmento actual
STATS = compute_stats([], T, T, COMP, GlobalParams(), 12 * HOUR, None, None, None)
NO_REARM = RearmEvidence(above_reference_s=0, above_target_s=0)
FULL_REARM = RearmEvidence(above_reference_s=2 * 86_400, above_target_s=2 * 86_400)


def ev(kind: AlertKind | None) -> Evaluation:
    return Evaluation(kind, frozenset(), frozenset(), STATS, Maturity.MATURE, "none", None)


def listing(muted_until: datetime | None = None) -> ListingState:
    return ListingState(1, "componentes", None, None, None, None, muted_until=muted_until)


def last(
    kind: AlertKind,
    ago: timedelta,
    price: int = 250_000,
    *,
    seg: datetime | None = None,
    delivery: str = "queued",
    ref: int = 300_000,
) -> AlertRecord:
    return AlertRecord(kind, price, ref, T - ago, seg, delivery)


def run(
    kind: AlertKind | None,
    prev: AlertRecord | None,
    price: int = 240_000,
    *,
    rearm: RearmEvidence = NO_REARM,
    now: datetime = T,
    muted_until: datetime | None = None,
) -> Send | Skip:
    return decide(
        ev(kind),
        prev,
        rearm,
        listing(muted_until),
        now,
        COMP,
        price_minor=price,
        segment_started_at=SEG,
    )


def test_no_verdict_skips() -> None:
    assert isinstance(run(None, None), Skip)


def test_first_alert_sends() -> None:
    assert run(AlertKind.GOOD_DEAL, None) == Send(AlertKind.GOOD_DEAL, silent=False)


def test_d01_same_segment_same_kind_skips() -> None:
    prev = last(AlertKind.GOOD_DEAL, HOUR, seg=SEG)
    assert isinstance(run(AlertKind.GOOD_DEAL, prev), Skip)


def test_d02_escalation_sends() -> None:
    prev = last(AlertKind.GOOD_DEAL, 20 * HOUR)
    assert isinstance(run(AlertKind.HISTORIC_LOW, prev), Send)


def test_d03_improvement_of_three_percent() -> None:
    prev = last(AlertKind.GOOD_DEAL, 30 * HOUR, price=250_000)
    assert isinstance(run(AlertKind.GOOD_DEAL, prev, 242_500), Send)
    assert isinstance(run(AlertKind.GOOD_DEAL, prev, 245_000), Skip)


def test_d04_not_rearmed_skips_even_after_cooldown() -> None:
    prev = last(AlertKind.GOOD_DEAL, 5 * DAY, price=250_000)
    assert isinstance(run(AlertKind.GOOD_DEAL, prev, 250_000), Skip)
    assert isinstance(run(AlertKind.GOOD_DEAL, prev, 250_000, rearm=FULL_REARM), Send)


def test_cooldown_blocks_even_when_rearmed() -> None:
    prev = last(AlertKind.GOOD_DEAL, 2 * DAY, price=250_000)
    assert isinstance(run(AlertKind.GOOD_DEAL, prev, 250_000, rearm=FULL_REARM), Skip)


def test_d05_new_target_rearms_target() -> None:
    prev = last(AlertKind.TARGET_REACHED, 2 * HOUR, price=240_000)
    reset = RearmEvidence(above_reference_s=0, above_target_s=0, target_reset=True)
    assert isinstance(run(AlertKind.TARGET_REACHED, prev, rearm=reset), Send)
    assert isinstance(run(AlertKind.TARGET_REACHED, prev), Skip)


def test_d06_muted_listing_is_recorded_as_muted() -> None:
    result = run(AlertKind.HISTORIC_LOW, None, muted_until=T + 3 * DAY)
    assert result == Skip("muted")


def test_suppressed_last_alert_is_ignored() -> None:
    prev = last(AlertKind.GOOD_DEAL, HOUR, delivery="suppressed")
    assert isinstance(run(AlertKind.GOOD_DEAL, prev), Send)


def test_old_last_alert_beyond_180_days_is_ignored() -> None:
    prev = last(AlertKind.GOOD_DEAL, 181 * DAY)
    assert isinstance(run(AlertKind.GOOD_DEAL, prev), Send)


def test_quiet_hours_silent_except_price_error() -> None:
    night = T.replace(hour=7)  # 02:00 en Bogotá
    assert run(AlertKind.GOOD_DEAL, None, now=night) == Send(AlertKind.GOOD_DEAL, silent=True)
    assert run(AlertKind.PRICE_ERROR, None, now=night) == Send(AlertKind.PRICE_ERROR, silent=False)
    day = T.replace(hour=13)  # 08:00 en Bogotá
    assert run(AlertKind.GOOD_DEAL, None, now=day) == Send(AlertKind.GOOD_DEAL, silent=False)


def pending(i: int, kind: AlertKind, cat: str) -> PendingAlert:
    return PendingAlert(alert_id=i, listing_id=100 + i, kind=kind, category=cat)


def test_summary_groups_three_good_deals() -> None:
    items = [
        pending(1, AlertKind.GOOD_DEAL, "componentes"),
        pending(2, AlertKind.COMMUNITY_DEAL, "componentes"),
        pending(3, AlertKind.GOOD_DEAL, "componentes"),
        pending(4, AlertKind.GOOD_DEAL, "hogar"),
        pending(5, AlertKind.HISTORIC_LOW, "componentes"),
    ]
    groups = group_summaries(items)
    assert [1, 2, 3] in [[p.alert_id for p in g] for g in groups]
    assert [[p.alert_id for p in g] for g in groups if g[0].category == "hogar"] == [[4]]
    assert [5] in [[p.alert_id for p in g] for g in groups]


def test_summary_groups_have_at_most_ten_items() -> None:
    items = [pending(i, AlertKind.GOOD_DEAL, "otros") for i in range(23)]
    sizes = sorted(len(g) for g in group_summaries(items))
    assert sizes == [3, 10, 10]


def test_storm_price_errors_of_one_store_go_in_one_message() -> None:
    def err(i: int, source: str, storm: bool = True) -> PendingAlert:
        return PendingAlert(i, 100 + i, AlertKind.PRICE_ERROR, "hogar", source, storm)

    groups = [
        [p.alert_id for p in g]
        for g in group_summaries([err(1, "olimpica"), err(2, "exito"), err(3, "olimpica")])
    ]
    assert [1, 3] in groups
    assert [2] in groups  # una sola de la tienda: va sola
    assert [
        [p.alert_id for p in g]
        for g in group_summaries([err(4, "olimpica", False), err(5, "olimpica", False)])
    ] == [[4], [5]]
