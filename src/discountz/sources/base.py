"""Contrato de los adaptadores de fuentes (spec §4.2).

Pensado para que Mercado Libre (OAuth), eBay, Reddit y las APIs tipo Algolia entren sin tocar
`domain/` ni `app/`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import SecretStr

from discountz.domain.errors import CredentialUnavailable, SourceError
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec
from discountz.domain.money import Currency
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpClient, HttpResponse

__all__ = [
    "BlockKind",
    "Capability",
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
]


class Capability(StrEnum):
    DETAIL = "detail"
    SEARCH = "search"
    CONFIRM = "confirm"
    LEADS = "leads"
    RESOLVE = "resolve"


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


class CredentialStore(Protocol):
    def bearer(self, provider: str) -> SecretStr: ...


class NoCredentials:
    """v1: ninguna fuente usa credenciales."""

    def bearer(self, provider: str) -> SecretStr:
        raise CredentialUnavailable(provider, "no_credentials")


@dataclass(frozen=True, slots=True)
class SourceContext:
    http: HttpClient  # ya envuelto en PacedClient
    credentials: CredentialStore
    deadline_monotonic: float


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
    error: SourceError


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
