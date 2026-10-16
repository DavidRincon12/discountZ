"""Contrato de los adaptadores de fuentes (spec §4.2).

Pensado para que Mercado Libre (OAuth), eBay, Reddit y las APIs tipo Algolia entren sin tocar
`domain/` ni `app/`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import SecretStr

from discountz.domain.catalog import CatalogObservation
from discountz.domain.errors import CredentialUnavailable, ParseError, SourceError
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec
from discountz.domain.money import Currency
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpClient, HttpResponse

__all__ = [
    "BlockKind",
    "Capability",
    "CardFacts",
    "CatalogPage",
    "CatalogSource",
    "CrawlSource",
    "CredentialStore",
    "ExtractionPath",
    "ListingFailed",
    "ListingFound",
    "ListingGone",
    "ListingOutcome",
    "NeedsResolution",
    "NoCredentials",
    "SearchPage",
    "Source",
    "SourceContext",
    "SourceDescriptor",
    "pack_unit_key",
]

# Regla «Pack» común a todas las tiendas (spec §4.2): `(24 Pack)` ⇒ `pk|24`.
PACK_RE = re.compile(r"(\d{1,3})[- ]?(pack|count|ct|unidades|und)\b", re.IGNORECASE)


def pack_unit_key(title: str) -> str | None:
    m = PACK_RE.search(title)
    return f"pk|{int(m.group(1))}" if m else None


class Capability(StrEnum):
    DETAIL = "detail"
    SEARCH = "search"
    CONFIRM = "confirm"
    LEADS = "leads"
    RESOLVE = "resolve"
    CATALOG = "catalog"


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    code: str
    kind: Literal["store", "feed"]
    currency: Currency
    hosts: frozenset[str]  # coincidencia EXACTA tras minúsculas + IDNA
    client: Literal["httpx", "curl"]
    capabilities: frozenset[Capability]
    max_batch: int  # refs por petición en fetch_listings
    external_id_re: re.Pattern[str]
    auth: Literal["none", "api_key", "oauth_client", "oauth_refresh"] = "none"
    image_hosts: frozenset[str] = frozenset()  # hosts permitidos para la foto de la tarjeta
    supports_changed: bool = False  # la fuente puede listar «cambiados desde» (fetch_changed)


class CredentialStore(Protocol):
    def bearer(self, provider: str) -> SecretStr: ...
    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]: ...
    def public_id(self, provider: str) -> str: ...  # identificador no secreto (publisherId)


class NoCredentials:
    """Sin credenciales: toda fuente que las pida queda no disponible."""

    def bearer(self, provider: str) -> SecretStr:
        raise CredentialUnavailable(provider, "no_credentials")

    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]:
        raise CredentialUnavailable(provider, "no_credentials")

    def public_id(self, provider: str) -> str:
        raise CredentialUnavailable(provider, "no_credentials")


@dataclass(frozen=True, slots=True)
class SourceContext:
    http: HttpClient  # ya envuelto en PacedClient
    credentials: CredentialStore
    deadline_monotonic: float
    now: datetime  # instante del ciclo: los adaptadores no leen el reloj


@dataclass(frozen=True, slots=True)
class NeedsResolution:
    source: str
    pending_ref: str  # 'slug:<slug>[|sku:<id>]' | 'short:<host>/<path>'


@dataclass(frozen=True, slots=True)
class ListingFound:
    offer: Offer


@dataclass(frozen=True, slots=True)
class ListingGone:
    ref: ListingRef


@dataclass(frozen=True, slots=True)
class ListingFailed:
    ref: ListingRef
    error: SourceError | ParseError  # ParseError conserva el tipo 'parse' para el breaker


ListingOutcome = ListingFound | ListingGone | ListingFailed


@dataclass(frozen=True, slots=True)
class SearchPage:
    offers: Sequence[Offer]
    total_results: int | None


class Source(Protocol):
    descriptor: SourceDescriptor

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None: ...
    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef: ...
    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]: ...
    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage: ...
    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None: ...
    def fetch_leads(self, ctx: SourceContext) -> list[Lead]: ...
    def detect_block(self, resp: HttpResponse) -> BlockKind | None: ...


@dataclass(frozen=True, slots=True)
class CatalogPage:
    """Una página del recorrido. `split` no vacío ⇒ la partición se reemplaza por esas."""

    items: Sequence[CatalogObservation]
    has_more: bool
    split: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class CardFacts:
    title: str
    url: str
    image_url: str | None


class CatalogSource(Protocol):
    descriptor: SourceDescriptor

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[str]: ...
    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage: ...
    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage: ...
    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> Sequence[CatalogObservation]: ...
    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]: ...


class CrawlSource(Source, CatalogSource, Protocol):
    """Fuente que se puede rastrear: ficha y búsqueda de la v1 más el catálogo."""
