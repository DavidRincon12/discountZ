"""Estadísticos del histórico ponderados por tiempo observado, solo con enteros (spec §3.2-§3.3)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise

from discountz.domain.model import SellerClass
from discountz.domain.params import CategoryParams, GlobalParams
from discountz.domain.segments import Segment, SegmentStatus

SECOND = timedelta(seconds=1)
BOGOTA = timezone(timedelta(hours=-5), "America/Bogota")  # sin horario de verano

Points = tuple[tuple[int, int], ...]


def segment_weight_s(
    seg: Segment, a: datetime, b: datetime, s_cur: datetime, h: timedelta, reset_at: datetime | None
) -> int:
    """Segundos del segmento dentro de `[a, b)`, sin pasar de `s_cur` ni de `last_seen + H`."""
    end = seg.last_seen_at + h if seg.ended_at is None else min(seg.ended_at, seg.last_seen_at + h)
    lo = max(seg.started_at, a, reset_at) if reset_at else max(seg.started_at, a)
    hi = min(end, b, s_cur)
    return max(0, (hi - lo) // SECOND)


def wq(points: Sequence[tuple[int, int]], q_bp: int) -> int | None:
    """Cuantil inferior ponderado. points = (price_minor, weight_s). Solo enteros."""
    total = sum(w for _, w in points)
    if total == 0:
        return None
    acc = 0
    for price, w in sorted(points):
        acc += w
        if acc * 10_000 >= q_bp * total:
            return price
    raise AssertionError("inalcanzable")


def rarity_bp(points: Sequence[tuple[int, int]], p: int, tol_bp: int = 50) -> int:
    """F(P): fracción del tiempo (bp) en que el precio fue ≤ P (+ tolerancia).

    Sin histórico devuelve 10.000 (nada es raro), que nunca habilita un `GOOD_DEAL`.
    """
    total = sum(w for _, w in points)
    if total == 0:
        return 10_000
    low = sum(w for price, w in points if price * 10_000 <= p * (10_000 + tol_bp))
    return low * 10_000 // total


def is_power10_shift(a: int, b: int, tol_bp: int = 300) -> bool:
    hi, lo = max(a, b), min(a, b)
    return any(abs(hi * 10_000 - lo * 10**k * 10_000) <= lo * 10**k * tol_bp for k in (1, 2, 3))


def real_saving_bp(r: int, p: int) -> int:
    """S_real = (R − P)·10000 // R (piso: conservador)."""
    return (r - p) * 10_000 // r


@dataclass(frozen=True, slots=True)
class HistoryStats:
    r: int | None
    m90: int | None
    m30: int | None  # None si no es válida (< 7 d)
    m_prior: int | None  # None si no es válida (< 7 d)
    m_rec: int | None
    q10: int | None
    q90: int | None
    v_bp: int | None
    coverage_s: int
    days: int
    m_robust: int | None
    raw_min: int | None
    max_charged: int | None
    anchor: int | None
    points: Points  # (precio, peso) de E sobre [t−W, s_cur): base de F(P)
    extended: bool  # ventana extendida a 365 d (bandera ref_antigua)
    last_eligible_seen: datetime | None  # último last_seen elegible antes de s_cur (gap_before)


def compute_stats(
    segments: Sequence[Segment],
    now: datetime,
    s_cur: datetime,
    cat: CategoryParams,
    glob: GlobalParams,
    h: timedelta,
    reset_at: datetime | None,
    alltime_min: int | None,
    last_alert_price: int | None,
    *,
    unit_key: str | None = None,
) -> HistoryStats:
    """Calcula los estadísticos de spec §3.3 sobre el conjunto elegible E.

    `unit_key` es la unidad actual; si no se da, se toma la del segmento que empieza en `s_cur`
    o, si no existe, la del último segmento.
    """
    ordered = sorted(segments, key=lambda s: s.started_at)
    unit = unit_key if unit_key is not None else _current_unit(ordered, s_cur)
    elig = [s for s in ordered if _eligible(s, unit, cat) and s.started_at < s_cur]
    stats = _compute(ordered, elig, now, s_cur, cat.window, cat, glob, h, reset_at)
    if stats.coverage_s < _s(cat.mature_coverage):
        wide = _compute(ordered, elig, now, s_cur, glob.extended_window, cat, glob, h, reset_at)
        if wide.coverage_s >= _s(cat.mature_coverage):
            stats = wide
    m_robust = _robust_min(ordered, elig, now, s_cur, glob, h, reset_at)
    if alltime_min is not None:
        m_robust = alltime_min if m_robust is None else min(m_robust, alltime_min)
    anchor = last_alert_price
    if anchor is None:
        old = _points(
            elig, now - glob.anchor_old_from, now - glob.anchor_old_to, s_cur, h, reset_at
        )
        anchor = wq(old, 5000) if _total(old) >= _s(glob.validity_min) else None
    return HistoryStats(
        r=stats.r,
        m90=stats.m90,
        m30=stats.m30,
        m_prior=stats.m_prior,
        m_rec=stats.m_rec,
        q10=stats.q10,
        q90=stats.q90,
        v_bp=stats.v_bp,
        coverage_s=stats.coverage_s,
        days=stats.days,
        m_robust=m_robust,
        raw_min=min((s.price_minor for s in elig if s.price_minor is not None), default=None),
        max_charged=stats.max_charged,
        anchor=anchor,
        points=stats.points,
        extended=stats.extended,
        last_eligible_seen=max((s.last_seen_at for s in elig), default=None),
    )


def _s(td: timedelta) -> int:
    return td // SECOND


def _total(points: Points) -> int:
    return sum(w for _, w in points)


def _current_unit(ordered: Sequence[Segment], s_cur: datetime) -> str | None:
    for s in ordered:
        if s.started_at == s_cur:
            return s.unit_key
    return ordered[-1].unit_key if ordered else None


def _eligible(s: Segment, unit: str | None, cat: CategoryParams) -> bool:
    seller_ok = s.seller_class is SellerClass.FIRST_PARTY or (
        cat.allow_marketplace and s.seller_class is SellerClass.MARKETPLACE
    )
    return (
        s.in_stock
        and s.price_minor is not None
        and s.status is SegmentStatus.OPEN
        and s.unit_key == unit
        and seller_ok
    )


def _points(
    elig: Sequence[Segment],
    a: datetime,
    b: datetime,
    s_cur: datetime,
    h: timedelta,
    reset_at: datetime | None,
) -> Points:
    out: list[tuple[int, int]] = []
    for s in elig:
        w = segment_weight_s(s, a, b, s_cur, h, reset_at)
        if w > 0 and s.price_minor is not None:
            out.append((s.price_minor, w))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class _Window:
    r: int | None
    m90: int | None
    m30: int | None
    m_prior: int | None
    m_rec: int | None
    q10: int | None
    q90: int | None
    v_bp: int | None
    coverage_s: int
    days: int
    max_charged: int | None
    points: Points
    extended: bool


def _compute(
    ordered: Sequence[Segment],
    elig: Sequence[Segment],
    now: datetime,
    s_cur: datetime,
    window: timedelta,
    cat: CategoryParams,
    glob: GlobalParams,
    h: timedelta,
    reset_at: datetime | None,
) -> _Window:
    del ordered
    start = now - window
    base = _points(elig, start, now, s_cur, h, reset_at)
    m90 = wq(base, 5000)
    p30 = _points(elig, now - glob.m30_window, now, s_cur, h, reset_at)
    m30 = wq(p30, 5000) if _total(p30) >= _s(glob.validity_min) else None
    prior = _points(elig, start, s_cur - cat.k, s_cur, h, reset_at)
    m_prior = wq(prior, 5000) if _total(prior) >= _s(glob.validity_min) else None
    candidates = [m for m in (m90, m30, m_prior) if m is not None]
    r = min(candidates) if candidates else None
    q10, q90 = wq(base, 1000), wq(base, 9000)
    v_bp = (q90 - q10) * 10_000 // r if r and q10 is not None and q90 is not None else None
    by_price: defaultdict[int, int] = defaultdict(int)
    for price, w in base:
        by_price[price] += w
    charged = [p for p, w in by_price.items() if w >= _s(glob.max_charged_min)]
    return _Window(
        r=r,
        m90=m90,
        m30=m30,
        m_prior=m_prior,
        m_rec=wq(_points(elig, s_cur - glob.recent_window, now, s_cur, h, reset_at), 5000),
        q10=q10,
        q90=q90,
        v_bp=v_bp,
        coverage_s=_total(base),
        days=_days(elig, start, s_cur, h, reset_at),
        max_charged=max(charged, default=None),
        points=base,
        extended=window != cat.window,
    )


def _days(
    elig: Sequence[Segment], a: datetime, s_cur: datetime, h: timedelta, reset_at: datetime | None
) -> int:
    """Días calendario de Bogotá que tocan los intervalos cubiertos de E en la ventana."""
    seen: set[date] = set()
    for s in elig:
        end = s.last_seen_at + h if s.ended_at is None else min(s.ended_at, s.last_seen_at + h)
        lo = max(s.started_at, a, reset_at) if reset_at else max(s.started_at, a)
        hi = min(end, s_cur)
        if hi <= lo:
            continue
        d = lo.astimezone(BOGOTA).date()
        last = (hi - timedelta(microseconds=1)).astimezone(BOGOTA).date()
        while d <= last:
            seen.add(d)
            d += timedelta(days=1)
    return len(seen)


def _robust_min(
    ordered: Sequence[Segment],
    elig: Sequence[Segment],
    now: datetime,
    s_cur: datetime,
    glob: GlobalParams,
    h: timedelta,
    reset_at: datetime | None,
) -> int | None:
    """Mínimo de E retenido ≤ 365 d, sin relámpagos comprobados (cota superior < 6 h)."""
    prev_last_seen: dict[datetime, datetime] = {}
    for prev, cur in pairwise(ordered):
        prev_last_seen[cur.started_at] = prev.last_seen_at
    best: int | None = None
    for s in elig:
        if s.price_minor is None:
            continue
        if segment_weight_s(s, now - glob.retained_window, now, s_cur, h, reset_at) <= 0:
            continue
        before = prev_last_seen.get(s.started_at)
        if s.ended_at is not None and before is not None and s.ended_at - before < glob.flash_max:
            continue
        best = s.price_minor if best is None else min(best, s.price_minor)
    return best
