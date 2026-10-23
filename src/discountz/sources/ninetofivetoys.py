"""Ofertas de tecnología de EE. UU. publicadas por 9to5Toys, vía su RSS (ADR 0014).

`/feed/` trae los ~50 posts más recientes (algo más de un día). No hay un campo de precio: va en
el título, junto al ahorro («$50 off») y al precio de antes («Reg. $30»), así que
`price_from_title` lo separa y prefiere saltar el post a inventar un precio. Los ids (`?p=` del
`<guid>`) no siguen el orden de publicación, así que el paso de canales relee el feed entero
(`ordered_ids=False`). «Ver oferta» lleva al artículo: sus enlaces a tiendas son acortadores de
afiliado y el artículo le da el crédito a 9to5Toys. Todo se compra por casillero.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element  # solo el tipo: el parseo es con defusedxml

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as XmlParseError
from defusedxml.ElementTree import fromstring

from discountz.domain.community import ChannelPost
from discountz.domain.errors import ParseError, UnsafeUrl
from discountz.domain.money import Currency, Money, parse_price
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import ChannelPage, PostsOnlySource, SourceContext, SourceDescriptor
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import register

CODE = "ninetofivetoys"
FEED_URL = "https://9to5toys.com/feed/"
HOSTS = frozenset({"9to5toys.com"})
TITLE_MAX = 200
AMAZON_HOSTS = frozenset({"link.amazon", "amzn.to", "www.amazon.com", "amazon.com"})
# Enlaces a los sitios de 9to5 (reseñas, otros posts): no son la tienda.
_OWN_SITES = ("9to5toys.com", "9to5mac.com", "9to5google.com", "electrek.co")
_AMOUNT_RE = re.compile(r"\$(\d[\d,]*(?:\.\d{1,2})?)")
# Lo que sigue al monto o lo precede y lo vuelve ahorro o precio de antes, no el precio.
_SAVING_AFTER_RE = re.compile(
    r"^\s*(?:off|savings?|discount|less|credit|gift|rebate|under)\b", re.IGNORECASE
)
_BEFORE_PRICE_RE = re.compile(
    r"\b(?:reg\.?|regularly|was|save|saving|list|msrp|worth|value|up\s+to)\s*$", re.IGNORECASE
)
# Un monto después de «from», «starting (down) at», «under» o «as low as» es de un resumen.
_ROUNDUP_RE = re.compile(
    r"\b(?:from|starting(?:\s+(?:down\s+)?at)?|under|as\s+low\s+as)(?:\s+(?:just|only))?\s*$",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"^\d{1,12}$")
_IMG_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_HREF_RE = re.compile(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)
_HOST_RE = re.compile(r"^https?://([^/:?#]+)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_COUPON_RE = re.compile(r"\bcode\s+[\"“”']?([A-Z0-9]{4,20})\b")


def price_from_title(title: str) -> Money | None:
    """El precio del título, o `None` si solo trae ahorros, precios de antes o es un resumen."""
    candidates: list[str] = []
    for m in _AMOUNT_RE.finditer(title):
        before, after = title[: m.start()], title[m.end() :]
        if _SAVING_AFTER_RE.match(after) or _BEFORE_PRICE_RE.search(before):
            continue
        if _ROUNDUP_RE.search(before):
            return None
        candidates.append(m.group(1))
    if not candidates:
        return None
    try:
        return Money(parse_price(candidates[0], Currency.USD), Currency.USD)
    except ParseError:
        return None


def _text(node: Element | None) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


def _safe(raw: str) -> SafeUrl | None:
    try:
        return SafeUrl.parse(html.unescape(raw.strip()), HOSTS)
    except UnsafeUrl:
        return None


def _published(item: Element) -> datetime | None:
    try:
        at = parsedate_to_datetime(_text(item.find("pubDate")))
    except (TypeError, ValueError):
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def _post_id(item: Element) -> int | None:
    guid = _safe(_text(item.find("guid")))
    raw = dict(guid.query).get("p", "") if guid is not None else ""
    return int(raw) if _ID_RE.match(raw) else None


def _store(description: str) -> str | None:
    """«Amazon» si el primer enlace a una tienda (no a 9to5) es de Amazon."""
    for href in _HREF_RE.findall(description):
        m = _HOST_RE.match(html.unescape(href))
        host = m.group(1).lower() if m else ""
        if not host or host.endswith(_OWN_SITES):
            continue
        return "Amazon" if host in AMAZON_HOSTS else None
    return None


def _image(description: str) -> SafeUrl | None:
    m = _IMG_RE.search(description)
    return _safe(m.group(1)) if m else None


def _coupon(description: str) -> str | None:
    m = _COUPON_RE.search(html.unescape(_TAG_RE.sub(" ", description)))
    return m.group(1) if m else None


def parse_ninetofivetoys_feed(body: bytes) -> ChannelPage:
    """Posts con precio del feed; lanza `ParseError` si no es un RSS legible o no trae ítems.

    Un ítem sin id, enlace, título o fecha legibles cuenta en `unparsed` (formato roto). Uno sin
    precio en el título (noticias, resúmenes, solo el ahorro) se salta en silencio.
    """
    try:
        root = fromstring(body, forbid_dtd=True)
    except (XmlParseError, DefusedXmlException) as exc:
        raise ParseError("bad_xml", loc=CODE) from exc
    if root.tag != "rss":
        raise ParseError("not_rss", loc=CODE)
    items = list(root.iter("item"))
    if not items:
        raise ParseError("no_items", loc=CODE)
    posts: list[ChannelPost] = []
    ids: list[int] = []
    unparsed = 0
    for item in items:
        post_id = _post_id(item)
        link = _safe(_text(item.find("link")))
        title = _text(item.find("title"))[:TITLE_MAX]
        published = _published(item)
        if post_id is None or link is None or not title or published is None:
            unparsed += 1
            continue
        ids.append(post_id)
        price = price_from_title(title)
        if price is None:
            continue
        description = item.findtext("description") or ""
        posts.append(
            ChannelPost(
                channel=CODE,
                post_id=post_id,
                published_at=published,
                title=title,
                price=price,
                coupon=_coupon(description),
                store=_store(description),
                hashtags=(),  # su categoría solo dice Deals/News/Steals: el tema sale del título
                image_url=_image(description),
                link=link,
                post_url=link,
                casillero=True,
            )
        )
    return ChannelPage(
        posts=tuple(posts),
        max_id=max(ids, default=None),
        has_more=False,
        skipped=len(items) - len(posts),
        unparsed=unparsed,
    )


class NineToFiveToysSource(PostsOnlySource):
    """RSS de 9to5Toys (`ChannelSource` sin orden de ids)."""

    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="channel",
            currency=Currency.USD,
            hosts=HOSTS,
            client="httpx",
            capabilities=frozenset(),
            max_batch=1,
            external_id_re=re.compile(r"^\d{1,12}$"),
            image_hosts=HOSTS,
            ordered_ids=False,
        )

    def fetch_posts(self, ctx: SourceContext, after_id: int | None) -> ChannelPage:
        resp = ctx.http.request("GET", FEED_URL, headers={"Accept": "application/rss+xml"})
        return parse_ninetofivetoys_feed(resp.body)

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


register(NineToFiveToysSource())
