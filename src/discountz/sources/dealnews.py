"""Ofertas de Amazon publicadas por DealNews, vía el RSS de su página de la tienda.

`/s313/Amazon/?rss=1&sort=time` trae las ~40 ofertas más recientes de Amazon con el precio en
`dealnews:price`. Sus ids no crecen con la publicación (DealNews vuelve a subir ofertas viejas),
así que el paso de canales lee el feed entero sin cursor por id (`ordered_ids=False`). Las
ofertas llegan «sin verificar», como las de los canales de Telegram. Los términos de DealNews
piden no modificar sus enlaces ni sus códigos de referido y atribuirles el contenido: el enlace
`?iref=rss` se guarda tal cual y la tarjeta dice «Visto en DealNews».
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
from discountz.domain.model import normalize_title
from discountz.domain.money import Currency, Money, parse_price
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import ChannelPage, PostsOnlySource, SourceContext, SourceDescriptor
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import register

CODE = "dealnews"
FEED_URL = "https://www.dealnews.com/s313/Amazon/?rss=1&sort=time"
HOSTS = frozenset({"www.dealnews.com"})
IMAGE_HOSTS = frozenset({"d.dlnws.com"})
TITLE_MAX = 200
_NS = "{https://www.dealnews.com/ns/rss/1.0.htm}"
_MEDIA = "{http://search.yahoo.com/mrss/}content"
_ID_RE = re.compile(r"/(\d{1,12})\.html$")
_TAG_RE = re.compile(r"<[^>]+>")
_COUPON_RE = re.compile(
    r"(?:coupon|promo)\s+code\s*[\"“”']([A-Za-z0-9]{3,30})[\"“”']", re.IGNORECASE
)


def _text(node: Element | None) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


def _safe(raw: str, hosts: frozenset[str]) -> SafeUrl | None:
    try:
        return SafeUrl.parse(raw.strip(), hosts)
    except UnsafeUrl:
        return None


def _published(item: Element) -> datetime | None:
    try:
        at = parsedate_to_datetime(_text(item.find("pubDate")))
    except (TypeError, ValueError):
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def _expired(item: Element, now: datetime) -> bool:
    try:
        until = datetime.fromisoformat(_text(item.find(f"{_NS}expires")))
    except ValueError:
        return False  # sin fecha legible no se descarta
    return until.tzinfo is not None and until <= now


def _price(item: Element) -> Money | None:
    node = item.find(f"{_NS}price")
    if node is None or (node.get("currency") or "USD") != "USD":
        return None
    try:
        return Money(parse_price(_text(node), Currency.USD), Currency.USD)
    except ParseError:
        return None


def _coupon(description: str) -> str | None:
    m = _COUPON_RE.search(html.unescape(_TAG_RE.sub(" ", description)))
    return m.group(1) if m else None


def _hashtags(item: Element) -> tuple[str, ...]:
    """Palabras de la categoría de DealNews («Skin Care» → `skin`, `care`) para el tema."""
    return tuple(dict.fromkeys(normalize_title(_text(item.find(f"{_NS}category"))).split()))


def _image(item: Element) -> SafeUrl | None:
    media = item.find(_MEDIA)
    return _safe(media.get("url") or "", IMAGE_HOSTS) if media is not None else None


def parse_dealnews_feed(body: bytes, now: datetime) -> ChannelPage:
    """Ofertas en USD y sin vencer del feed; lanza `ParseError` si no es un RSS legible.

    Una oferta sin id, enlace, título o fecha legibles, o sin precio legible sin ser una venta
    general («sale»), cuenta en `unparsed`: si son muchas y no hay ninguna oferta, el formato
    del feed cambió. Un feed sin ofertas también es un error: el de Amazon nunca está vacío.
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
        # El id de la oferta va en el `<guid>`: en las de tipo «product» el `<link>` abre la
        # página del producto y lleva su id, que se repite entre ofertas del mismo producto.
        link = _safe(_text(item.find("link")), HOSTS)
        guid = _safe(_text(item.find("guid")), HOSTS)
        m = _ID_RE.search(guid.path) if guid is not None else None
        title = _text(item.find("title"))[:TITLE_MAX]
        published = _published(item)
        if link is None or m is None or not title or published is None:
            unparsed += 1
            continue
        ids.append(int(m.group(1)))
        if _expired(item, now):
            continue
        price = _price(item)
        if price is None:
            if _text(item.find(f"{_NS}dealType")) != "sale":
                unparsed += 1
            continue
        posts.append(
            ChannelPost(
                channel=CODE,
                post_id=ids[-1],
                published_at=published,
                title=title,
                price=price,
                coupon=_coupon(item.findtext("description") or ""),
                store=_text(item.find(f"{_NS}retailer")) or None,
                hashtags=_hashtags(item),
                image_url=_image(item),
                link=link,
                post_url=link,
            )
        )
    return ChannelPage(
        posts=tuple(posts),
        max_id=max(ids, default=None),
        has_more=False,
        skipped=len(items) - len(posts),
        unparsed=unparsed,
    )


class DealNewsSource(PostsOnlySource):
    """Feed de ofertas de Amazon de DealNews (`ChannelSource` sin orden de ids)."""

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
            image_hosts=IMAGE_HOSTS,
            ordered_ids=False,
        )

    def fetch_posts(self, ctx: SourceContext, after_id: int | None) -> ChannelPage:
        resp = ctx.http.request("GET", FEED_URL, headers={"Accept": "application/rss+xml"})
        return parse_dealnews_feed(resp.body, ctx.now)

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


register(DealNewsSource())
