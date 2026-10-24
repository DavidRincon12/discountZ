"""Adaptador de Falabella por el JSON de sus listados filtrados por vendedor (ADR 0017).

La web de Falabella es Next.js: cada listado trae sus productos en `__NEXT_DATA__`. Solo se
rastrea lo que vende la propia tienda (filtro `f.derived.variant.sellerId=FALABELLA`, comprobado
de nuevo por producto). El precio es el menor de `internetPrice` y `eventPrice` sin tachar;
`cmrPrice` exige la tarjeta de la tienda y el «antes» tachado nunca se lee.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discountz.domain.catalog import CatalogObservation
from discountz.domain.errors import NotFound, ParseError, Unavailable, UnsafeUrl
from discountz.domain.model import (
    ExtractionPath,
    Lead,
    ListingRef,
    Offer,
    SearchSpec,
    SellerClass,
)
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.infra.log import event
from discountz.sources.base import (
    Capability,
    CardFacts,
    CatalogPage,
    CatalogPartition,
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

CODE = "falabella"
BASE = "https://www.falabella.com.co"
HOSTS = frozenset({"www.falabella.com.co"})
IMAGE_HOSTS = frozenset({"media.falabella.com.co"})
EXTERNAL_ID_RE = re.compile(r"^\d{1,12}$")
OWN_SELLER = (
    "FALABELLA_COLOMBIA"  # `sellerId` de lo que vende la tienda, en el listado y en la ficha
)

_NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_PRICE_TYPES = frozenset({"internetPrice", "eventPrice", "normalPrice"})  # cmrPrice nunca
_AMOUNT = re.compile(r"^\d{1,3}(?:\.\d{3})*$|^\d+$")
_PRODUCT_PATH = re.compile(r"^/falabella-co/product/(\d{1,12})(?:/|$)")
PAGE_SIZE = 48
MAX_SEEN = 100_000  # listados guardados en memoria para la confirmación y la tarjeta
_REDIRECTS = frozenset({301, 302, 307, 308})
SELLER_FILTER = "f.derived.variant.sellerId"
NO_RESULT_PATH = "/falabella-co/noResult"

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FalabellaItem:
    external_id: str
    title: str
    price: Money
    image_url: str | None
    url: str | None  # ruta de la ficha tal como la trae el listado


@dataclass(frozen=True, slots=True)
class CategoryPage:
    items: tuple[FalabellaItem, ...]
    total: int  # productos del listado, de `pagination.count`
    single: bool = False  # con un solo resultado la web responde con la ficha, no con el listado


@dataclass(frozen=True, slots=True)
class ProductInfo:
    title: str
    price: Money | None
    in_stock: bool
    marketplace: bool
    url: str | None


class _Price(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    type: str
    crossed: bool = False
    price: list[str] = Field(default_factory=list)


class _Row(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    productId: str
    displayName: str
    sellerId: str
    url: str | None = None
    prices: list[_Price] = Field(default_factory=list)
    mediaUrls: list[str] = Field(default_factory=list)


class _Pagination(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    count: int


class _Listing(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    results: list[dict[str, object]]
    pagination: _Pagination


class _Offering(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    sellerId: str


class _Variant(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    prices: list[_Price] = Field(default_factory=list)
    isPurchaseable: bool = False
    offerings: list[_Offering] = Field(default_factory=list)


class _Product(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    id: str
    name: str
    slug: str = ""
    variants: list[_Variant]


def _next_data(body: bytes) -> dict[str, object]:
    match = _NEXT_DATA.search(body)
    if match is None:
        raise ParseError("no_next_data")
    try:
        data = json.loads(match.group(1), parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ParseError("bad_json") from exc
    if not isinstance(data, dict):
        raise ParseError("bad_json")
    return data


def _page_props(data: dict[str, object]) -> dict[str, object]:
    props = data.get("props")
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    if not isinstance(page_props, dict):
        raise ParseError("bad_shape")
    return page_props


def _price(prices: Sequence[_Price]) -> Money | None:
    """El menor valor sin tachar de `internetPrice`, `eventPrice` o `normalPrice`; sin CMR."""
    amounts = [
        int(text.replace(".", ""))
        for p in prices
        if p.type in _PRICE_TYPES and not p.crossed
        for text in p.price
        if _AMOUNT.fullmatch(text)
    ]
    positive = [a for a in amounts if a > 0]
    return Money(min(positive), Currency.COP) if positive else None


def _image(urls: Sequence[str]) -> str | None:
    for raw in urls[:1]:
        try:
            return str(SafeUrl.parse(raw, IMAGE_HOSTS))
        except UnsafeUrl:
            return None
    return None


def _path(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return SafeUrl.parse(raw, HOSTS).path
    except UnsafeUrl:
        return None


def parse_category(body: bytes) -> CategoryPage:
    """Productos de la tienda de una página del listado y el total del listado.

    Lo del marketplace, los repetidos (los patrocinados se repiten) y los productos rotos se
    descartan solos. Si había productos de la tienda y ninguno sirve, el formato cambió y es
    `ParseError`. Una página con la ficha y sin listado es vacía (`single`).
    """
    page_props = _page_props(_next_data(body))
    if "results" not in page_props and "productData" in page_props:
        return CategoryPage((), 0, single=True)
    try:
        listing = _Listing.model_validate(page_props)
    except ValidationError as exc:
        raise ParseError("bad_shape") from exc
    items: dict[str, FalabellaItem] = {}
    own = 0
    skipped: Counter[str] = Counter()
    for raw in listing.results:
        try:
            row = _Row.model_validate(raw)
        except ValidationError:
            own += 1  # no se sabe de quién es: cuenta como roto
            skipped["bad_item"] += 1
            continue
        if row.sellerId != OWN_SELLER:
            continue
        own += 1
        price = _price(row.prices)
        title = row.displayName.strip()
        if not EXTERNAL_ID_RE.fullmatch(row.productId) or not title or price is None:
            skipped["bad_item"] += 1
            continue
        items.setdefault(
            row.productId,
            FalabellaItem(row.productId, title, price, _image(row.mediaUrls), _path(row.url)),
        )
    for code, count in sorted(skipped.items()):
        event(log, "falabella_items_skipped", source=CODE, code=code, count=count)
    if own and not items:
        raise ParseError("no_valid_items")
    return CategoryPage(tuple(items.values()), listing.pagination.count)


def parse_product(body: bytes) -> ProductInfo:
    """Precio, stock y vendedor de una ficha; sin vendedor en ninguna variante es `ParseError`."""
    page_props = _page_props(_next_data(body))
    try:
        product = _Product.model_validate(page_props.get("productData"))
    except ValidationError as exc:
        raise ParseError("bad_shape") from exc
    variants = [v for v in product.variants if v.offerings]
    if not variants:
        raise ParseError("no_seller")
    own = [v for v in variants if any(o.sellerId == OWN_SELLER for o in v.offerings)]
    chosen = own or variants
    buyable = [v for v in chosen if v.isPurchaseable]
    prices = [p for v in (buyable or chosen) for p in v.prices]
    slug = product.slug.strip()
    return ProductInfo(
        title=product.name.strip(),
        price=_price(prices),
        in_stock=bool(buyable),
        marketplace=not own,
        url=f"{BASE}/falabella-co/product/{product.id}/{slug}" if slug else None,
    )


@dataclass(frozen=True, slots=True)
class Category:
    """Una categoría del perfil: su id y slug en la URL y la ruta con la que se asigna el tema."""

    key: str
    slug: str
    path: str


CATEGORIES: tuple[Category, ...] = (
    Category("cat171006", "Computadores", "/tecnologia/computadores/"),
    Category("cat1660941", "Celulares-y-Telefonos", "/tecnologia/celulares/"),
    Category("cat10550940", "Audio", "/tecnologia/audio/"),
    Category("cat1360967", "Televisores-y-Video", "/tecnologia/tv y video/"),
    Category("cat10430953", "Smartwatch", "/tecnologia/smartwatch/"),
    Category("cat50590", "Gaming", "/tecnologia/gaming/"),
    Category("cat311003", "Fotografia", "/tecnologia/fotografia/"),
    Category("cat10431000", "Smart-Home", "/tecnologia/smart home/"),
    Category("cat50623", "Electrohogar", "/electrodomesticos/electrohogar/"),
    Category("cat4670964", "Ropa-Mujer", "/moda/ropa mujer/"),
    Category("cat4670965", "Ropa-para-hombre", "/moda/ropa hombre/"),
    Category("CATG36088", "Calzado-y-tenis", "/moda/calzado/"),
    Category("cat6871001", "Relojes", "/moda/relojes/"),
    Category("cat591075", "Articulos-de-escritorio-y-papeleria", "/papeleria/utiles escolares/"),
)
_BY_KEY = {c.key: c for c in CATEGORIES}


def _location(resp: HttpResponse) -> str:
    return next((v for k, v in resp.headers.items() if k.lower() == "location"), "")


class FalabellaSource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.COP,
            hosts=HOSTS,
            client="httpx",
            capabilities=frozenset({Capability.DETAIL, Capability.CONFIRM, Capability.CATALOG}),
            max_batch=1,
            external_id_re=EXTERNAL_ID_RE,
            image_hosts=IMAGE_HOSTS,
        )
        self._seen: dict[str, FalabellaItem] = {}

    # --- contrato de la v1 ------------------------------------------------------------------

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _PRODUCT_PATH.match(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # los enlaces de Falabella ya traen el id

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return [self._outcome(ctx, ref) for ref in refs]

    def fetch_catalog_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return self.fetch_listings(ctx, refs)  # la misma ficha y la misma regla de vendedor

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        return SearchPage((), 0)  # solo rastreo y /seguir (spec F4)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        outcome = self._outcome(ctx, offer.ref)
        if isinstance(outcome, ListingFailed):
            raise outcome.error
        if isinstance(outcome, ListingGone) or not outcome.offer.in_stock:
            return None
        return outcome.offer

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)

    # --- fichas ---------------------------------------------------------------------------------

    def _outcome(self, ctx: SourceContext, ref: ListingRef) -> ListingOutcome:
        try:
            info, path = self._product(ctx, ref.external_id)
        except NotFound:
            return ListingGone(ref)
        except (Unavailable, ParseError) as exc:
            return ListingFailed(ref, exc)
        if info.marketplace:  # solo lo que vende Falabella (spec F1)
            return ListingGone(ref)
        price = info.price
        offer = Offer(
            ref=ref,
            parent_external_id=None,
            title=info.title,
            price=price,
            list_price=None,  # el «antes» de la tienda no se lee: el descuento sale del histórico
            in_stock=info.in_stock and price is not None,
            seller_class=SellerClass.FIRST_PARTY,
            unit_key=pack_unit_key(info.title) or "un|1",
            extraction=ExtractionPath.EMBEDDED_JSON,
            observed_at=ctx.now,
            url_path=path,
        )
        return ListingFound(offer)

    def _product(self, ctx: SourceContext, external_id: str) -> tuple[ProductInfo, str]:
        """La ficha y su ruta canónica; sin enlace guardado, el id solo (la web lo acepta)."""
        seen = self._seen.get(external_id)
        bare = f"/falabella-co/product/{external_id}"
        resp = ctx.http.request("GET", BASE + (seen.url if seen and seen.url else bare))
        info = parse_product(resp.body)
        path = urlsplit(info.url).path if info.url else None
        return info, path or (seen.url if seen and seen.url else bare)

    # --- catálogo ---------------------------------------------------------------------------

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[CatalogPartition]:
        return [CatalogPartition(c.key, c.path) for c in CATEGORIES]

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        """Una página de 48 productos de la tienda en la categoría; `page` cuenta desde 0."""
        category = _BY_KEY.get(partition)
        if category is None:
            raise ParseError("bad_partition")
        number = page + 1
        resp = ctx.http.request(
            "GET",
            f"{BASE}/falabella-co/category/{category.key}/{category.slug}",
            params={SELLER_FILTER: "FALABELLA", "page": str(number)},
        )
        if resp.status in _REDIRECTS:
            # Con un solo resultado la web manda a su ficha: la página queda vacía.
            target = urlsplit(_location(resp)).path
            if _PRODUCT_PATH.match(target):
                return CatalogPage((), False)
            # Una página pasada del final responde con la búsqueda sin resultados.
            if target == NO_RESULT_PATH:
                return CatalogPage((), False)
            raise ParseError("bad_redirect")
        parsed = parse_category(resp.body)
        for item in parsed.items:
            if item.external_id in self._seen or len(self._seen) < MAX_SEEN:
                self._seen[item.external_id] = item
        items = tuple(
            CatalogObservation(
                i.external_id, i.price, category.path, None, SellerClass.FIRST_PARTY, title=i.title
            )
            for i in parsed.items
        )
        return CatalogPage(items, number * PAGE_SIZE < parsed.total)

    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage:
        return CatalogPage((), False)  # la tienda no lista cambios

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> list[CatalogObservation]:
        return []  # sin lectura por id barata: la vuelta completa es el único recorrido

    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]:
        """Solo de lo ya leído en esta corrida: sin red; el resto usa la ficha al confirmar."""
        facts: dict[str, CardFacts] = {}
        for external_id in external_ids:
            item = self._seen.get(external_id)
            if item is not None and item.url is not None:
                facts[external_id] = CardFacts(item.title, BASE + item.url, item.image_url)
        return facts


register(FalabellaSource())
