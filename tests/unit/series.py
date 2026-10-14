"""Helper para construir series de segmentos relativas a T = 2026-10-13T15:00Z."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from discountz.domain.model import SellerClass
from discountz.domain.segments import Segment, SegmentStatus

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
_REL = re.compile(r"^(?:d-(\d+)|t)$")


def at(when: str | datetime) -> datetime:
    """`"d-90"` = T − 90 días; `"t"` = T; un datetime se devuelve tal cual."""
    if isinstance(when, datetime):
        return when
    m = _REL.fullmatch(when)
    if m is None:
        raise ValueError(when)
    return T - timedelta(days=int(m.group(1))) if m.group(1) else T


def seg(
    start: str | datetime,
    end: str | datetime | None,
    price: int | None,
    *,
    list_price: int | None = None,
    in_stock: bool = True,
    seller: SellerClass = SellerClass.FIRST_PARTY,
    unit: str = "un|1",
    status: SegmentStatus = SegmentStatus.OPEN,
    last_seen: datetime | None = None,
    listing_id: int = 1,
) -> Segment:
    """Segmento `[start, end)` con lecturas hasta 30 min antes de `end` (o de T si está abierto)."""
    s = at(start)
    e = None if end is None else at(end)
    ls = last_seen or ((e if e is not None else T) - timedelta(minutes=30))
    return Segment(
        listing_id, s, max(ls, s), e, price, list_price, in_stock, seller, unit, status, 5
    )
