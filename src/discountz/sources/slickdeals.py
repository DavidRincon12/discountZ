"""Feed comunitario de Slickdeals (spec §4.3 «Slickdeals»): RSS de portada → `Lead`.

La URL de tienda se obtiene SIN red, en este orden:
1. un `href` directo a una tienda admitida;
2. el parámetro `u2=` de un enlace de redirección, URL-decodificado;
3. el atributo `data-aps-asin` cuando el enlace sale a amazon.com.

El RSS real del 2026-10-13 ya no trae `u2=` (sus redirecciones usan `u3=` cifrado), así que
hoy solo Amazon sale por la vía 3; los demás leads se ignoran hasta que haya una vía sin red.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit
from xml.etree.ElementTree import Element  # solo el tipo: el parseo es con defusedxml

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as XmlParseError
from defusedxml.ElementTree import fromstring
from selectolax.lexbor import LexborHTMLParser

from discountz.domain.errors import ParseError, UnsafeUrl
from discountz.domain.model import Lead, ListingRef, Offer, SearchSpec
from discountz.domain.money import Currency, Money, parse_price
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    ListingOutcome,
    NeedsResolution,
    SearchPage,
    SourceContext,
    SourceDescriptor,
)
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import register

CODE = "slickdeals"
RSS_URL = (
    "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"
)
MAX_AGE = timedelta(hours=24)
# Tiendas de destino posibles (la app decide después cuáles están activas).
STORE_HOSTS = frozenset(
    {
        "www.amazon.com",
        "amazon.com",
        "www.newegg.com",
        "newegg.com",
        "www.walmart.com",
        "walmart.com",
        "www.bestbuy.com",
        "bestbuy.com",
    }
)
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _text(node: Element | None) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


def _price(title: str) -> Money | None:
    """El último `$N` del título (los títulos terminan en el precio de la oferta)."""
    found = _PRICE_RE.findall(title)
    if not found:
        return None
    try:
        return Money(parse_price(found[-1], Currency.USD), Currency.USD)
    except ParseError:
        return None


def _safe(raw: str) -> SafeUrl | None:
    try:
        return SafeUrl.parse(raw, STORE_HOSTS)
    except UnsafeUrl:
        return None


def _target(content: str) -> SafeUrl | None:
    tree = LexborHTMLParser(content)
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        direct = _safe(href)
        if direct is not None:
            return direct
        redirect = dict(parse_qsl(urlsplit(href).query)).get("u2")
        if redirect and (via_u2 := _safe(redirect)) is not None:
            return via_u2
    for a in tree.css("a[data-aps-asin]"):
        asin = a.attributes.get("data-aps-asin") or ""
        exit_site = (a.attributes.get("data-product-exitwebsite") or "").lower()
        if exit_site == "amazon.com" and _ASIN_RE.fullmatch(asin):
            return SafeUrl.parse(f"https://www.amazon.com/dp/{asin}", STORE_HOSTS)
    return None


def _published(item: Element) -> datetime | None:
    raw = _text(item.find("pubDate"))
    try:
        at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def parse_slickdeals_rss(body: bytes, now: datetime) -> list[Lead]:
    """Leads de menos de 24 h con URL de tienda admitida; el resto se ignora."""
    try:
        root = fromstring(body, forbid_dtd=True)
    except (XmlParseError, DefusedXmlException) as exc:
        raise ParseError("bad_xml", loc="rss") from exc
    if root.tag != "rss":
        raise ParseError("not_rss", loc="rss")
    leads: list[Lead] = []
    for item in root.iter("item"):
        published = _published(item)
        if published is None or not now - MAX_AGE < published <= now + timedelta(hours=1):
            continue
        title = _text(item.find("title"))
        target = _target((item.findtext(_CONTENT) or "") + (item.findtext("description") or ""))
        if not title or target is None:
            continue
        leads.append(Lead(CODE, title, target, _price(title), published))
    return leads


class SlickdealsSource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="feed",
            currency=Currency.USD,
            hosts=frozenset({"slickdeals.net"}),
            client="httpx",
            capabilities=frozenset({Capability.LEADS}),
            max_batch=1,
            external_id_re=re.compile(r"^\d{1,12}$"),
        )

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        return None  # un hilo de Slickdeals no es un producto que seguir

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return []

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        return SearchPage((), None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        return None

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        resp = ctx.http.request("GET", RSS_URL, headers={"Accept": "application/rss+xml"})
        return parse_slickdeals_rss(resp.body, ctx.now)

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


register(SlickdealsSource())
