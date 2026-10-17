"""Feed comunitario de Slickdeals (spec §4.3 «Slickdeals»): RSS de portada → `Lead`.

La URL de tienda se obtiene SIN red, en este orden:
1. un `href` directo a una tienda admitida;
2. el parámetro `u2=` de un enlace de redirección, URL-decodificado;
3. el atributo `data-aps-asin` cuando el enlace sale a amazon.com.

El RSS real del 2026-10-13 ya no trae `u2=` (sus redirecciones usan `u3=` cifrado), así que
hoy solo Amazon sale por la vía 3; los demás leads se ignoran hasta que haya una vía sin red.

El mismo RSS se lee además como canal (`slickdeals_amazon`): sus ofertas a Amazon llegan como
tarjetas «sin verificar», con el ASIN de la vía 3 y el hilo de Slickdeals.
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

from discountz.domain.community import ChannelPost
from discountz.domain.errors import ParseError, UnsafeUrl
from discountz.domain.model import Lead, ListingRef, Offer, SearchSpec
from discountz.domain.money import Currency, Money, parse_price
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    ChannelPage,
    ListingOutcome,
    NeedsResolution,
    PostsOnlySource,
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
# Canal «sin verificar» con las ofertas de la portada que salen a Amazon.
AMAZON_CODE = "slickdeals_amazon"
AMAZON_HOSTS = frozenset({"www.amazon.com", "amazon.com"})
IMAGE_HOSTS = frozenset({"static.slickdealscdn.com"})
THREAD_HOSTS = frozenset({"slickdeals.net"})
TITLE_MAX = 200
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_THREAD_RE = re.compile(r"thread-(\d{1,12})")
_COUPON_RE = re.compile(r"[A-Za-z0-9]{3,30}")
_AMAZON_EXIT_RE = re.compile(r"data-product-exitwebsite\s*=\s*[\"']amazon\.com[\"']", re.IGNORECASE)


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


def _safe(raw: str, hosts: frozenset[str] = STORE_HOSTS) -> SafeUrl | None:
    try:
        return SafeUrl.parse(raw, hosts)
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


def _rss_items(body: bytes) -> list[Element]:
    try:
        root = fromstring(body, forbid_dtd=True)
    except (XmlParseError, DefusedXmlException) as exc:
        raise ParseError("bad_xml", loc="rss") from exc
    if root.tag != "rss":
        raise ParseError("not_rss", loc="rss")
    return list(root.iter("item"))


def parse_slickdeals_rss(body: bytes, now: datetime) -> list[Lead]:
    """Leads de menos de 24 h con URL de tienda admitida; el resto se ignora."""
    leads: list[Lead] = []
    for item in _rss_items(body):
        published = _published(item)
        if published is None or not now - MAX_AGE < published <= now + timedelta(hours=1):
            continue
        title = _text(item.find("title"))
        target = _target((item.findtext(_CONTENT) or "") + (item.findtext("description") or ""))
        if not title or target is None:
            continue
        leads.append(Lead(CODE, title, target, _price(title), published))
    return leads


def _extras(content: str) -> tuple[str | None, SafeUrl | None]:
    """Cupón y foto del cuerpo de la oferta; la foto solo del CDN de Slickdeals."""
    tree = LexborHTMLParser(content)
    code = tree.css_first("[data-role=couponCode] .code")
    coupon = code.text(strip=True) if code is not None else ""
    img = tree.css_first("img[src]")
    image = _safe(img.attributes.get("src") or "", IMAGE_HOSTS) if img is not None else None
    return (coupon if _COUPON_RE.fullmatch(coupon) else None), image


def parse_slickdeals_amazon(body: bytes) -> ChannelPage:
    """Ofertas de la portada que salen a Amazon, como publicaciones «sin verificar».

    Una oferta que sale a Amazon sin ASIN, título o precio legibles cuenta en `unparsed`: si
    son muchas y no hay ninguna oferta, cambió el formato. La edad la decide el paso de canales.
    """
    items = _rss_items(body)
    posts: list[ChannelPost] = []
    ids: list[int] = []
    unparsed = 0
    for item in items:
        m = _THREAD_RE.fullmatch(_text(item.find("guid")))
        published = _published(item)
        if m is None or published is None:
            continue
        ids.append(int(m.group(1)))
        content = item.findtext(_CONTENT) or ""
        target = _target(content + (item.findtext("description") or ""))
        if target is None or target.host not in AMAZON_HOSTS:
            if _AMAZON_EXIT_RE.search(content):
                unparsed += 1
            continue
        title = _text(item.find("title"))[:TITLE_MAX]
        price = _price(title)
        thread = _safe(_text(item.find("link")), THREAD_HOSTS)
        if not title or price is None or thread is None:
            unparsed += 1
            continue
        coupon, image = _extras(content)
        posts.append(
            ChannelPost(
                channel=AMAZON_CODE,
                post_id=ids[-1],
                published_at=published,
                title=title,
                price=price,
                coupon=coupon,
                store="Amazon",
                hashtags=(),
                image_url=image,
                link=target,
                post_url=thread,
            )
        )
    return ChannelPage(
        posts=tuple(posts),
        max_id=max(ids, default=None),
        has_more=False,
        skipped=len(items) - len(posts),
        unparsed=unparsed,
    )


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


class SlickdealsAmazonSource(PostsOnlySource):
    """El mismo RSS de portada, leído como canal: sus ofertas a Amazon salen sin verificar.

    Un lead a Amazon solo da alerta si se puede leer la ficha de Amazon; este canal entrega
    esas ofertas aunque no se pueda. No tiene hosts: los enlaces a slickdeals.net siguen
    siendo de `slickdeals`. Los ids de hilo no siguen el orden de la portada.
    """

    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=AMAZON_CODE,
            kind="channel",
            currency=Currency.USD,
            hosts=frozenset(),
            client="httpx",
            capabilities=frozenset(),
            max_batch=1,
            external_id_re=re.compile(r"^\d{1,12}$"),
            image_hosts=IMAGE_HOSTS,
            ordered_ids=False,
        )

    def fetch_posts(self, ctx: SourceContext, after_id: int | None) -> ChannelPage:
        resp = ctx.http.request("GET", RSS_URL, headers={"Accept": "application/rss+xml"})
        return parse_slickdeals_amazon(resp.body)

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


register(SlickdealsSource())
register(SlickdealsAmazonSource())
