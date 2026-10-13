"""Guardas G1–G14 de validación de ofertas (spec §3.4). Funciones puras."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from enum import StrEnum

from discountz.domain.errors import ParseError
from discountz.domain.model import ListingRef, Offer, SellerClass, normalize_title
from discountz.domain.money import Currency
from discountz.domain.segments import Segment
from discountz.domain.stats import HistoryStats, is_power10_shift


class GuardResult(StrEnum):
    OK = "ok"
    PARSE_SHIFT = "parse_shift"
    UNIT_CHANGED = "unit_changed"
    IDENTITY_RESET = "identity_reset"
    NOT_BUYABLE = "not_buyable"
    OUT_OF_STOCK = "out_of_stock"
    BATCH_CONSTANT = "batch_constant"
    DECIMAL_SHIFT = "decimal_shift"
    JUMP = "jump"
    SELLER_RESTRICTED = "seller_restricted"


# G3: cotas absolutas en unidad menor (COP pesos, USD centavos).
BOUNDS: Mapping[Currency, tuple[int, int]] = {
    Currency.COP: (1_000, 100_000_000),
    Currency.USD: (50, 5_000_000),
}
MAX_LIST_RATIO = 20  # G4: L > 20·P es incoherente
SHIFT_TOL_BP = 300  # G5 y G11: ±3 % de 10^k
BATCH_MIN = 5  # G10
BATCH_MODE_BP = 8000  # G10: moda ≥ 80 %
BATCH_HISTORY_MIN = 3  # G10: listings del lote con histórico
BATCH_GAP_BP = 3000  # G10: |moda − R| > 30 % de R
STORM_MIN = 2  # G12
JUMP_WINDOW = timedelta(days=7)  # G13

_COP_MARKER = re.compile(r"\bCOP\b|\bCOL\$", re.IGNORECASE)
_USD_MARKER = re.compile(r"US\$|\bUSD\b", re.IGNORECASE)
_CAPACITY_RE = re.compile(r"\d+(?:gb|tb|w|hz|in)")
_MODEL_RE = re.compile(r"[a-z]{1,4}-?\d{2,}")


def check_currency_text(text: str, source_currency: Currency) -> None:
    """G2 sobre texto: un marcador de la otra moneda lanza `ParseError`."""
    other = _USD_MARKER if source_currency is Currency.COP else _COP_MARKER
    if other.search(text):
        raise ParseError("currency", loc="price")


def check_offer(offer: Offer, source_currency: Currency, prev_list_price: int | None) -> Offer:
    """G1–G5: lanza `ParseError` o devuelve la oferta (sin `list_price` si G4 la descarta)."""
    for field, money in (("price", offer.price), ("list_price", offer.list_price)):
        if money is not None and money.currency is not source_currency:
            raise ParseError("currency", loc=field)
    if offer.price is None:
        return replace(offer, list_price=None)
    p = offer.price.minor
    if p <= 0:
        raise ParseError("non_positive", loc="price")
    lo, hi = BOUNDS[source_currency]
    if not lo <= p <= hi:
        raise ParseError("out_of_bounds", loc="price")
    if offer.list_price is None:
        return offer
    lp = offer.list_price.minor
    if lp <= p or lp > MAX_LIST_RATIO * p:
        return replace(offer, list_price=None)
    if prev_list_price is not None and is_power10_shift(lp, prev_list_price, SHIFT_TOL_BP):
        raise ParseError("parse_shift", loc="list_price")
    return offer


def hard_tokens(title: str) -> frozenset[str]:
    """Tokens duros del título: capacidades (`1tb`, `144hz`) y modelos (`gp850`)."""
    found: set[str] = set()
    for token in normalize_title(title).split():
        if _CAPACITY_RE.fullmatch(token):
            found.add(token)
        found.update(_MODEL_RE.findall(token))
    return frozenset(found)


def hard_tokens_changed(old_title: str, new_title: str) -> bool:
    """G7: cambian los tokens duros (ambos títulos los tienen y difieren)."""
    old, new = hard_tokens(old_title), hard_tokens(new_title)
    return bool(old) and bool(new) and old != new


def _mode(offers: Sequence[Offer]) -> tuple[int, int, int] | None:
    prices = [o.price.minor for o in offers if o.price is not None]
    if len(prices) < BATCH_MIN:
        return None
    mode, count = Counter(prices).most_common(1)[0]
    return mode, count, len(prices)


def batch_is_constant(offers: Sequence[Offer]) -> bool:
    """G10 (parte 1): n ≥ 5 y la moda reúne ≥ 80 % de los precios del lote."""
    m = _mode(offers)
    return m is not None and m[1] * 10_000 >= BATCH_MODE_BP * m[2]


def check_batch(offers: Sequence[Offer], r_by_listing: Mapping[ListingRef, int | None]) -> bool:
    """G10: `True` = descartar el lote (precio constante que no cuadra con ≥ 3 históricos).

    Un lote constante sin histórico suficiente se conserva; el llamador no alerta ese ciclo
    (`batch_is_constant`).
    """
    m = _mode(offers)
    if m is None or not batch_is_constant(offers):
        return False
    mode = m[0]
    far = 0
    for o in offers:
        r = r_by_listing.get(o.ref)
        if r is not None and abs(mode - r) * 10_000 > BATCH_GAP_BP * r:
            far += 1
    return far >= BATCH_HISTORY_MIN


def check_storm(candidates: int) -> bool:
    """G12: ≥ 2 candidatos a `PRICE_ERROR` o `DECIMAL_SHIFT` en la fuente y el ciclo."""
    return candidates >= STORM_MIN


def guard_flags(
    offer: Offer, open_seg: Segment | None, stats: HistoryStats | None, allow_marketplace: bool
) -> frozenset[GuardResult]:
    """G6, G8, G9, G11, G13 y G14 sobre una oferta ya validada por `check_offer`.

    `open_seg` es el segmento abierto antes de esta lectura; `stats` los estadísticos vigentes.
    """
    flags: set[GuardResult] = set()
    if open_seg is not None and open_seg.unit_key != offer.unit_key:
        flags.add(GuardResult.UNIT_CHANGED)
    if offer.price is None:
        flags.add(GuardResult.NOT_BUYABLE)
    if not offer.in_stock:
        flags.add(GuardResult.OUT_OF_STOCK)
    if offer.price is not None:
        p = offer.price.minor
        if stats is not None and stats.r is not None and is_power10_shift(stats.r, p, SHIFT_TOL_BP):
            flags.add(GuardResult.DECIMAL_SHIFT)
        if _is_jump(p, open_seg, offer):
            flags.add(GuardResult.JUMP)
    if offer.seller_class is SellerClass.UNKNOWN or (
        offer.seller_class is SellerClass.MARKETPLACE and not allow_marketplace
    ):
        flags.add(GuardResult.SELLER_RESTRICTED)
    return frozenset(flags) or frozenset({GuardResult.OK})


def _is_jump(p: int, open_seg: Segment | None, offer: Offer) -> bool:
    if open_seg is None or not open_seg.in_stock or open_seg.price_minor is None:
        return False
    if offer.observed_at - open_seg.last_seen_at > JUMP_WINDOW:
        return False
    prev = open_seg.price_minor
    return 2 * p <= prev or p >= 2 * prev
