"""Adaptador de Amazon US (spec §4.2, §4.3 y §3.6).

Ruta principal: el buybox (`#corePrice_feature_div .a-offscreen`). Ruta independiente:
`input#twister-plus-price-data-price` de la misma ficha (otro nodo y otro parser); si no está
en la página, no hay ruta y un error de precio nunca alerta. En búsquedas se descartan los
patrocinados y solo cuenta el nodo de precio principal.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from discountz.domain.errors import NotFound, ParseError, SourceError
from discountz.domain.guards import check_currency_text
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money, parse_price
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

CODE = "amazon_us"
BASE = "https://www.amazon.com"
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_PATH_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)")
# `pending_ref` solo admite [A-Za-z0-9/_|-]: el host corto va como alias sin puntos.
_SHORT_HOSTS = {"a.co": "aco", "amzn.to": "amznto"}
_SHORT_ALIAS = {v: k for k, v in _SHORT_HOSTS.items()}
_SHORT_PATH = re.compile(r"^/[A-Za-z0-9/_-]{1,120}$")
MAX_HOPS = 3
COOKIES = {"i18n-prefs": "USD", "lc-main": "en_US"}
_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
_UNAVAILABLE = ("currently unavailable", "out of stock", "unavailable")
_AKAMAI_MARKERS = (b"bm-verify=", b"/_sec/verify?provider=interstitial")


def _tree(body: bytes) -> LexborHTMLParser:
    return LexborHTMLParser(body)


def _text(node: LexborNode | None) -> str | None:
    if node is None:
        return None
    text = node.text(strip=True)
    return text or None


def _price(text: str | None) -> Money | None:
    """Precio en USD; un marcador de otra moneda es `ParseError` (G2, U14)."""
    if text is None:
        return None
    check_currency_text(text, Currency.USD)
    return Money(parse_price(text.strip(), Currency.USD), Currency.USD)


def _seller(tree: LexborHTMLParser) -> SellerClass:
    name = _text(
        tree.css_first("#merchantInfoFeature_feature_div .offer-display-feature-text-message")
    )
    if name is None:
        return SellerClass.UNKNOWN
    return SellerClass.FIRST_PARTY if name == "Amazon.com" else SellerClass.MARKETPLACE


def _in_stock(tree: LexborHTMLParser, price: Money | None) -> bool:
    if price is None:
        return False
    availability = (_text(tree.css_first("#availability span")) or "").lower()
    return not any(word in availability for word in _UNAVAILABLE)


def _observed_asin(tree: LexborHTMLParser, asin: str) -> str:
    node = tree.css_first("input#ASIN")
    value = node.attributes.get("value") if node is not None else None
    return value if value and ASIN_RE.fullmatch(value) else asin


def _offer(
    asin: str,
    title: str,
    price: Money | None,
    list_price: Money | None,
    in_stock: bool,
    seller: SellerClass,
    observed_at: datetime,
    extraction: ExtractionPath = ExtractionPath.HTML,
) -> Offer:
    return Offer(
        ref=ListingRef(CODE, asin),
        parent_external_id=None,
        title=title,
        price=price,
        list_price=list_price,
        in_stock=in_stock,
        seller_class=seller,
        unit_key=pack_unit_key(title) or "un|1",
        extraction=extraction,
        observed_at=observed_at,
        url_path=f"/dp/{asin}",
    )


def parse_amazon_detail(body: bytes, asin: str, observed_at: datetime) -> Offer | None:
    """Ficha: solo el buybox. Sin título es `ParseError` (página que no es una ficha)."""
    tree = _tree(body)
    title = _text(tree.css_first("#productTitle"))
    if title is None:
        raise ParseError("no_title", loc="productTitle")
    price = _price(_text(tree.css_first("#corePrice_feature_div .a-price .a-offscreen")))
    list_price = _price(
        _text(
            tree.css_first("#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen")
            or tree.css_first("#corePrice_feature_div .basisPrice .a-offscreen")
        )
    )
    return _offer(
        _observed_asin(tree, asin),
        title,
        price,
        list_price,
        _in_stock(tree, price),
        _seller(tree),
        observed_at,
    )


def parse_amazon_independent(body: bytes, asin: str, observed_at: datetime) -> Offer | None:
    """Ruta independiente: `input#twister-plus-price-data-price`; `None` si no existe."""
    tree = _tree(body)
    node = tree.css_first("input#twister-plus-price-data-price")
    raw = node.attributes.get("value") if node is not None else None
    if not raw:
        return None
    price = _price(raw)
    title = _text(tree.css_first("#productTitle")) or ""
    return _offer(
        _observed_asin(tree, asin),
        title,
        price,
        None,
        _in_stock(tree, price),
        _seller(tree),
        observed_at,
        ExtractionPath.EMBEDDED_JSON,
    )


def _is_sponsored(node: LexborNode) -> bool:
    classes = node.attributes.get("class") or ""
    return "AdHolder" in classes or node.css_first(".puis-sponsored-label-text") is not None


def parse_amazon_search(body: bytes, observed_at: datetime) -> list[Offer]:
    """Primera página de `/s`: sin patrocinados ni duplicados.

    El vendedor no aparece en la tarjeta: se marca `f` provisional y la ficha decide, porque
    todo veredicto de búsqueda exige leerla (confirmación con «mismo vendedor», spec §3.6).
    """
    out: list[Offer] = []
    seen: set[str] = set()
    for node in _tree(body).css('div[data-component-type="s-search-result"]'):
        asin = node.attributes.get("data-asin") or ""
        if not ASIN_RE.fullmatch(asin) or asin in seen or _is_sponsored(node):
            continue
        title = _text(node.css_first("h2"))
        if title is None:
            continue
        seen.add(asin)
        price = _price(_text(node.css_first(".a-price:not(.a-text-price) .a-offscreen")))
        list_price = _price(_text(node.css_first(".a-price.a-text-price .a-offscreen")))
        out.append(
            _offer(
                asin,
                title,
                price,
                list_price,
                price is not None,
                SellerClass.FIRST_PARTY,
                observed_at,
            )
        )
    return out


class AmazonSource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.USD,
            hosts=frozenset({"www.amazon.com", "amazon.com", *_SHORT_HOSTS}),
            client="curl",
            capabilities=frozenset(
                {Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM, Capability.RESOLVE}
            ),
            max_batch=1,
            external_id_re=ASIN_RE,
        )
        # asin → cuerpo de la última ficha leída en este proceso: la ruta independiente
        # vive en la misma página y no merece otra petición.
        self._bodies: dict[str, bytes] = {}

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        if url.host in _SHORT_HOSTS:
            if not _SHORT_PATH.fullmatch(url.path):
                return None
            return NeedsResolution(CODE, f"short:{_SHORT_HOSTS[url.host]}{url.path}")
        m = _PATH_ASIN.search(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        """Sigue a mano hasta 3 redirecciones, validando cada `Location` contra la lista."""
        alias, _, path = pending.pending_ref.removeprefix("short:").partition("/")
        host = _SHORT_ALIAS.get(alias)
        if host is None:
            raise ParseError("bad_short_ref")
        url = f"https://{host}/{path}"
        for _ in range(MAX_HOPS):
            resp = ctx.http.request("GET", url, headers=_HEADERS, cookies=COOKIES)
            location = {k.lower(): v for k, v in resp.headers.items()}.get("location")
            if resp.status not in (301, 302, 303, 307, 308) or not location:
                break
            safe = SafeUrl.parse(location, self.descriptor.hosts)
            ref = self.parse_url(safe)
            if isinstance(ref, ListingRef):
                return ref
            url = str(safe)
        raise NotFound(CODE, "short_not_resolved")

    def _get(self, ctx: SourceContext, asin: str) -> bytes:
        resp = ctx.http.request("GET", f"{BASE}/dp/{asin}", headers=_HEADERS, cookies=COOKIES)
        self._bodies[asin] = resp.body
        return resp.body

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for ref in refs:
            try:
                offer = parse_amazon_detail(
                    self._get(ctx, ref.external_id), ref.external_id, ctx.now
                )
            except (SourceError, ParseError) as exc:
                out.append(ListingFailed(ref, exc))
                continue
            out.append(ListingFound(offer) if offer else ListingFailed(ref, ParseError("empty")))
        return out

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        del page  # solo la primera página
        resp = ctx.http.request(
            "GET", f"{BASE}/s", params={"k": spec.query}, headers=_HEADERS, cookies=COOKIES
        )
        return SearchPage(parse_amazon_search(resp.body, ctx.now), None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        asin = offer.ref.external_id
        body = self._bodies.get(asin) or self._get(ctx, asin)
        return parse_amazon_independent(body, asin, ctx.now)

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        common = detect_block_common(resp)
        if common is not None:
            return common
        head = resp.body[:65_536]
        if any(marker in head for marker in _AKAMAI_MARKERS):
            return BlockKind.AKAMAI  # intersticial de Akamai Bot Manager
        if "/errors/validateCaptcha" in urlsplit(resp.url).path:
            return BlockKind.CAPTCHA
        return None


register(AmazonSource())
