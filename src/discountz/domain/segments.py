"""Histórico por segmentos de estado constante y su actualización pura (spec §3.2)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from discountz.domain.model import SellerClass


class SegmentStatus(StrEnum):
    OPEN = "o"  # lectura válida: entra en las estadísticas
    SUSPECT = "s"  # candidato sin confirmar: nunca entra en E
    GLITCH = "g"  # error de precio confirmado: fuera de E hasta su reclasificación


@dataclass(frozen=True, slots=True)
class Segment:
    listing_id: int
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None
    price_minor: int | None
    list_price_minor: int | None
    in_stock: bool
    seller_class: SellerClass
    unit_key: str
    status: SegmentStatus
    sample_count: int


@dataclass(frozen=True, slots=True)
class Observation:
    t: datetime
    price_minor: int | None
    list_price_minor: int | None
    in_stock: bool
    seller_class: SellerClass
    unit_key: str


@dataclass(frozen=True, slots=True)
class Extend:
    segment: Segment  # el segmento abierto ya extendido


@dataclass(frozen=True, slots=True)
class CloseAndOpen:
    closed: Segment | None  # None si no había segmento abierto
    opened: Segment


@dataclass(frozen=True, slots=True)
class Ignore:
    pass


def gap_h(nominal_interval: timedelta) -> timedelta:
    """H = máx(12 h, 1,5 × intervalo nominal)."""
    return max(timedelta(hours=12), nominal_interval * 3 / 2)


def apply_sample(
    open_seg: Segment | None, obs: Observation, h: timedelta, status: SegmentStatus
) -> Extend | CloseAndOpen | Ignore:
    """Decide si una lectura extiende el segmento abierto o lo cierra y abre otro."""
    opened = Segment(
        listing_id=open_seg.listing_id if open_seg else 0,
        started_at=obs.t,
        last_seen_at=obs.t,
        ended_at=None,
        price_minor=obs.price_minor,
        list_price_minor=obs.list_price_minor,
        in_stock=obs.in_stock,
        seller_class=obs.seller_class,
        unit_key=obs.unit_key,
        status=status,
        sample_count=1,
    )
    if open_seg is None:
        return CloseAndOpen(closed=None, opened=opened)
    if obs.t <= open_seg.last_seen_at:
        return Ignore()
    same_state = (
        open_seg.price_minor == obs.price_minor
        and open_seg.in_stock == obs.in_stock
        and open_seg.seller_class == obs.seller_class
        and open_seg.unit_key == obs.unit_key
        and open_seg.status == status
    )
    if same_state and obs.t - open_seg.last_seen_at <= h:
        return Extend(
            replace(
                open_seg,
                last_seen_at=obs.t,
                sample_count=open_seg.sample_count + 1,
                list_price_minor=_max_optional(open_seg.list_price_minor, obs.list_price_minor),
            )
        )
    return CloseAndOpen(closed=replace(open_seg, ended_at=obs.t), opened=opened)


def _max_optional(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
