"""Adaptador de Mercado Libre Colombia por la API de su catálogo (spec de Mercado Libre, ADR 0018).

Con el token de la app solo abren el catálogo de productos, sus vendedores con precio y la
reputación de cada vendedor: la búsqueda general y las publicaciones dan 403. El precio es el del
vendedor elegible más barato (nuevo, sin envío internacional y en pesos); `original_price` nunca se
lee, porque el descuento sale del histórico propio.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discountz.domain.errors import ParseError, UnsafeUrl
from discountz.domain.money import Currency, Money, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.log import event

CODE = "mercadolibre"
IMAGE_HOSTS = frozenset({"http2.mlstatic.com"})
EXTERNAL_ID_RE = re.compile(r"^MCO\d{1,12}$")
_GREEN = "5_green"
_LEADERS = frozenset({"platinum", "gold"})
_MAX_PRICE = 10**10  # más que esto en pesos no es un precio, y no cabría en el redondeo

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MeliSeller:
    seller_id: int
    price: Money
    official: bool  # tienda oficial de la marca o de la cadena (`official_store_id`)
    free_shipping: bool


@dataclass(frozen=True, slots=True)
class MeliProduct:
    name: str
    image_url: str | None


class _Results(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    results: list[object]


class _Shipping(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    free_shipping: bool = False


class _Seller(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    seller_id: int
    price: int | Decimal
    currency_id: str
    condition: str
    international_delivery_mode: str
    official_store_id: int | None = None
    shipping: _Shipping = Field(default_factory=_Shipping)


class _Product(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    name: str
    pictures: list[object] | None = None


class _Reputation(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    level_id: str | None = None
    power_seller_status: str | None = None


class _User(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    seller_reputation: _Reputation


def _json(body: bytes, code: str) -> dict[str, object]:
    try:
        data = json.loads(body, parse_float=Decimal)
    except ValueError as exc:  # incluye UnicodeDecodeError
        raise ParseError(code) from exc
    if not isinstance(data, dict):
        raise ParseError(code)
    return data


def parse_search(body: bytes) -> str | None:
    """El id del primer producto colombiano (`MCO…`) de una búsqueda, o nada si no hay."""
    try:
        search = _Results.model_validate(_json(body, "bad_search"))
    except ValidationError as exc:
        raise ParseError("bad_search") from exc
    for raw in search.results:
        found = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(found, str) and EXTERNAL_ID_RE.fullmatch(found):
            return found
    return None


def _seller(row: _Seller) -> MeliSeller | None:
    """El vendedor si es elegible; `None` si no lo es o si su precio no sirve."""
    if (row.condition, row.international_delivery_mode, row.currency_id) != ("new", "none", "COP"):
        return None
    if not 0 < row.price <= _MAX_PRICE:
        return None
    minor = to_minor(Decimal(row.price), Currency.COP)
    if minor <= 0:
        return None
    return MeliSeller(
        row.seller_id,
        Money(minor, Currency.COP),
        official=row.official_store_id is not None,
        free_shipping=row.shipping.free_shipping,
    )


def parse_sellers(body: bytes) -> list[MeliSeller]:
    """Vendedores elegibles de un producto, del más barato al más caro (empate por id).

    Un vendedor que no valida se descarta solo; si había vendedores y ninguno valida, el formato
    cambió y es `ParseError`.
    """
    try:
        items = _Results.model_validate(_json(body, "bad_items"))
    except ValidationError as exc:
        raise ParseError("bad_items") from exc
    sellers: list[MeliSeller] = []
    invalid = 0
    for raw in items.results:
        try:
            row = _Seller.model_validate(raw)
        except ValidationError:
            invalid += 1
            continue
        seller = _seller(row)
        if seller is not None:
            sellers.append(seller)
    if invalid:
        event(log, "mercadolibre_sellers_skipped", source=CODE, count=invalid)
    if items.results and invalid == len(items.results):
        raise ParseError("bad_items")
    return sorted(sellers, key=lambda s: (s.price.minor, s.seller_id))


def _picture(pictures: Sequence[object]) -> str | None:
    """La primera foto, solo si está en el CDN de Mercado Libre por https."""
    first = pictures[0] if pictures else None
    raw = first.get("url") if isinstance(first, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return str(SafeUrl.parse(raw, IMAGE_HOSTS))
    except UnsafeUrl:
        return None


def parse_product(body: bytes) -> MeliProduct:
    """Nombre y primera foto de un producto del catálogo; sin nombre es `ParseError`."""
    try:
        product = _Product.model_validate(_json(body, "bad_product"))
    except ValidationError as exc:
        raise ParseError("bad_product") from exc
    name = product.name.strip()
    if not name:
        raise ParseError("bad_product")
    return MeliProduct(name, _picture(product.pictures or []))


def parse_leader(body: bytes) -> str | None:
    """`"platinum"` o `"gold"` si el vendedor es MercadoLíder con reputación verde; si no, nada."""
    try:
        user = _User.model_validate(_json(body, "bad_user"))
    except ValidationError as exc:
        raise ParseError("bad_user") from exc
    reputation = user.seller_reputation
    if reputation.level_id != _GREEN or reputation.power_seller_status not in _LEADERS:
        return None
    return reputation.power_seller_status
