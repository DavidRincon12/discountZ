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
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discountz.domain.errors import ParseError, UnsafeUrl
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.log import event

CODE = "homecenter"
BASE = "https://www.homecenter.com.co"
HOSTS = frozenset({"www.homecenter.com.co"})
IMAGE_HOSTS = frozenset({"media.falabella.com"})
EXTERNAL_ID_RE = re.compile(r"^\d{1,12}$")

_NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_PRODUCT_LINK = re.compile(rb'href="(/homecenter-co/product/(\d{1,12})/[^"\s<>]+/)"')
_PRICE_TYPES = ("INTERNET", "NORMAL")  # por orden de preferencia; CMR (tarjeta) nunca

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
