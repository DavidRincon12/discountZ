"""Adaptador de Homecenter por el JSON de sus páginas de categoría (spec de Homecenter, ADR 0016).

La web de Homecenter es Next.js: cada categoría trae sus 40 productos en `__NEXT_DATA__`. El
precio es `INTERNET`, o `NORMAL` si no hay; `CMR` exige la tarjeta de la tienda y nunca se lee.
El listado no trae vendedor: la ficha sí (`is_marketplace_product`) y la confirmación descarta
lo del marketplace.
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

CODE = "homecenter"
BASE = "https://www.homecenter.com.co"
HOSTS = frozenset({"www.homecenter.com.co"})
IMAGE_HOSTS = frozenset({"media.falabella.com"})
EXTERNAL_ID_RE = re.compile(r"^\d{1,12}$")

_NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_PRODUCT_LINK = re.compile(rb'href="(/homecenter-co/product/(\d{1,12})/[^"\s<>]+/)"')
_PRICE_TYPES = ("INTERNET", "NORMAL")  # por orden de preferencia; CMR (tarjeta) nunca
_PRODUCT_PATH = re.compile(r"^/homecenter-co/product/(\d{1,12})/")
PAGE_SIZE = 40
MAX_SEEN = 100_000  # listados guardados en memoria para la confirmación y la tarjeta
_REDIRECTS = frozenset({301, 302, 307, 308})

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HomecenterItem:
    external_id: str
    title: str
    price: Money
    image_url: str | None
    path: str | None  # enlace de la ficha tal como lo trae el listado


@dataclass(frozen=True, slots=True)
class CategoryPage:
    items: tuple[HomecenterItem, ...]
    total: int  # productos de la categoría, de `pagination.count`


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
    priceWithoutFormatting: int


class _Row(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    productId: str
    displayName: str
    prices: list[_Price] = Field(default_factory=list)
    isPromoted: bool = False
    mediaUrls: list[str] = Field(default_factory=list)


class _Pagination(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    count: int


class _Search(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    results: list[dict[str, object]]
    pagination: _Pagination


class _Availability(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    hasStock: bool = False


class _Variant(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    price: list[_Price] = Field(default_factory=list)
    availability: list[_Availability] = Field(default_factory=list)


class _Attribute(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    name: str
    values: list[str] = Field(default_factory=list)


class _Result(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    name: str
    attributes: list[_Attribute] = Field(default_factory=list)
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


def _dig(data: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            raise ParseError("bad_shape")
        data = data[key]
    return data


def _price(prices: Sequence[_Price]) -> Money | None:
    """`INTERNET`, si no `NORMAL`, en pesos enteros > 0; sin ninguno de los dos, nada."""
    by_type = {p.type: p.priceWithoutFormatting for p in prices}
    for kind in _PRICE_TYPES:
        value = by_type.get(kind)
        if value is not None and value > 0:
            return Money(value, Currency.COP)
    return None


def _image(urls: Sequence[str]) -> str | None:
    for raw in urls[:1]:
        try:
            return str(SafeUrl.parse(raw, IMAGE_HOSTS))
        except UnsafeUrl:
            return None
    return None


def parse_category(body: bytes) -> CategoryPage:
    """Productos legibles de una página de categoría y el total de la categoría.

    Un producto roto o repetido se descarta solo, y los patrocinados no cuentan. Si había
    productos y ninguno sirve, el formato cambió y es `ParseError`.
    """
    data = _next_data(body)
    try:
        search = _Search.model_validate(
            _dig(data, "props", "pageProps", "searchProps", "searchData")
        )
    except ValidationError as exc:
        raise ParseError("bad_shape") from exc
    links = {
        m.group(2).decode(): m.group(1).decode()
        for m in reversed(list(_PRODUCT_LINK.finditer(body)))
    }
    items: dict[str, HomecenterItem] = {}
    candidates = 0
    skipped: Counter[str] = Counter()
    for raw in search.results:
        try:
            row = _Row.model_validate(raw)
        except ValidationError:
            candidates += 1
            skipped["bad_item"] += 1
            continue
        if row.isPromoted:
            continue
        candidates += 1
        price = _price(row.prices)
        title = row.displayName.strip()
        if not EXTERNAL_ID_RE.fullmatch(row.productId) or not title or price is None:
            skipped["bad_item"] += 1
            continue
        items.setdefault(
            row.productId,
            HomecenterItem(
                row.productId, title, price, _image(row.mediaUrls), links.get(row.productId)
            ),
        )
    for code, count in sorted(skipped.items()):
        event(log, "homecenter_items_skipped", source=CODE, code=code, count=count)
    if candidates and not items:
        raise ParseError("no_valid_items")
    return CategoryPage(tuple(items.values()), search.pagination.count)


def parse_product(body: bytes) -> ProductInfo:
    """Precio, stock y vendedor de una ficha; sin el atributo del vendedor es `ParseError`."""
    data = _next_data(body)
    props = _dig(data, "props", "pageProps", "productProps")
    try:
        result = _Result.model_validate(_dig(props, "result"))
    except ValidationError as exc:
        raise ParseError("bad_shape") from exc
    flag = next((a for a in result.attributes if a.name == "is_marketplace_product"), None)
    if flag is None or not flag.values:
        raise ParseError("no_seller")
    if not result.variants:
        raise ParseError("no_variant")
    variant = result.variants[0]
    canonical = props.get("canonicalUrl") if isinstance(props, dict) else None
    return ProductInfo(
        title=result.name.strip(),
        price=_price(variant.price),
        in_stock=any(a.hasStock for a in variant.availability),
        marketplace=flag.values[0].strip().lower() == "true",
        url=_canonical(canonical),
    )


def _canonical(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return str(SafeUrl.parse(raw, HOSTS))
    except UnsafeUrl:
        return None


@dataclass(frozen=True, slots=True)
class Category:
    """Una categoría del perfil: su id y slug en la URL y la ruta con la que se asigna el tema."""

    key: str
    slug: str
    path: str


_HERRAMIENTAS = "/herramientas y ferreteria/"
_TECNOLOGIA = "/tecnologia/"
CATEGORIES: tuple[Category, ...] = (
    Category(
        "cat1670400",
        "herramientas-electricas-e-inalambricas",
        _HERRAMIENTAS + "herramientas electricas e inalambricas/",
    ),
    Category(
        "cat300017",
        "destornilladores-electricos-y-llaves-de-impacto",
        _HERRAMIENTAS + "destornilladores electricos y llaves de impacto/",
    ),
    Category("cat300031", "sierras-electricas-y-accesorios", _HERRAMIENTAS + "sierras electricas/"),
    Category("cat300056", "kit-de-herramientas-manuales", _HERRAMIENTAS + "kits manuales/"),
    Category("cat1670384", "multiherramientas-y-navajas", _HERRAMIENTAS + "multiherramientas/"),
    Category("cat4230187", "otras-herramientas-de-medicion", _HERRAMIENTAS + "medicion/"),
    Category("cat300065", "cajas-de-herramientas", _HERRAMIENTAS + "cajas de herramientas/"),
    Category("cat10684", "herramientas-para-electricistas", _HERRAMIENTAS + "electricistas/"),
    Category("cat300008", "soldadoras-y-complementos", _HERRAMIENTAS + "soldadoras/"),
    Category(
        "cat370014",
        "computadores-tablets-e-impresoras",
        _TECNOLOGIA + "computadores, tablets e impresoras/",
    ),
    Category("cat910126", "celulares-y-smartphones", _TECNOLOGIA + "celulares y smartphones/"),
    Category("cat920005", "smartwatch-y-dispositivos", _TECNOLOGIA + "smartwatch y dispositivos/"),
    Category(
        "cat10330", "sillas-de-escritorio-y-oficina", "/hogar/sillas de escritorio y oficina/"
    ),
)
_BY_KEY = {c.key: c for c in CATEGORIES}


def _location(resp: HttpResponse) -> str:
    return next((v for k, v in resp.headers.items() if k.lower() == "location"), "")


class HomecenterSource:
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
        self._seen: dict[str, HomecenterItem] = {}

    # --- contrato de la v1 ------------------------------------------------------------------

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _PRODUCT_PATH.match(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # los enlaces de Homecenter ya traen el id

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return [self._outcome(ctx, ref) for ref in refs]

    def fetch_catalog_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return self.fetch_listings(ctx, refs)  # la misma ficha y la misma regla de vendedor

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        return SearchPage((), 0)  # la búsqueda de la tienda está cerrada por robots.txt

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
        if info.marketplace:  # solo lo que vende Homecenter (spec H3)
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
        """La ficha y su ruta canónica; sin enlace guardado, el id solo y un salto de `301`."""
        seen = self._seen.get(external_id)
        path = seen.path if seen is not None and seen.path else None
        resp = ctx.http.request(
            "GET", BASE + (path or f"/homecenter-co/product/{external_id}/x/{external_id}/")
        )
        if resp.status in _REDIRECTS:
            try:
                path = SafeUrl.parse(_location(resp), HOSTS).path
            except UnsafeUrl as exc:
                raise ParseError("bad_redirect") from exc
            resp = ctx.http.request("GET", BASE + path)
        info = parse_product(resp.body)
        if info.url is not None:
            path = urlsplit(info.url).path
        return info, path or f"/homecenter-co/product/{external_id}/x/{external_id}/"

    # --- catálogo ---------------------------------------------------------------------------

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[CatalogPartition]:
        return [CatalogPartition(c.key, c.path) for c in CATEGORIES]

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        """Una página de 40 productos de la categoría; `page` cuenta desde 0."""
        category = _BY_KEY.get(partition)
        if category is None:
            raise ParseError("bad_partition")
        number = page + 1
        resp = ctx.http.request(
            "GET",
            f"{BASE}/homecenter-co/category/{category.key}/{category.slug}/",
            params={"currentpage": str(number)} if number > 1 else None,
        )
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
            if item is not None and item.path is not None:
                facts[external_id] = CardFacts(item.title, BASE + item.path, item.image_url)
        return facts


register(HomecenterSource())
