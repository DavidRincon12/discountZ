"""Detección de bajadas de precio del catálogo (spec del rastreo §3). Puro.

El descuento se mide solo contra precios que discountZ observó; el `ListPrice` de la tienda no
existe en este modelo (en el marketplace llega inflado: spec §0.3).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from discountz.domain.model import AlertKind, SellerClass
from discountz.domain.money import Money
from discountz.domain.params import GlobalParams
from discountz.domain.topics import TopicParams

STABLE_FOR = timedelta(hours=24)  # un precio pasa a «habitual» tras 24 h…
MIN_READS = 2  # …y al menos 2 lecturas
WARMUP = timedelta(days=3)  # observación mínima para CATALOG_DROP
COOLDOWN = timedelta(hours=72)
# La mediana del modelo no converge al precio nuevo como el «antes»: sin bajar más, la misma
# publicación no vuelve a alertar por §4.2 en este plazo (spec de eBay §4.2).
MODEL_REALERT = timedelta(days=30)
MAX_MIN_DAYS = 90
MIN_WINDOW = timedelta(days=MAX_MIN_DAYS)  # el mínimo de precios estables vence a los 90 días
MIN_PEERS = 5  # publicaciones del mismo modelo para comparar (spec de eBay §4.2)


@dataclass(frozen=True, slots=True)
class CatalogObservation:
    external_id: str
    price: Money
    category_path: str  # tal como la da la tienda
    ean: str | None
    seller_class: SellerClass
    model_key: str | None = None  # `epid` de eBay; None en las demás tiendas


@dataclass(frozen=True, slots=True)
class CatalogState:
    price_minor: int
    usual_minor: int | None
    min_seen_minor: int | None
    first_seen_at: datetime
    stable_since: datetime
    reads_at_price: int
    last_changed_at: datetime | None
    last_alert_at: datetime | None
    last_alert_minor: int | None
    min_seen_at: datetime | None = None  # última vez que el precio estable estuvo en el mínimo


@dataclass(frozen=True, slots=True)
class CatalogCandidate:
    kind: AlertKind
    price: Money
    usual: Money
    drop_bp: int
    min_days: int


def observe(
    state: CatalogState | None,
    obs: CatalogObservation,
    now: datetime,
    topic: TopicParams,
    glob: GlobalParams,
) -> tuple[CatalogState, CatalogCandidate | None, bool]:
    """Aplica una lectura: estado nuevo, candidato y si hay que registrar el cambio de precio."""
    p = obs.price.minor
    if state is None:
        fresh = CatalogState(p, None, None, now, now, 1, None, None, None)
        return fresh, None, True
    if p == state.price_minor:
        reads = state.reads_at_price + 1
        stable = reads >= MIN_READS and now - state.stable_since >= STABLE_FOR
        state = replace(state, reads_at_price=reads)
        if stable:
            state = replace(state, usual_minor=p)
            lowest = _min_seen(state, now)
            if lowest is None or p <= lowest:
                state = replace(state, min_seen_minor=p, min_seen_at=now)
            elif state.min_seen_at is None:  # guardado antes de la ventana: cuenta desde hoy
                state = replace(state, min_seen_at=now)
        return state, None, False
    candidate = _candidate(state, obs, now, topic, glob)
    moved = replace(state, price_minor=p, stable_since=now, reads_at_price=1, last_changed_at=now)
    return moved, candidate, True


def _candidate(
    state: CatalogState,
    obs: CatalogObservation,
    now: datetime,
    topic: TopicParams,
    glob: GlobalParams,
) -> CatalogCandidate | None:
    u, p = state.usual_minor, obs.price.minor
    if u is None or p >= u or topic.t_bp is None or topic.g_bp is None:
        return None
    drop_bp = (u - p) * 10_000 // u
    kind: AlertKind | None = None
    if drop_bp >= topic.g_bp:
        kind = AlertKind.PRICE_ERROR
    elif (
        now - state.first_seen_at >= WARMUP
        and drop_bp >= topic.t_bp
        and ((lowest := _min_seen(state, now)) is None or p < lowest)
        and u - p >= glob.min_abs[obs.price.currency]
    ):
        kind = AlertKind.CATALOG_DROP
    if kind is None or _cooling(state, p, now, glob):
        return None
    days = min(MAX_MIN_DAYS, (now - state.first_seen_at).days)
    return CatalogCandidate(kind, obs.price, Money(u, obs.price.currency), drop_bp, days)


def _min_seen(state: CatalogState, now: datetime) -> int | None:
    """El mínimo de precios estables de los últimos 90 días; `None` si venció."""
    if state.min_seen_at is not None and now - state.min_seen_at > MIN_WINDOW:
        return None
    return state.min_seen_minor


def _cooling(
    state: CatalogState | None,
    p: int,
    now: datetime,
    glob: GlobalParams,
    window: timedelta = COOLDOWN,
) -> bool:
    """Ya alertó dentro de `window` (72 h por defecto) y el precio no mejoró lo suficiente."""
    return (
        state is not None
        and state.last_alert_at is not None
        and state.last_alert_minor is not None
        and now - state.last_alert_at < window
        and p * 10_000 > state.last_alert_minor * (10_000 - glob.improve_bp)
    )


def split_price_band(lo: int, hi: int) -> list[tuple[int, int]]:
    """Parte la franja `[lo, hi]` en el punto medio geométrico; un solo precio no se parte.

    Los precios se agolpan en los valores bajos: el punto medio geométrico reparte los productos
    mejor que el aritmético (VTEX y Alkosto parten así las franjas que pasan su límite).
    """
    if lo >= hi:
        return []
    mid = min(max(math.isqrt(max(lo, 1) * hi), lo), hi - 1)
    return [(lo, mid), (mid + 1, hi)]


def median_low(values: Sequence[int]) -> int:
    """Mediana que siempre es uno de los valores: con una cantidad par, la menor de las dos."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def model_candidate(
    state: CatalogState | None,
    now: datetime,
    obs: CatalogObservation,
    peers: Sequence[int],
    topic: TopicParams,
    glob: GlobalParams,
) -> CatalogCandidate | None:
    """Precio bajo frente a las demás publicaciones del mismo modelo (spec de eBay §4.2)."""
    if len(peers) < MIN_PEERS or topic.t_bp is None or topic.g_bp is None:
        return None
    median, p = median_low(peers), obs.price.minor
    if p >= median or median - p < glob.min_abs[obs.price.currency]:
        return None
    drop_bp = (median - p) * 10_000 // median
    if drop_bp >= topic.g_bp:
        kind = AlertKind.PRICE_ERROR
    elif drop_bp >= topic.t_bp:
        kind = AlertKind.MODEL_LOW
    else:
        return None
    if _cooling(state, p, now, glob, MODEL_REALERT):
        return None
    return CatalogCandidate(kind, obs.price, Money(median, obs.price.currency), drop_bp, 0)
