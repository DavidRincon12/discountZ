"""Adaptador de Alkosto por su índice público de Algolia (spec de Alkosto, ADR 0009).

La web de Alkosto busca en Algolia con una clave de solo búsqueda que publica en su portada. El
adaptador la lee de ahí, la guarda solo en memoria del proceso y nunca la manda a logs, a la BD, a
`repr` ni a mensajes de error. El precio es `lowestprice_double` (el de la ficha); el «antes» de la
tienda (`pricevalue_cop_double`) viene inflado y nunca se lee.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discountz.domain.errors import ParseError, UnsafeUrl
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.log import event

CODE = "alkosto"
APP_ID = "QX5IPS1B1Q"
INDEX = "alkostoIndexAlgoliaPRD"
ALGOLIA = f"https://{APP_ID}-dsn.algolia.net"
BASE = "https://www.alkosto.com"
HOSTS = frozenset({"www.alkosto.com", "alkosto.com"})
IMAGE_HOSTS = frozenset({"cdn.dam.alkosto.com"})
EXTERNAL_ID_RE = re.compile(r"^[0-9A-Za-z]{1,40}$")
# Solo los campos que se usan: con ellos, 1.000 productos pesan ~1 MB.
ATTRIBUTES: tuple[str, ...] = (
    "objectID",
    "code_string",
    "name_text_es",
    "lowestprice_double",
    "instockflag_boolean",
    "stocklevel_int",
    "url_es_string",
    "img-820wx820h_string",
    "img-750wx750h_string",
    "hierarchicalcategory_string_mv",
)
# La portada la trae como `"apiKey": "<clave>"` en el bloque del buscador (clave segura de Algolia,
# en base64, con el filtro de lo que la tienda muestra).
_API_KEY = re.compile(rb'"apiKey"\s*:\s*"([A-Za-z0-9+/=_]{8,400})"')
_GTIN_LENGTHS = frozenset({8, 12, 13, 14})

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlkostoItem:
    external_id: str
    title: str
    price: Money | None  # None si no está en stock
    in_stock: bool
    ean: str | None
    category_path: str
    url: str
    image_url: str | None


class _Hit(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    objectID: str
    name_text_es: str
    url_es_string: str
    code_string: str | None = None
    lowestprice_double: Decimal | int | None = None
    instockflag_boolean: bool = False
    stocklevel_int: int = 0
    img_820: str | None = Field(None, alias="img-820wx820h_string")
    img_750: str | None = Field(None, alias="img-750wx750h_string")
    hierarchicalcategory_string_mv: dict[str, str | list[str]] = Field(default_factory=dict)


class _Query(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    hits: list[dict[str, object]]
    nbHits: int


class _Objects(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    results: list[dict[str, object] | None]


def parse_api_key(html: bytes) -> str:
    match = _API_KEY.search(html)
    if match is None:
        raise ParseError("api_key")
    return match.group(1).decode("ascii")


def _load(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ParseError("bad_json") from exc


def parse_query(body: bytes) -> tuple[list[AlkostoItem], int]:
    """Productos válidos de una consulta y su `nbHits`; un hit inválido se descarta solo."""
    try:
        page = _Query.model_validate(_load(body))
    except ValidationError as exc:
        raise ParseError("bad_query") from exc
    items = [item for raw in page.hits if (item := _item(raw)) is not None]
    return items, page.nbHits


def parse_objects(body: bytes) -> dict[str, AlkostoItem]:
    """Fichas de `getObjects` por id; los ids inexistentes (`null`) se omiten."""
    try:
        page = _Objects.model_validate(_load(body))
    except ValidationError as exc:
        raise ParseError("bad_objects") from exc
    items = (_item(raw) for raw in page.results if raw is not None)
    return {item.external_id: item for item in items if item is not None}


def _item(raw: dict[str, object]) -> AlkostoItem | None:
    try:
        return _to_item(_Hit.model_validate(raw))
    except ValidationError:
        code = "bad_item"
    except UnsafeUrl:
        code = "bad_url"
    except ParseError as exc:
        code = exc.code
    event(log, "alkosto_item_skipped", source=CODE, code=code)
    return None


def _to_item(hit: _Hit) -> AlkostoItem:
    if not EXTERNAL_ID_RE.fullmatch(hit.objectID):
        raise ParseError("bad_id")
    title = hit.name_text_es.strip()
    if not title:
        raise ParseError("no_title")
    if not hit.url_es_string.startswith("/") or hit.url_es_string.startswith("//"):
        raise UnsafeUrl("bad_url")
    url = str(SafeUrl.parse(BASE + hit.url_es_string, HOSTS))
    in_stock = hit.instockflag_boolean and hit.stocklevel_int > 0
    return AlkostoItem(
        external_id=hit.objectID,
        title=title,
        price=_price(hit.lowestprice_double) if in_stock else None,
        in_stock=in_stock,
        ean=_ean(hit.code_string),
        category_path=_category_path(hit.hierarchicalcategory_string_mv),
        url=url,
        image_url=_image(hit.img_820 or hit.img_750),
    )


def _price(value: Decimal | int | None) -> Money:
    """Pesos enteros > 0; un decimal, un cero o la falta del campo es formato roto."""
    if value is None or isinstance(value, bool):
        raise ParseError("bad_price")
    amount = Decimal(value)
    if amount <= 0 or amount != amount.to_integral_value():
        raise ParseError("bad_price")
    return Money(int(amount), Currency.COP)


def _ean(code: str | None) -> str | None:
    if code is None or not code.isdigit() or len(code) not in _GTIN_LENGTHS:
        return None
    return code


def _category_path(levels: dict[str, str | list[str]]) -> str:
    """`TV > Smart TV` ⇒ `/tv/smart tv/` (nivel 1, o el 0 si no hay)."""
    raw = levels.get("lvl1") or levels.get("lvl0") or ""
    text = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(text, str):
        return "/"
    parts = [p.strip().lower() for p in text.split(">") if p.strip()]
    return "/" + "".join(f"{p}/" for p in parts)


def _image(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return str(SafeUrl.parse(raw, IMAGE_HOSTS))
    except UnsafeUrl:
        return None
