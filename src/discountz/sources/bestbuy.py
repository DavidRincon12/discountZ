"""Adaptador de Best Buy US (spec §4.2 «Best Buy»), detrás de la puerta de activación (§4.1).

Ruta principal: JSON-LD `Product.offers` (precio y `availability`). Ruta independiente: el
estado de Apollo que la ficha trae en línea (`ApolloSSRDataTransport`): el `customerPrice` del
`ItemPrice` con el mismo `skuId`, que deja fuera garantías y membresía Plus, y los
`buttonStates` de compra. La ficha no trae el precio renderizado en HTML. El precio regular sale
de `displayableRegularPrice`. Best Buy siempre vende: `f`.

Medido el 2026-10-13 desde la nube de la sesión: la búsqueda responde, pero su HTML trae los
productos SIN precio (llega después por GraphQL) y el único «$49.99» es la membresía Plus (el
precio constante del spike); las fichas responden 502 o se cuelgan. Sus fixtures se grabaron el
2026-10-14 desde la red de David. `/site/<sku>.p?skuId=<sku>` redirige a `/product/<slug>/<bsin>`.
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

CODE = "bestbuy_us"
BASE = "https://www.bestbuy.com"
SKU_RE = re.compile(r"^\d{6,8}$")
_SITE_PATH = re.compile(r"^/site/(?:[^/]+/)?(\d{6,8})\.p/?$")
_PRODUCT_PATH = re.compile(r"^/product/[^/]+/[A-Z0-9]+/sku/(\d{6,8})/?$")
_JSON_PRICE = re.compile(r"^\d{1,7}(?:\.\d{1,2})?$")
_IN_STOCK = ("schema.org/InStock", "schema.org/LimitedAvailability")
_APOLLO_PRICE = re.compile(rb'\{[^{}]*"customerPrice":(\d{1,7}(?:\.\d{1,2})?)(?![\d.])[^{}]*')
_REGULAR_PRICE = re.compile(rb'"displayableRegularPrice":(\d{1,7}(?:\.\d{1,2})?)(?![\d.])')
_BUTTON_STATE = re.compile(rb'"buttonStates":\[\{"buttonState":"([A-Z_]+)"')
_CELL_PRICE = ".priceView-customer-price span[aria-hidden=true]"
_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
_REDIRECTS = (301, 302, 303, 307, 308)

Number = Decimal | int | str


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _Offer(_Model):
    price: Number | None = None
    priceCurrency: str = "USD"
    availability: str = ""


class _Product(_Model):
    name: str
    sku: str = ""
    offers: _Offer | list[_Offer]


def _text(node: LexborNode | None) -> str | None:
    if node is None:
        return None
    text = " ".join(node.text(strip=False).split())
    return text or None


def _product(tree: LexborHTMLParser) -> _Product:
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
    text = _text(node)
    if text is None:
        return None
    check_currency_text(text, Currency.USD)
    return Money(parse_price(text.replace(" ", ""), Currency.USD), Currency.USD)


def _sold_out(scope: LexborNode | None) -> bool:
    button = _text(scope.css_first(".add-to-cart-button")) if scope is not None else None
    return button is not None and "sold out" in button.lower()


def _usd(raw: bytes) -> Money:
    return Money(to_minor(Decimal(raw.decode()), Currency.USD), Currency.USD)


def _regular_price(body: bytes) -> Money | None:
    """`displayableRegularPrice` del estado Apollo, solo si todas sus apariciones coinciden."""
    found = set(_REGULAR_PRICE.findall(body))
    return _usd(found.pop()) if len(found) == 1 else None


def _apollo_price(body: bytes, sku: str) -> Money | None:
    marker = b'"skuId":"' + sku.encode() + b'"'
    found = {m[1] for m in _APOLLO_PRICE.finditer(body) if marker in m[0]}
    if len(found) > 1:
        raise ParseError("price_mismatch", loc="apollo.customerPrice")
    return _usd(found.pop()) if found else None


def _offer(
    sku: str,
    title: str,
    price: Money | None,
    list_price: Money | None,
    in_stock: bool,
    observed_at: datetime,
    extraction: ExtractionPath,
) -> Offer:
    return Offer(
        ref=ListingRef(CODE, sku),
        parent_external_id=None,
        title=title,
        price=price if in_stock else None,  # sin stock no hay precio comprable (G8)
        list_price=list_price,
        in_stock=in_stock and price is not None,
        seller_class=SellerClass.FIRST_PARTY,
        unit_key=pack_unit_key(title) or "un|1",
        extraction=extraction,
        observed_at=observed_at,
        url_path=f"/site/{sku}.p?skuId={sku}",
    )


def parse_bestbuy_detail(body: bytes, sku: str, observed_at: datetime) -> Offer | None:
    """Ficha por JSON-LD. Sin `Product` es `ParseError` (página que no es una ficha)."""
    tree = LexborHTMLParser(body)
    product = _product(tree)
    if isinstance(product.offers, list) and not product.offers:
        raise ParseError("no_offers", loc="offers")
    offer = product.offers[0] if isinstance(product.offers, list) else product.offers
    title = " ".join(product.name.split())
    check_currency_text(title, Currency.USD)
    observed = product.sku if SKU_RE.fullmatch(product.sku) else sku
    return _offer(
        observed,
        title,
        _json_price(offer),
        _regular_price(body),
        any(marker in offer.availability for marker in _IN_STOCK),
        observed_at,
        ExtractionPath.EMBEDDED_JSON,
    )


def parse_bestbuy_independent(body: bytes, sku: str, observed_at: datetime) -> Offer | None:
    """Ruta independiente: precio y botones del estado Apollo; `None` si no hay precio del sku."""
    price = _apollo_price(body, sku)
    if price is None:
        return None
    title = _text(LexborHTMLParser(body).css_first("h1")) or ""
    buttons = _BUTTON_STATE.findall(body)
    return _offer(
        sku,
        title,
        price,
        _regular_price(body),
        bool(buttons) and all(b == b"ADD_TO_CART" for b in buttons),
        observed_at,
        ExtractionPath.HTML,
    )


def parse_bestbuy_search(body: bytes, observed_at: datetime) -> list[Offer]:
    """Celdas `li.product-list-item` con precio; las que no lo traen en el HTML se omiten
    (guardarlas sin precio registraría un «sin stock» falso)."""
    out: list[Offer] = []
    seen: set[str] = set()
    for cell in LexborHTMLParser(body).css("li.product-list-item"):
        sku = cell.attributes.get("data-testid") or ""
        heading = cell.css_first("h3.product-title")
        title = heading.attributes.get("title") if heading is not None else None
        if not SKU_RE.fullmatch(sku) or sku in seen or not title:
            continue
        price = _html_price(cell.css_first(_CELL_PRICE))
        if price is None:
            continue
        seen.add(sku)
        title = " ".join(title.split())
        out.append(
            _offer(sku, title, price, None, not _sold_out(cell), observed_at, ExtractionPath.HTML)
        )
    return out


class BestBuySource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.USD,
            hosts=frozenset({"www.bestbuy.com", "bestbuy.com"}),
            client="curl",
            capabilities=frozenset({Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM}),
            max_batch=1,
            external_id_re=SKU_RE,
        )
        self._bodies: dict[str, bytes] = {}

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _SITE_PATH.match(url.path) or _PRODUCT_PATH.match(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # Best Buy no usa enlaces cortos

    def _get(self, ctx: SourceContext, sku: str) -> bytes:
        """`/site/<sku>.p` redirige a `/product/<slug>/<bsin>`: un salto, validado a mano."""
        params = {"skuId": sku, "intl": "nosplash"}
        resp = ctx.http.request("GET", f"{BASE}/site/{sku}.p", params=params, headers=_HEADERS)
        if resp.status in _REDIRECTS:
            location = {k.lower(): v for k, v in resp.headers.items()}.get("location", "")
            parts = urlsplit(location)
            ok_host = parts.netloc in ("", "www.bestbuy.com") and parts.scheme in ("", "https")
            if not ok_host or not parts.path.startswith("/product/"):
                raise NotFound(CODE, "bad_redirect")
            resp = ctx.http.request(
                "GET", f"{BASE}{parts.path}", params={"intl": "nosplash"}, headers=_HEADERS
            )
        self._bodies[sku] = resp.body
        return resp.body

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for ref in refs:
            try:
                offer = parse_bestbuy_detail(
                    self._get(ctx, ref.external_id), ref.external_id, ctx.now
                )
            except (SourceError, ParseError) as exc:
                out.append(ListingFailed(ref, exc))
                continue
            out.append(ListingFound(offer) if offer else ListingFailed(ref, ParseError("empty")))
        return out

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        del page  # solo la primera página
        params = {"st": spec.query, "intl": "nosplash"}
        resp = ctx.http.request(
            "GET", f"{BASE}/site/searchpage.jsp", params=params, headers=_HEADERS
        )
        return SearchPage(parse_bestbuy_search(resp.body, ctx.now), None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        sku = offer.ref.external_id
        body = self._bodies.get(sku) or self._get(ctx, sku)
        return parse_bestbuy_independent(body, sku, ctx.now)

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


register(BestBuySource())
