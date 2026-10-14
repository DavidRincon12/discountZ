"""Adaptador de Newegg (spec §4.2 «Newegg», §4.3 y §3.6).

Ruta principal: `window.__initialState__.ItemDetail` de la ficha (`FinalPrice`, `Instock`,
`Seller.SellerName`, `Description.Title`); la ficha real ya no trae JSON-LD `Product`. Ruta
independiente: el HTML del buy box (`.price-current_2026`, `<strong>` + `<sup>`, y «Sold by»)
de la misma página, con otro parser. «Sold by Newegg» da `f`; el buy box de un Item# `N82E…`
puede ser de un tercero.

Las fichas se grabaron el 2026-10-14 desde la red de David (`/p/<item>` responde 301 hacia
`/<slug>/p/<item>`). La búsqueda sigue sintética: Newegg responde 403 con su CAPTCHA a la nube
de la sesión y a la red de David (el spike desde Actions dio 12/12).
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

CODE = "newegg"
BASE = "https://www.newegg.com"
ITEM_RE = re.compile(r"^[A-Z0-9]{8,20}$")
_PATH_ITEM = re.compile(r"^/(?:[^/]+/)?p/([A-Z0-9]{8,20})/?$")
_HREF_ITEM = re.compile(r"/p/([A-Z0-9]{8,20})(?:[/?#]|$)")
_JSON_PRICE = re.compile(r"^\d{1,7}(?:\.\d{1,2})?$")
_FIRST_PARTY = "newegg"
_STATE_PREFIX = "window.__initialState__"
_BOX_PRICE = ".price-current_2026, .product-price .price-current"
_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
_CAPTCHA_PATHS = ("/areyouahuman",)
_REDIRECTS = (301, 302, 303, 307, 308)

Number = Decimal | int | str


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _Seller(_Model):
    SellerName: str = ""


class _Description(_Model):
    Title: str


class _ItemDetail(_Model):
    FinalPrice: Number | None = None
    Instock: bool = False
    Seller: _Seller | None = None
    Description: _Description


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


def _item_detail(tree: LexborHTMLParser) -> _ItemDetail:
    """`ItemDetail` del estado inicial de la página; sin él la página no es una ficha."""
    for node in tree.css("script"):
        text = node.text(strip=False).strip()
        if not text.startswith(_STATE_PREFIX):
            continue
        raw = text[len(_STATE_PREFIX) :].lstrip(" =").rstrip("; \n")
        try:
            data = json.loads(raw, parse_float=Decimal)
        except (ValueError, RecursionError):
            raise ParseError("bad_state", loc="__initialState__") from None
        detail = data.get("ItemDetail") if isinstance(data, dict) else None
        if not isinstance(detail, dict):
            break
        try:
            return _ItemDetail.model_validate(detail)
        except ValidationError as exc:
            first = exc.errors(include_input=False, include_url=False)[0]
            loc = ".".join(str(p) for p in first.get("loc", ()))[:60]
            raise ParseError("schema", loc=loc or None) from None
    raise ParseError("no_item_detail", loc="__initialState__")


def _json_price(raw: Number | None) -> Money | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if not _JSON_PRICE.fullmatch(raw.strip()):
            raise ParseError("bad_price", loc="ItemDetail.FinalPrice")
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


def parse_newegg_detail(body: bytes, item: str, observed_at: datetime) -> Offer | None:
    """Ficha por `__initialState__`. Sin `ItemDetail` es `ParseError` (no es una ficha)."""
    tree = _tree(body)
    detail = _item_detail(tree)
    title = " ".join(detail.Description.Title.split())
    check_currency_text(title, Currency.USD)
    return _offer(
        item,
        title,
        _json_price(detail.FinalPrice),
        _list_price(_buy_box(tree)),
        detail.Instock,
        _seller_class(detail.Seller.SellerName if detail.Seller else None),
        observed_at,
        ExtractionPath.EMBEDDED_JSON,
    )


def _html_seller(box: LexborNode) -> SellerClass:
    link = box.css_first(".product-seller-sold-by a[title]")
    if link is not None:
        return _seller_class(link.attributes.get("title"))
    return _seller_class(_text(box.css_first(".product-seller strong")))


def _html_in_stock(box: LexborNode) -> bool:
    inventory = (_text(box.css_first(".product-inventory")) or "").lower()
    button = (_text(box.css_first(".btn-primary")) or "").lower()
    return "out of stock" not in inventory and "add to cart" in button


def parse_newegg_independent(body: bytes, item: str, observed_at: datetime) -> Offer | None:
    """Ruta independiente: el HTML del buy box; `None` si la página no lo trae."""
    tree = _tree(body)
    box = _buy_box(tree)
    if box is None:
        return None
    price = _html_price(box.css_first(_BOX_PRICE))
    title = _text(tree.css_first("h1.product-title")) or ""
    return _offer(
        item,
        title,
        price,
        _list_price(box),
        _html_in_stock(box),
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
        """`/p/<item>` redirige a `/<slug>/p/<item>`: un salto, validado a mano."""
        resp = ctx.http.request("GET", f"{BASE}/p/{item}", headers=_HEADERS)
        if resp.status in _REDIRECTS:
            location = {k.lower(): v for k, v in resp.headers.items()}.get("location", "")
            parts = urlsplit(location)
            ok_host = parts.netloc in ("", "www.newegg.com") and parts.scheme in ("", "https")
            m = _PATH_ITEM.match(parts.path)
            if not ok_host or m is None or m.group(1) != item:
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
