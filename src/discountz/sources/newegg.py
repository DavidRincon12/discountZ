"""Adaptador de Newegg (spec §4.2 «Newegg», §4.3 y §3.6).

Ruta principal: el JSON-LD `Product.offers` de la ficha. Ruta independiente: el HTML del buy
box (`.product-buy-box .price-current`, `<strong>` + `<sup>`) de la misma página, con otro
parser. «Sold by Newegg» da `f`.

Las fixtures son sintéticas: Newegg responde 403 con su página CAPTCHA a la nube de la sesión
de desarrollo (el spike desde Actions dio 12/12). Se regraban con `record-fixture`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from discountz.domain.errors import ParseError, SourceError
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

CODE = "newegg"
BASE = "https://www.newegg.com"
ITEM_RE = re.compile(r"^[A-Z0-9]{8,20}$")
_PATH_ITEM = re.compile(r"^/(?:[^/]+/)?p/([A-Z0-9]{8,20})/?$")
_HREF_ITEM = re.compile(r"/p/([A-Z0-9]{8,20})(?:[/?#]|$)")
_JSON_PRICE = re.compile(r"^\d{1,7}(?:\.\d{1,2})?$")
_FIRST_PARTY = "newegg"
_IN_STOCK = ("schema.org/InStock", "schema.org/LimitedAvailability")
_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
_CAPTCHA_PATHS = ("/areyouahuman",)

Number = Decimal | int | str


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _Seller(_Model):
    name: str = ""


class _Offer(_Model):
    price: Number | None = None
    priceCurrency: str = "USD"
    availability: str = ""
    seller: _Seller | None = None


class _Product(_Model):
    name: str
    sku: str = ""
    offers: _Offer | list[_Offer]


def _tree(body: bytes) -> LexborHTMLParser:
    return LexborHTMLParser(body)


def _text(node: LexborNode | None) -> str | None:
    if node is None:
        return None
    text = " ".join(node.text(strip=False).split())
    return text or None


def _seller_class(name: str | None) -> SellerClass:
    if not name:
        return SellerClass.UNKNOWN
    return (
        SellerClass.FIRST_PARTY
        if name.strip().lower() == _FIRST_PARTY
        else (SellerClass.MARKETPLACE)
    )


def _product(tree: LexborHTMLParser) -> _Product:
    """El primer bloque JSON-LD de tipo `Product`; sin él la página no es una ficha."""
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(strip=False), parse_float=Decimal)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            try:
                return _Product.model_validate(data)
            except ValidationError as exc:
                first = exc.errors(include_input=False, include_url=False)[0]
                loc = ".".join(str(p) for p in first.get("loc", ()))[:60]
                raise ParseError("schema", loc=loc or None) from None
    raise ParseError("no_product_ld", loc="ld+json")


def _json_price(offer: _Offer) -> Money | None:
    if offer.priceCurrency != "USD":
        raise ParseError("currency", loc="offers.priceCurrency")
    raw = offer.price
    if raw is None:
        return None
    if isinstance(raw, str):
        if not _JSON_PRICE.fullmatch(raw.strip()):
            raise ParseError("bad_price", loc="offers.price")
        raw = Decimal(raw.strip())
    minor = to_minor(Decimal(raw), Currency.USD)
    return Money(minor, Currency.USD) if minor > 0 else None


def _html_price(node: LexborNode | None) -> Money | None:
    """`$<strong>1,299</strong><sup>.99</sup>` o `$1,299.99`; vacío ⇒ sin precio."""
    text = _text(node)
    if text is None:
        return None
    text = text.replace(" ", "")
    check_currency_text(text, Currency.USD)
    return Money(parse_price(text, Currency.USD), Currency.USD)


def _buy_box(tree: LexborHTMLParser) -> LexborNode | None:
    return tree.css_first(".product-buy-box")


def _list_price(scope: LexborNode | None) -> Money | None:
    return _html_price(scope.css_first(".price-was-data")) if scope is not None else None


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
        url_path=f"/p/{item}",
    )


def _observed_item(sku: str, item: str) -> str:
    return sku if ITEM_RE.fullmatch(sku) else item


def parse_newegg_detail(body: bytes, item: str, observed_at: datetime) -> Offer | None:
    """Ficha por JSON-LD. Sin `Product` es `ParseError` (página que no es una ficha)."""
    tree = _tree(body)
    product = _product(tree)
    if isinstance(product.offers, list) and not product.offers:
        raise ParseError("no_offers", loc="offers")
    offer = product.offers[0] if isinstance(product.offers, list) else product.offers
    title = " ".join(product.name.split())
    check_currency_text(title, Currency.USD)
    price = _json_price(offer)
    in_stock = any(marker in offer.availability for marker in _IN_STOCK)
    return _offer(
        _observed_item(product.sku, item),
        title,
        price,
        _list_price(_buy_box(tree)),
        in_stock,
        _seller_class(offer.seller.name if offer.seller else None),
        observed_at,
        ExtractionPath.EMBEDDED_JSON,
    )


def _html_seller(box: LexborNode) -> SellerClass:
    node = box.css_first(".product-seller strong")
    return _seller_class(_text(node))


def parse_newegg_independent(body: bytes, item: str, observed_at: datetime) -> Offer | None:
    """Ruta independiente: el HTML del buy box; `None` si la página no lo trae."""
    tree = _tree(body)
    box = _buy_box(tree)
    if box is None:
        return None
    price = _html_price(box.css_first(".product-price .price-current"))
    inventory = (_text(box.css_first(".product-inventory")) or "").lower()
    title = _text(tree.css_first("h1.product-title")) or ""
    return _offer(
        item,
        title,
        price,
        _list_price(box),
        "out of stock" not in inventory,
        _html_seller(box),
        observed_at,
        ExtractionPath.HTML,
    )


def _search_seller(item: str) -> SellerClass:
    """La tarjeta no muestra el vendedor: los Item# `N82E…` son de Newegg y los demás de
    terceros. Es provisional: la confirmación exige el mismo vendedor y la ficha decide."""
    return SellerClass.FIRST_PARTY if item.startswith("N82E") else SellerClass.MARKETPLACE


def parse_newegg_search(body: bytes, observed_at: datetime) -> list[Offer]:
    """Primera página de `/p/pl`: sin patrocinados ni duplicados."""
    out: list[Offer] = []
    seen: set[str] = set()
    for cell in _tree(body).css(".item-cell"):
        link = cell.css_first("a.item-title")
        if link is None or cell.css_first(".item-sponsored") is not None:
            continue
        m = _HREF_ITEM.search(link.attributes.get("href") or "")
        title = _text(link)
        if m is None or title is None or m.group(1) in seen:
            continue
        item = m.group(1)
        seen.add(item)
        price = _html_price(cell.css_first(".price-current"))
        promo = (_text(cell.css_first(".item-promo")) or "").lower()
        out.append(
            _offer(
                item,
                title,
                price,
                _list_price(cell),
                "out of stock" not in promo,
                _search_seller(item),
                observed_at,
                ExtractionPath.HTML,
            )
        )
    return out


class NeweggSource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.USD,
            hosts=frozenset({"www.newegg.com", "newegg.com"}),
            client="httpx",
            capabilities=frozenset({Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM}),
            max_batch=1,
            external_id_re=ITEM_RE,
        )
        # item → cuerpo de la última ficha leída en este proceso: la ruta independiente vive
        # en la misma página y no merece otra petición.
        self._bodies: dict[str, bytes] = {}

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _PATH_ITEM.match(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # Newegg no usa enlaces cortos

    def _get(self, ctx: SourceContext, item: str) -> bytes:
        resp = ctx.http.request("GET", f"{BASE}/p/{item}", headers=_HEADERS)
        self._bodies[item] = resp.body
        return resp.body

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for ref in refs:
            try:
                offer = parse_newegg_detail(
                    self._get(ctx, ref.external_id), ref.external_id, ctx.now
                )
            except (SourceError, ParseError) as exc:
                out.append(ListingFailed(ref, exc))
                continue
            out.append(ListingFound(offer) if offer else ListingFailed(ref, ParseError("empty")))
        return out

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        del page  # solo la primera página
        resp = ctx.http.request("GET", f"{BASE}/p/pl", params={"d": spec.query}, headers=_HEADERS)
        return SearchPage(parse_newegg_search(resp.body, ctx.now), None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        item = offer.ref.external_id
        body = self._bodies.get(item) or self._get(ctx, item)
        return parse_newegg_independent(body, item, ctx.now)

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        common = detect_block_common(resp)
        if common is not None:
            return common
        if urlsplit(resp.url).path.lower().startswith(_CAPTCHA_PATHS):
            return BlockKind.CAPTCHA
        if b"<title>CAPTCHA page</title>" in resp.body[:65_536]:
            return BlockKind.CAPTCHA
        return None


register(NeweggSource())
