"""Adaptador de Best Buy US por la API oficial (spec del rastreo §2.1 y §4.4).

La v1 leía el HTML, que desde Actions responde 502 o se cuelga. La API (`api.bestbuy.com/v1`,
clave gratuita de developer.bestbuy.com) da precio, disponibilidad, categoría y UPC en JSON. Se
conserva el código `bestbuy_us` para no perder el histórico. La clave viaja en la query como
`apiKey`: los errores llevan solo códigos y los logs nunca registran queries.

Términos de la API: se guarda solo SKU, precio y fechas; título y foto se leen al alertar.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, ValidationError

from discountz.domain.catalog import CatalogObservation
from discountz.domain.errors import CredentialUnavailable, ParseError, SourceError, Unavailable
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    CardFacts,
    CatalogPage,
    ListingFailed,
    ListingFound,
    ListingGone,
    ListingOutcome,
    NeedsResolution,
    SearchPage,
    SourceContext,
    SourceDescriptor,
    pack_unit_key,
)
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import register

CODE = "bestbuy_us"
API = "https://api.bestbuy.com/v1"
SKU_RE = re.compile(r"^\d{6,8}$")
PAGE_SIZE = 100
CATALOG_QUERY = "products(active=true&orderable!=NotAvailable)"
# Best Buy publica `priceUpdateDate` en hora del centro de EE. UU. sin zona: margen de 6 h.
CHANGED_MARGIN = timedelta(hours=6)
_SITE_PATH = re.compile(r"^/site/(?:[^/]+/)?(\d{6,8})\.p/?$")
_PRODUCT_PATH = re.compile(r"^/product/[^/]+/[A-Z0-9]+/sku/(\d{6,8})/?$")
_WORD = re.compile(r"[^A-Za-z0-9]")
_UPC_RE = re.compile(r"^\d{6,14}$")
_SHOW = ",".join(
    (
        "sku",
        "name",
        "salePrice",
        "regularPrice",
        "onlineAvailability",
        "marketplace",
        "categoryPath.name",
        "upc",
        "image",
    )
)
_HEADERS = {"Accept": "application/json"}

Number = Decimal | int


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _Category(_Model):
    name: str


class _Product(_Model):
    sku: int
    name: str = ""
    salePrice: Number | None = None
    regularPrice: Number | None = None
    onlineAvailability: bool = False
    marketplace: bool | None = None
    categoryPath: list[_Category] = []
    upc: str | None = None
    image: str | None = None


class _Page(_Model):
    currentPage: int = 1
    totalPages: int = 1
    products: list[_Product]


def _load(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise ParseError("bad_json") from exc


def _validate[M: BaseModel](model: type[M], data: object) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False)[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))[:60]
        raise ParseError("schema", loc=loc or None) from None


def _usd(value: Number | None) -> Money | None:
    if value is None:
        return None
    minor = to_minor(Decimal(value), Currency.USD)
    return Money(minor, Currency.USD) if minor > 0 else None


def _seller(p: _Product) -> SellerClass:
    return SellerClass.MARKETPLACE if p.marketplace else SellerClass.FIRST_PARTY


def _offer(p: _Product, observed_at: datetime) -> Offer:
    sku = str(p.sku)
    title = " ".join(p.name.split())
    price = _usd(p.salePrice)
    in_stock = p.onlineAvailability and price is not None
    return Offer(
        ref=ListingRef(CODE, sku),
        parent_external_id=None,
        title=title,
        price=price if in_stock else None,  # sin stock no hay precio comprable (G8)
        list_price=_usd(p.regularPrice),
        in_stock=in_stock,
        seller_class=_seller(p),
        unit_key=pack_unit_key(title) or "un|1",
        extraction=ExtractionPath.API_JSON,
        observed_at=observed_at,
        url_path=f"/site/{sku}.p?skuId={sku}",
    )


def _observation(p: _Product) -> CatalogObservation | None:
    price = _usd(p.salePrice)
    if price is None or not p.onlineAvailability:
        return None
    names = [c.name for c in p.categoryPath[1:]]  # el primero es la raíz «Best Buy»
    path = "/" + "".join(f"{n}/" for n in names)
    upc = p.upc if p.upc and _UPC_RE.fullmatch(p.upc) else None
    return CatalogObservation(str(p.sku), price, path, upc, _seller(p))


def parse_bestbuy_page(body: bytes) -> _Page:
    return _validate(_Page, _load(body))


class BestBuySource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.USD,
            hosts=frozenset({"www.bestbuy.com", "bestbuy.com"}),
            client="httpx",
            capabilities=frozenset(
                {Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM, Capability.CATALOG}
            ),
            max_batch=PAGE_SIZE,
            external_id_re=SKU_RE,
            auth="api_key",
            image_hosts=frozenset({"pisces.bbystatic.com", "images.bbystatic.com"}),
            supports_changed=True,
        )

    def _get(self, ctx: SourceContext, resource: str, extra: dict[str, str] | None = None) -> bytes:
        key = ctx.credentials.bearer("bestbuy").get_secret_value()
        params = {"apiKey": key, "format": "json", "show": _SHOW, **(extra or {})}
        return ctx.http.request("GET", f"{API}/{resource}", params=params, headers=_HEADERS).body

    def _page(self, ctx: SourceContext, query: str, page: int) -> CatalogPage:
        extra = {"sort": "sku.asc", "pageSize": str(PAGE_SIZE), "page": str(page + 1)}
        data = parse_bestbuy_page(self._get(ctx, query, extra))
        items = [o for p in data.products if (o := _observation(p)) is not None]
        return CatalogPage(items, has_more=data.currentPage < data.totalPages)

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _SITE_PATH.match(url.path) or _PRODUCT_PATH.match(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # Best Buy no usa enlaces cortos

    def _products(self, ctx: SourceContext, skus: Sequence[str]) -> dict[str, _Product]:
        query = f"products(sku in({','.join(skus)}))"
        data = parse_bestbuy_page(self._get(ctx, query, {"pageSize": str(PAGE_SIZE)}))
        return {str(p.sku): p for p in data.products}

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for i in range(0, len(refs), PAGE_SIZE):
            batch = [r for r in refs[i : i + PAGE_SIZE] if SKU_RE.fullmatch(r.external_id)]
            if not batch:
                continue
            try:
                found = self._products(ctx, [r.external_id for r in batch])
            except (SourceError, ParseError) as exc:
                out.extend(ListingFailed(r, exc) for r in batch)
                continue
            for r in batch:
                p = found.get(r.external_id)
                out.append(ListingFound(_offer(p, ctx.now)) if p else ListingGone(r))
        return out

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        del page  # una sola página de 100
        words = [w for w in (_WORD.sub("", t) for t in spec.query.split()) if w][:8]
        if not words:
            return SearchPage([], 0)
        query = "products(" + "&".join(f"search={w}" for w in words) + ")"
        data = parse_bestbuy_page(self._get(ctx, query, {"pageSize": str(PAGE_SIZE)}))
        return SearchPage([_offer(p, ctx.now) for p in data.products], None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        """Ruta independiente: el recurso de un solo producto (`products/<sku>.json`)."""
        sku = offer.ref.external_id
        if not SKU_RE.fullmatch(sku):
            return None
        p = _validate(_Product, _load(self._get(ctx, f"products/{sku}.json")))
        return _offer(p, ctx.now) if str(p.sku) == sku else None

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[str]:
        return ["all"]

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        if partition != "all":
            raise ParseError("bad_partition")
        return self._page(ctx, CATALOG_QUERY, page)

    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage:
        floor = (since - CHANGED_MARGIN).strftime("%Y-%m-%dT%H:%M:%S")
        return self._page(ctx, f"products(priceUpdateDate>{floor}&active=true)", page)

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> list[CatalogObservation]:
        skus = [s for s in external_ids if SKU_RE.fullmatch(s)][:PAGE_SIZE]
        if not skus:
            return []
        found = self._products(ctx, skus)
        return [o for p in found.values() if (o := _observation(p)) is not None]

    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]:
        skus = [s for s in external_ids if SKU_RE.fullmatch(s)][:PAGE_SIZE]
        if not skus:
            return {}
        return {
            sku: CardFacts(
                " ".join(p.name.split()),
                f"https://www.bestbuy.com/site/{sku}.p?skuId={sku}",
                p.image,
            )
            for sku, p in self._products(ctx, skus).items()
        }

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        """Un 403 de la API por cuota o clave no es un bloqueo anti-bots: no abre el breaker."""
        if resp.status == 403:
            head = resp.body[:2048]
            if b"Over Quota" in head:
                raise Unavailable(CODE, "quota", 403)
            if b"API Key" in head or b"apiKey" in head:
                raise CredentialUnavailable(CODE, "bad_key", 403)
        return detect_block_common(resp)


register(BestBuySource())
