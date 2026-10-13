"""Modelo de dominio: ofertas, leads, búsquedas, tipos de alerta y banderas."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from discountz.domain.money import Money
from discountz.domain.urls import SafeUrl


@dataclass(frozen=True, slots=True)
class ListingRef:
    """Identidad de una variante vendible en una fuente."""

    source: str
    external_id: str


class SellerClass(StrEnum):
    FIRST_PARTY = "f"
    MARKETPLACE = "m"
    UNKNOWN = "u"


class ExtractionPath(StrEnum):
    API_JSON = "api_json"
    EMBEDDED_JSON = "embedded_json"
    HTML = "html"


@dataclass(frozen=True, slots=True)
class Offer:
    ref: ListingRef  # (source, external_id) a nivel de VARIANTE
    parent_external_id: str | None
    title: str
    price: Money | None  # None = sin precio comprable (G8)
    list_price: Money | None
    in_stock: bool
    seller_class: SellerClass
    unit_key: str  # 'un|1', 'g|0.5', 'pk|6'
    extraction: ExtractionPath
    observed_at: datetime
    url_path: str | None = None  # ruta canónica en el host de la fuente (p. ej. `/<slug>/p`)


@dataclass(frozen=True, slots=True)
class Lead:
    source: str  # 'slickdeals'
    title: str
    target_url: SafeUrl  # URL de tienda ya validada contra la lista blanca
    price: Money | None
    published_at: datetime


_UNITS = (
    "tb|gb|mb|kb|ghz|mhz|hz|w|mah|mm|cm|m|kg|g|mg|ml|l|lb|lbs|oz|in|pulgadas|pulg|"
    "p|k|v|un|und|unidades|pack|ct|count"
)
_NUM_UNIT_RE = re.compile(rf"\b(\d+(?:[.,]\d+)?)\s+({_UNITS})\b")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?[a-z]*")


def normalize_title(text: str) -> str:
    """Minúsculas, sin tildes y con número y unidad unidos (`1 TB` → `1tb`)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    joined = _NUM_UNIT_RE.sub(r"\1\2", plain)
    return " ".join(_TOKEN_RE.findall(joined))


@dataclass(frozen=True, slots=True)
class SearchSpec:
    source: str
    query: str
    exclude_terms: tuple[str, ...]
    min_minor: int | None
    max_minor: int | None
    category: str

    def accepts(self, offer: Offer) -> bool:
        """Filtro puro: todos los tokens, ningún excluido y precio dentro de `[min, max]`."""
        title_tokens = set(normalize_title(offer.title).split())
        if not set(normalize_title(self.query).split()) <= title_tokens:
            return False
        for term in self.exclude_terms:
            if set(normalize_title(term).split()) & title_tokens:
                return False
        if self.min_minor is None and self.max_minor is None:
            return True
        if offer.price is None:
            return False
        p = offer.price.minor
        return (self.min_minor is None or p >= self.min_minor) and (
            self.max_minor is None or p <= self.max_minor
        )


class AlertKind(StrEnum):
    PRICE_ERROR = "price_error"
    HISTORIC_LOW = "historic_low"
    TARGET_REACHED = "target_reached"
    GOOD_DEAL = "good_deal"
    COMMUNITY_DEAL = "community_deal"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {
    AlertKind.PRICE_ERROR: 5,
    AlertKind.HISTORIC_LOW: 4,
    AlertKind.TARGET_REACHED: 3,
    AlertKind.GOOD_DEAL: 2,
    AlertKind.COMMUNITY_DEAL: 1,
}


class Maturity(StrEnum):
    COLD = "cold"
    WARM = "warm"
    MATURE = "mature"


class Flag(StrEnum):
    """Banderas informativas de una alerta (CHECK de `alert.flags`, spec §2.2 y §3.7)."""

    INFLATED_LIST = "inflated_list"
    ADVERTISED_GAP = "advertised_gap"
    PRE_INFLATED = "pre_inflated"
    FLASH_SEEN = "flash_seen"
    GAP_BEFORE = "gap_before"
    SHORT_HISTORY = "short_history"
    MARKETPLACE = "marketplace"
    TARGET_ABOVE_TYPICAL = "target_above_typical"
    REF_ANTIGUA = "ref_antigua"
    UNIT_CHANGED = "unit_changed"
    STORM = "storm"
    TARGET_ALSO = "target_also"
    PERSISTENCE_ONLY = "persistence_only"
    HOT = "hot"
    COMMUNITY = "community"
    UNVERIFIED = "unverified"
