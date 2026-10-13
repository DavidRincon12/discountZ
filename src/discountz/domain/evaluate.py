"""Evaluación de una oferta contra su histórico: madurez, veredictos, confirmación y banderas.

Implementa spec §3.5 (madurez y veredictos), §3.6 (qué requiere confirmación) y §3.7 (banderas).
La oferta llega ya validada por `guards.check_offer` (G1–G5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from discountz.domain.guards import GuardResult, guard_flags
from discountz.domain.model import AlertKind, Flag, Maturity, Offer, SellerClass
from discountz.domain.money import Money
from discountz.domain.params import CategoryParams, GlobalParams
from discountz.domain.segments import Segment, gap_h
from discountz.domain.stats import (
    SECOND,
    HistoryStats,
    compute_stats,
    rarity_bp,
    real_saving_bp,
)

Origin = Literal["detail", "search", "lead"]
Confirmation = Literal["none", "independent", "persistence", "forbidden"]


@dataclass(frozen=True, slots=True)
class WatchTerms:
    target_minor: int | None
    target_set_at: datetime | None


@dataclass(frozen=True, slots=True)
class ListingState:
    id: int
    category: str
    alltime_min_minor: int | None
    history_reset_at: datetime | None
    last_price_minor: int | None
    hot_until: datetime | None


@dataclass(frozen=True, slots=True)
class AlertRecord:
    kind: AlertKind
    price_minor: int
    reference_minor: int | None
    created_at: datetime
    segment_started_at: datetime | None
    delivery: str


@dataclass(frozen=True, slots=True)
class EvalContext:
    now: datetime
    offer: Offer
    origin: Origin
    segments: Sequence[Segment]  # histórico SIN el segmento actual
    current_started_at: datetime  # s_cur
    listing: ListingState
    cat: CategoryParams
    glob: GlobalParams
    nominal_interval: timedelta
    watch: WatchTerms | None
    peers: Sequence[int] | None  # precios de otras ofertas en stock de la misma búsqueda
    lead_price: int | None
    last_alert: AlertRecord | None
    has_independent_route: bool


@dataclass(frozen=True, slots=True)
class Evaluation:
    verdict: AlertKind | None
    labels: frozenset[AlertKind]
    flags: frozenset[Flag]
    stats: HistoryStats
    maturity: Maturity
    confirmation: Confirmation
    reject_code: str | None


def evaluate(ctx: EvalContext) -> Evaluation:
    offer, cat, glob = ctx.offer, ctx.cat, ctx.glob
    h = gap_h(ctx.nominal_interval)
    stats = compute_stats(
        ctx.segments,
        ctx.now,
        ctx.current_started_at,
        cat,
        glob,
        h,
        ctx.listing.history_reset_at,
        ctx.listing.alltime_min_minor,
        _anchor_price(ctx),
        unit_key=offer.unit_key,
    )
    maturity = _maturity(stats, cat, glob)
    prev = _previous_segment(ctx.segments, ctx.current_started_at)
    guards = guard_flags(offer, prev, stats, cat.allow_marketplace)
    flags = set(_flags(ctx, stats, maturity))
    if GuardResult.UNIT_CHANGED in guards:
        flags.add(Flag.UNIT_CHANGED)
        return _none(stats, maturity, flags, "unit_changed")
    if GuardResult.NOT_BUYABLE in guards:
        return _none(stats, maturity, flags, "not_buyable")
    if GuardResult.OUT_OF_STOCK in guards:
        return _none(stats, maturity, flags, "out_of_stock")
    if offer.price is None:  # inalcanzable: G8 ya lo cubre; estrecha el tipo para mypy
        return _none(stats, maturity, flags, "not_buyable")
    p = offer.price.minor
    restricted = GuardResult.SELLER_RESTRICTED in guards
    kinds = _candidate_kinds(ctx, stats, maturity, offer.price, restricted)
    if not kinds:
        return _none(stats, maturity, flags, None)
    verdict = max(kinds, key=lambda k: k.rank)
    labels = frozenset(kinds - {verdict})
    if AlertKind.TARGET_REACHED in labels:
        flags.add(Flag.TARGET_ALSO)
    if verdict is AlertKind.COMMUNITY_DEAL:
        flags.add(Flag.COMMUNITY)
    return Evaluation(
        verdict=verdict,
        labels=labels,
        flags=frozenset(flags),
        stats=stats,
        maturity=maturity,
        confirmation=_confirmation(ctx, verdict, guards, maturity, p),
        reject_code=None,
    )


def _none(
    stats: HistoryStats, maturity: Maturity, flags: set[Flag], code: str | None
) -> Evaluation:
    return Evaluation(None, frozenset(), frozenset(flags), stats, maturity, "none", code)


def _anchor_price(ctx: EvalContext) -> int | None:
    last = ctx.last_alert
    if last is None or last.kind is AlertKind.TARGET_REACHED:
        return None
    if ctx.now - last.created_at > ctx.glob.anchor_window:
        return None
    return last.price_minor


def _previous_segment(segments: Sequence[Segment], s_cur: datetime) -> Segment | None:
    before = [s for s in segments if s.started_at < s_cur]
    return max(before, key=lambda s: s.started_at) if before else None


def _s(td: timedelta) -> int:
    return td // SECOND


def _maturity(stats: HistoryStats, cat: CategoryParams, glob: GlobalParams) -> Maturity:
    c = stats.coverage_s
    if c < _s(glob.cold_coverage):
        return Maturity.COLD
    if c >= _s(cat.mature_coverage) and stats.days >= glob.mature_days and not stats.extended:
        return Maturity.MATURE
    return Maturity.WARM


def _thresholds(stats: HistoryStats, cat: CategoryParams, glob: GlobalParams) -> tuple[int, int]:
    """(T_eff, D_eff): adaptados a la volatilidad propia solo con C ≥ 60 d."""
    if stats.coverage_s < _s(glob.adapt_min_coverage) or stats.v_bp is None:
        return cat.t_good_bp, cat.d_low_bp
    v = stats.v_bp
    return (
        max(cat.t_floor_bp, min(cat.t_good_bp, 2 * v)),
        max(cat.d_floor_bp, min(cat.d_low_bp, v)),
    )


def _seller_admitted(offer: Offer, cat: CategoryParams) -> bool:
    return offer.seller_class is SellerClass.FIRST_PARTY or (
        cat.allow_marketplace and offer.seller_class is SellerClass.MARKETPLACE
    )


def _candidate_kinds(
    ctx: EvalContext, stats: HistoryStats, maturity: Maturity, price: Money, restricted: bool
) -> set[AlertKind]:
    offer, cat, glob = ctx.offer, ctx.cat, ctx.glob
    p = price.minor
    kinds: set[AlertKind] = set()
    target = ctx.watch.target_minor if ctx.watch else None
    if target is not None and p <= target:
        kinds.add(AlertKind.TARGET_REACHED)
    if restricted:
        return kinds  # G14: solo TARGET_REACHED

    r, m_robust = stats.r, stats.m_robust
    t_eff, d_eff = _thresholds(stats, cat, glob)
    abs_ok = r is not None and r - p >= glob.min_abs[price.currency]
    s_real = real_saving_bp(r, p) if r else None
    rarity = rarity_bp(stats.points, p, glob.rarity_tol_bp)

    if offer.seller_class is SellerClass.FIRST_PARTY:
        if maturity is Maturity.COLD:
            if _peers_price_error(ctx, p):
                kinds.add(AlertKind.PRICE_ERROR)
        elif (
            r is not None
            and p * 10_000 <= r * (10_000 - cat.g_bp)
            and (m_robust is None or p * 10_000 <= m_robust * 7_000)
        ):
            kinds.add(AlertKind.PRICE_ERROR)

    if maturity is Maturity.MATURE and abs_ok and m_robust is not None and s_real is not None:
        trend = stats.anchor is not None and p * 10_000 <= stats.anchor * (10_000 - cat.d_trend_bp)
        if p * 10_000 <= m_robust * (10_000 - cat.epsilon_bp) and (s_real >= d_eff or trend):
            kinds.add(AlertKind.HISTORIC_LOW)

    if abs_ok and s_real is not None and rarity <= cat.f_max_bp:
        mature_ok = maturity is Maturity.MATURE and s_real >= t_eff
        watch_warm_ok = (
            maturity is Maturity.WARM and ctx.watch is not None and s_real >= t_eff + 1000
        )  # descubierto en warm: nunca
        if mature_ok or watch_warm_ok:
            kinds.add(AlertKind.GOOD_DEAL)

    if (
        ctx.origin == "lead"
        and ctx.lead_price is not None
        and _seller_admitted(offer, cat)
        and p * 10_000 <= ctx.lead_price * 10_100
    ):
        kinds.add(AlertKind.COMMUNITY_DEAL)
    return kinds


def _peers_price_error(ctx: EvalContext, p: int) -> bool:
    """PRICE_ERROR en frío por pares de la misma respuesta de búsqueda."""
    glob, offer = ctx.glob, ctx.offer
    peers = sorted(ctx.peers or ())
    if len(peers) < glob.peers_min or offer.list_price is None:
        return False
    med = peers[(len(peers) - 1) // 2]  # mediana inferior sin ponderar
    lp = offer.list_price.minor
    return (
        p * 10_000 <= med * glob.peers_price_bp
        and lp * 10_000 >= med * glob.peers_list_bp
        and p * 10_000 <= lp * (10_000 - ctx.cat.g_bp)
    )


def _confirmation(
    ctx: EvalContext,
    verdict: AlertKind,
    guards: frozenset[GuardResult],
    maturity: Maturity,
    p: int,
) -> Confirmation:
    route: Confirmation = "independent" if ctx.has_independent_route else "persistence"
    if verdict is AlertKind.PRICE_ERROR and GuardResult.DECIMAL_SHIFT in guards:
        return "independent" if ctx.has_independent_route else "forbidden"
    if verdict is AlertKind.PRICE_ERROR or GuardResult.JUMP in guards:
        return route
    if ctx.origin == "search" and verdict.rank >= AlertKind.GOOD_DEAL.rank:
        return route
    target = ctx.watch.target_minor if ctx.watch else None
    if (
        verdict is AlertKind.TARGET_REACHED
        and maturity is Maturity.COLD
        and target is not None
        and 2 * p <= target
    ):
        return route
    return "none"


def _flags(ctx: EvalContext, stats: HistoryStats, maturity: Maturity) -> set[Flag]:
    offer, glob = ctx.offer, ctx.glob
    flags: set[Flag] = set()
    p = offer.price.minor if offer.price is not None else None
    lp = offer.list_price.minor if offer.list_price is not None else None
    r = stats.r
    if (
        lp is not None
        and stats.max_charged is not None
        and stats.coverage_s >= _s(timedelta(days=21))
        and lp * 100 > stats.max_charged * 102
    ):
        flags.add(Flag.INFLATED_LIST)
    if p is not None and lp is not None and lp > p:
        s_adv = (lp - p) * 10_000 // lp
        s_real = real_saving_bp(r, p) if r else 0
        if s_adv - max(s_real, 0) >= 1500:
            flags.add(Flag.ADVERTISED_GAP)
    if (
        stats.m_rec is not None
        and stats.m_prior is not None
        and stats.m_rec * 10_000 >= stats.m_prior * (10_000 + glob.rho_bp)
    ):
        flags.add(Flag.PRE_INFLATED)
    if stats.raw_min is not None and stats.m_robust is not None and stats.raw_min < stats.m_robust:
        flags.add(Flag.FLASH_SEEN)
    last_seen = stats.last_eligible_seen
    if last_seen is not None and last_seen < ctx.current_started_at - glob.gap_before:
        flags.add(Flag.GAP_BEFORE)
    if maturity is not Maturity.MATURE:
        flags.add(Flag.SHORT_HISTORY)
    if offer.seller_class is SellerClass.MARKETPLACE:
        flags.add(Flag.MARKETPLACE)
    target = ctx.watch.target_minor if ctx.watch else None
    if target is not None and r is not None and r <= target:
        flags.add(Flag.TARGET_ABOVE_TYPICAL)
    if stats.extended:
        flags.add(Flag.REF_ANTIGUA)
    if ctx.listing.hot_until is not None and ctx.listing.hot_until > ctx.now:
        flags.add(Flag.HOT)
    return flags
