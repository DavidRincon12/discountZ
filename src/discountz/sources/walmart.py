"""Adaptador de Walmart US (spec §4.2 «Walmart»), detrás de la puerta de activación (§4.1).

Ruta principal: `__NEXT_DATA__` → `props.pageProps.initialData.data.product`
(`priceInfo.currentPrice.price`, `availabilityStatus`, `sellerName`). Solo se lee ese nodo:
los planes de protección («3-Year Plan - $24.00», el 24,00 del spike) viven en otros nodos y
nunca entran. Ruta independiente: el microdato del precio principal en el HTML
(`[itemprop=price][data-seo-id=hero-price]`); las fichas sin precio visible («See price in
cart») no tienen ruta.

En la ficha real ya no hay JSON-LD `Product` (solo `WebPage`), por eso la ruta independiente
no es `offers.price` como decía el spec.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError
from selectolax.lexbor import LexborHTMLParser

from discountz.domain.errors import NotFound, ParseError, SourceError
from discountz.domain.guards import check_currency_text
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money, parse_price, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    ListingFailed,
    ListingFound,
    ListingOutcome,
    NeedsResolution,
    SearchPage,
    SourceContext,
    SourceDescriptor,
    pack_unit_key,
)
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import register

CODE = "walmart_us"
BASE = "https://www.walmart.com"
ITEM_RE = re.compile(r"^\d{5,12}$")
_PATH_ITEM = re.compile(r"^/ip/(?:[^/]+/)?(\d{5,12})/?$")
_NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_FIRST_PARTY = "Walmart.com"
_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
_REDIRECTS = (301, 302, 303, 307, 308)
_SOLD_BY = re.compile(r"^Sold (?:and shipped )?by (.+)$")

Number = Decimal | int


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _Price(_Model):
    price: Number | None = None
    currencyUnit: str = "USD"


class _PriceInfo(_Model):
    currentPrice: _Price | None = None
    wasPrice: _Price | None = None


class _Product(_Model):
    usItemId: str
    name: str
    availabilityStatus: str | None = None
    sellerName: str | None = None
    priceInfo: _PriceInfo


class _Availability(_Model):
    value: str = ""


class _SearchItem(_Model):
    usItemId: str = ""
    name: str = ""
    price: Number | None = None
    isSponsoredFlag: bool | None = None
    isPreowned: bool | None = None
    sellerName: str | None = None
    availabilityStatusV2: _Availability | None = None


def _schema_error(exc: ValidationError) -> ParseError:
    first = exc.errors(include_input=False, include_url=False)[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))[:60]
    return ParseError("schema", loc=loc or None)


def _next_data(body: bytes) -> dict[str, object]:
    m = _NEXT_DATA.search(body)
    if m is None:
        raise ParseError("no_next_data", loc="__NEXT_DATA__")
    try:
        data = json.loads(m.group(1), parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise ParseError("bad_json", loc="__NEXT_DATA__") from exc
    if not isinstance(data, dict):
        raise ParseError("not_an_object", loc="__NEXT_DATA__")
    return data


def _dig(data: object, *path: str) -> object:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            raise ParseError("missing", loc=".".join(path)[:60])
        data = data[key]
    return data


def _money(value: Number | None) -> Money | None:
    if value is None:
        return None
    minor = to_minor(Decimal(value), Currency.USD)
    return Money(minor, Currency.USD) if minor > 0 else None


def _seller(name: str | None) -> SellerClass:
    if not name:
        return SellerClass.UNKNOWN
    return SellerClass.FIRST_PARTY if name == _FIRST_PARTY else SellerClass.MARKETPLACE


def _offer(
    item: str,
    title: str,
    price: Money | None,
    list_price: Money | None,
    in_stock: bool,
    seller: SellerClass,
    observed_at: datetime,
    extraction: ExtractionPath,
) -> Offer:
    title = " ".join(title.split())
    return Offer(
        ref=ListingRef(CODE, item),
        parent_external_id=None,
        title=title,
        price=price if in_stock else None,  # sin stock no hay precio comprable (G8)
        list_price=list_price,
        in_stock=in_stock and price is not None,
        seller_class=seller,
        unit_key=pack_unit_key(title) or "un|1",
        extraction=extraction,
        observed_at=observed_at,
        url_path=f"/ip/{item}",
    )


def _product(body: bytes) -> _Product:
    raw = _dig(_next_data(body), "props", "pageProps", "initialData", "data", "product")
    try:
        return _Product.model_validate(raw)
    except ValidationError as exc:
        raise _schema_error(exc) from None


def parse_walmart_detail(body: bytes, item: str, observed_at: datetime) -> Offer | None:
    """Ficha por `__NEXT_DATA__`; solo el nodo `product`, nunca garantías ni planes."""
    product = _product(body)
    current = product.priceInfo.currentPrice
    if current is not None and current.currencyUnit != "USD":
        raise ParseError("currency", loc="priceInfo.currentPrice.currencyUnit")
    was = product.priceInfo.wasPrice
    observed = product.usItemId if ITEM_RE.fullmatch(product.usItemId) else item
    return _offer(
        observed,
        product.name,
        _money(current.price if current else None),
        _money(was.price if was else None),
        product.availabilityStatus == "IN_STOCK",
        _seller(product.sellerName),
        observed_at,
        ExtractionPath.EMBEDDED_JSON,
    )


def parse_walmart_independent(body: bytes, item: str, observed_at: datetime) -> Offer | None:
    """Ruta independiente: el precio principal del HTML; `None` si la ficha no lo muestra."""
    tree = LexborHTMLParser(body)
    node = tree.css_first('[itemprop="price"][data-seo-id="hero-price"]')
    text = node.text(strip=True) if node is not None else ""
    if not text.startswith("$"):
        return None  # «See price in cart» u otra cosa que no es un precio
    check_currency_text(text, Currency.USD)
    price = Money(parse_price(text, Currency.USD), Currency.USD)
    title_node = tree.css_first("h1#main-title")
    title = title_node.text(strip=True) if title_node is not None else ""
    return _offer(
        item,
        title,
        price,
        None,
        True,  # el precio principal solo se muestra si la ficha se puede comprar
        _seller(_html_seller(tree)),
        observed_at,
        ExtractionPath.HTML,
    )


def _html_seller(tree: LexborHTMLParser) -> str | None:
    """«Sold by X» o «Sold and shipped by X» del primer bloque de vendedor."""
    node = tree.css_first('[data-testid="product-seller-info"]')
    label = (node.attributes.get("aria-label") or "") if node is not None else ""
    m = _SOLD_BY.match(label)
    return m.group(1).strip() if m else None


def parse_walmart_search(body: bytes, observed_at: datetime) -> list[Offer]:
    """Primera página: sin patrocinados, usados ni duplicados."""
    stacks = _dig(_next_data(body), "props", "pageProps", "initialData", "searchResult")
    raw_stacks = stacks.get("itemStacks") if isinstance(stacks, dict) else None
    out: list[Offer] = []
    seen: set[str] = set()
    for stack in raw_stacks if isinstance(raw_stacks, list) else []:
        items = stack.get("items") if isinstance(stack, dict) else None
        for raw in items if isinstance(items, list) else []:
            if not isinstance(raw, dict) or raw.get("__typename") != "Product":
                continue
            try:
                it = _SearchItem.model_validate(raw)
            except ValidationError as exc:
                raise _schema_error(exc) from None
            if (
                not ITEM_RE.fullmatch(it.usItemId)
                or it.usItemId in seen
                or it.isSponsoredFlag
                or it.isPreowned
                or not it.name
            ):
                continue
            seen.add(it.usItemId)
            stock = it.availabilityStatusV2.value if it.availabilityStatusV2 else ""
            out.append(
                _offer(
                    it.usItemId,
                    it.name,
                    _money(it.price),
                    None,
                    stock == "IN_STOCK",
                    _seller(it.sellerName),
                    observed_at,
                    ExtractionPath.EMBEDDED_JSON,
                )
            )
    return out


class WalmartSource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.USD,
            hosts=frozenset({"www.walmart.com", "walmart.com"}),
            client="curl",
            capabilities=frozenset({Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM}),
            max_batch=1,
            external_id_re=ITEM_RE,
        )
        self._bodies: dict[str, bytes] = {}

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _PATH_ITEM.match(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # Walmart no usa enlaces cortos

    def _get(self, ctx: SourceContext, item: str) -> bytes:
        """`/ip/<id>` redirige a `/ip/<slug>/<id>`: se sigue un salto, validado a mano."""
        resp = ctx.http.request("GET", f"{BASE}/ip/{item}", headers=_HEADERS)
        if resp.status in _REDIRECTS:
            location = {k.lower(): v for k, v in resp.headers.items()}.get("location", "")
            parts = urlsplit(location)
            m = _PATH_ITEM.match(parts.path)
            same_host = parts.netloc in ("", "www.walmart.com")
            if (
                m is None
                or m.group(1) != item
                or not same_host
                or parts.scheme not in ("", "https")
            ):
                raise NotFound(CODE, "bad_redirect")
            resp = ctx.http.request("GET", f"{BASE}{parts.path}", headers=_HEADERS)
        self._bodies[item] = resp.body
        return resp.body

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for ref in refs:
            try:
                offer = parse_walmart_detail(
                    self._get(ctx, ref.external_id), ref.external_id, ctx.now
                )
            except (SourceError, ParseError) as exc:
                out.append(ListingFailed(ref, exc))
                continue
            out.append(ListingFound(offer) if offer else ListingFailed(ref, ParseError("empty")))
        return out

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        del page  # solo la primera página
        resp = ctx.http.request("GET", f"{BASE}/search", params={"q": spec.query}, headers=_HEADERS)
        return SearchPage(parse_walmart_search(resp.body, ctx.now), None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        item = offer.ref.external_id
        body = self._bodies.get(item) or self._get(ctx, item)
        return parse_walmart_independent(body, item, ctx.now)

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


register(WalmartSource())
