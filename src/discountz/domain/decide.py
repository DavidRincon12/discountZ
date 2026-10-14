"""Deduplicación, cooldown, rearme, horas de silencio y resúmenes (spec §3.8). Puro."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from discountz.domain.evaluate import AlertRecord, Evaluation, ListingState
from discountz.domain.model import AlertKind
from discountz.domain.params import CategoryParams

BOGOTA = timezone(timedelta(hours=-5), "America/Bogota")
LOOKBACK = timedelta(days=180)
IMPROVE_BP = 300
REARM_S = 24 * 3600
QUIET_FROM, QUIET_TO = 23, 7  # horas de Bogotá
SUMMARY_MIN, SUMMARY_MAX = 3, 10
_SUMMARY_KINDS = frozenset({AlertKind.GOOD_DEAL, AlertKind.COMMUNITY_DEAL})


@dataclass(frozen=True, slots=True)
class RearmEvidence:
    above_reference_s: int  # Σ w_e de E posteriores a last con p_e ≥ ref·(1 − T_eff/2)
    above_target_s: int  # Σ w_e posteriores a last con p_e > objetivo
    target_reset: bool = False  # target_set_at > last.created_at


@dataclass(frozen=True, slots=True)
class Send:
    kind: AlertKind
    silent: bool


@dataclass(frozen=True, slots=True)
class Skip:
    reason: str  # "muted" ⇒ el llamador registra la alerta con delivery='muted'


@dataclass(frozen=True, slots=True)
class PendingAlert:
    alert_id: int
    listing_id: int
    kind: AlertKind
    category: str


def decide(
    ev: Evaluation,
    last: AlertRecord | None,
    rearm: RearmEvidence,
    listing: ListingState,
    now: datetime,
    cat: CategoryParams,
    *,
    price_minor: int,
    segment_started_at: datetime | None,
) -> Send | Skip:
    kind = ev.verdict
    if kind is None:
        return Skip("no_verdict")
    if listing.muted_until is not None and listing.muted_until > now:
        return Skip("muted")
    send = Send(kind, silent=_is_quiet(now) and kind is not AlertKind.PRICE_ERROR)
    if last is not None and last.delivery == "suppressed":
        last = None
    if last is not None and last.kind is kind and last.segment_started_at == segment_started_at:
        return Skip("duplicate")
    if last is None or now - last.created_at > LOOKBACK:
        return send
    if kind.rank > last.kind.rank:
        return send
    if price_minor * 10_000 <= last.price_minor * (10_000 - IMPROVE_BP):
        return send
    if kind is AlertKind.TARGET_REACHED and rearm.target_reset:
        return send
    if now - last.created_at >= cat.cooldown and _rearmed(kind, rearm):
        return send
    return Skip("cooldown")


def _rearmed(kind: AlertKind, rearm: RearmEvidence) -> bool:
    if kind is AlertKind.TARGET_REACHED:
        return rearm.above_target_s >= REARM_S or rearm.target_reset
    return rearm.above_reference_s >= REARM_S


def _is_quiet(now: datetime) -> bool:
    hour = now.astimezone(BOGOTA).hour
    return hour >= QUIET_FROM or hour < QUIET_TO


def group_summaries(sends: Sequence[PendingAlert]) -> list[list[PendingAlert]]:
    """≥ 3 good_deal/community_deal de una categoría ⇒ grupos de hasta 10; el resto, solos."""
    by_cat: defaultdict[str, list[PendingAlert]] = defaultdict(list)
    groups: list[list[PendingAlert]] = []
    for item in sends:
        if item.kind in _SUMMARY_KINDS:
            by_cat[item.category].append(item)
        else:
            groups.append([item])
    for items in by_cat.values():
        if len(items) < SUMMARY_MIN:
            groups.extend([i] for i in items)
            continue
        groups.extend(items[i : i + SUMMARY_MAX] for i in range(0, len(items), SUMMARY_MAX))
    return groups
