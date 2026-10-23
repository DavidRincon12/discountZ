"""Ofertas de Woot por su API oficial (`developer.woot.com`, ADR 0015).

Cada lectura pide los feeds de Computers, Electronics y Tools con la clave en `x-api-key`
(1 petición por segundo y 1.000 al día). Sus ofertas no se numeran en orden: el paso de canales
relee el feed entero sin cursor por id (`ordered_ids=False`) y salta lo ya guardado. Llegan
«sin verificar» y por casillero. El precio de lista no se usa: viene de Amazon y puede estar
inflado. Solo pasan las nuevas, como en eBay.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from discountz.domain.community import ChannelPost
from discountz.domain.errors import CredentialUnavailable, ParseError, UnsafeUrl
from discountz.domain.model import normalize_title
from discountz.domain.money import Currency, Money, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import ChannelPage, PostsOnlySource, SourceContext, SourceDescriptor
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import register

CODE = "woot"
API = "https://developer.woot.com/feed/"
FEEDS = ("Computers", "Electronics", "Tools")  # los temas en tarjeta del perfil (ADR 0013)
STORE = "Woot"
TITLE_MAX = 200
HOSTS = frozenset(
    {"woot.com", "www.woot.com"}
    | {
        f"{sub}.woot.com"
        for sub in (
            "computers",
            "electronics",
            "tools",
            "home",
            "sport",
            "shirt",
            "wine",
            "gourmet",
            "kids",
            "sellout",
            "accessories",
            "deals",
        )
    }
)
IMAGE_HOSTS = frozenset({"d3gqasl9vmjfd8.cloudfront.net"})
_NEW = "new"


def offer_post_id(raw: object) -> int | None:
    """Los primeros 63 bits del `OfferId` (UUID): caben en el `bigint` de `channel_post`."""
    if not isinstance(raw, str):
        return None
    try:
        value = uuid.UUID(raw)
    except ValueError:
        return None
    post_id = int.from_bytes(value.bytes[:8]) >> 1
    return post_id or None


def _text(raw: object) -> str:
    return " ".join(raw.split()) if isinstance(raw, str) else ""


def _safe(raw: object, hosts: frozenset[str]) -> SafeUrl | None:
    if not isinstance(raw, str):
        return None
    try:
        return SafeUrl.parse(raw.strip(), hosts)
    except UnsafeUrl:
        return None


def _when(raw: object) -> datetime | None:
    try:
        at = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return at.astimezone(UTC) if at is not None and at.tzinfo is not None else None


def _price(raw: object) -> Money | None:
    """`SalePrice.Minimum` (con `parse_float=Decimal`): con rango, el precio «desde»."""
    low = raw.get("Minimum") if isinstance(raw, Mapping) else None
    if isinstance(low, bool) or not isinstance(low, Decimal | int):
        return None
    try:
        minor = to_minor(Decimal(low), Currency.USD)
    except InvalidOperation:
        return None
    return Money(minor, Currency.USD) if minor > 0 else None


def _hashtags(raw: object) -> tuple[str, ...]:
    """Palabras de sus categorías («COMPUTERS/Laptops» → `computers`, `laptops`) para el tema."""
    paths = raw if isinstance(raw, list) else []
    words = (w for p in paths if isinstance(p, str) for w in normalize_title(p).split())
    return tuple(dict.fromkeys(words))


def _unavailable(item: Mapping[str, Any], now: datetime) -> bool:
    """Agotada, solo en la app, vencida o no nueva: no es oferta, pero tampoco formato roto."""
    if item.get("IsSoldOut") is True or item.get("IsAvailableOnMobileAppOnly") is True:
        return True
    until = _when(item.get("EndDate"))
    if until is not None and until <= now:
        return True
    condition = item.get("Condition")
    return condition is not None and _text(condition).casefold() != _NEW


def _items(body: bytes) -> list[Any]:
    try:
        data = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ParseError("bad_json", loc=CODE) from exc
    items = data.get("Items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ParseError("not_feed", loc=CODE)
    return items


def _post(item: Mapping[str, Any], post_id: int) -> ChannelPost | None:
    title = _text(item.get("Title"))[:TITLE_MAX]
    link = _safe(item.get("Url"), HOSTS)
    published = _when(item.get("StartDate"))
    price = _price(item.get("SalePrice"))
    if not title or link is None or published is None or price is None:
        return None
    return ChannelPost(
        channel=CODE,
        post_id=post_id,
        published_at=published,
        title=title,
        price=price,
        coupon=None,
        store=STORE,
        hashtags=_hashtags(item.get("Categories")),
        image_url=_safe(item.get("Photo"), IMAGE_HOSTS),
        link=link,
        post_url=link,
        casillero=True,
    )


def parse_woot_feeds(bodies: Sequence[bytes], now: datetime) -> ChannelPage:
    """Ofertas nuevas y a la venta de los feeds; lanza `ParseError` si uno no es un `FeedPage`.

    Una oferta en dos feeds cuenta una vez. Sin id, título, enlace de Woot, fecha con zona o
    precio legibles cuenta en `unparsed`. Un feed vacío no es error: una categoría puede
    quedarse sin ofertas.
    """
    posts: list[ChannelPost] = []
    seen: set[int] = set()
    skipped = unparsed = 0
    for body in bodies:
        for item in _items(body):
            post_id = offer_post_id(item.get("OfferId")) if isinstance(item, dict) else None
            if post_id is None:
                unparsed += 1
                continue
            if post_id in seen:
                continue
            seen.add(post_id)
            if _unavailable(item, now):
                skipped += 1
                continue
            post = _post(item, post_id)
            if post is None:
                unparsed += 1
                continue
            posts.append(post)
    return ChannelPage(
        posts=tuple(posts),
        max_id=max(seen, default=None),
        has_more=False,
        skipped=skipped + unparsed,
        unparsed=unparsed,
    )


class WootSource(PostsOnlySource):
    """Feeds de Woot por su API (`ChannelSource` sin orden de ids)."""

    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="channel",
            currency=Currency.USD,
            hosts=HOSTS,
            client="httpx",
            capabilities=frozenset(),
            max_batch=1,
            external_id_re=re.compile(r"^\d{1,19}$"),
            auth="api_key",
            image_hosts=IMAGE_HOSTS,
            ordered_ids=False,
            # Un feed de categoría suma ofertas que empezaron antes: sin el tope de 6 h.
            refeatures=True,
        )

    def fetch_posts(self, ctx: SourceContext, after_id: int | None) -> ChannelPage:
        key = ctx.credentials.bearer(CODE).get_secret_value()  # sin clave, ni se pregunta
        headers = {"Accept": "application/json", "x-api-key": key}
        bodies = [ctx.http.request("GET", API + feed, headers=headers).body for feed in FEEDS]
        return parse_woot_feeds(bodies, ctx.now)

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        """Un 403 de la API es la clave mala, no un bloqueo anti-bots."""
        if resp.status == 403:
            raise CredentialUnavailable(CODE, "bad_key", 403)
        return detect_block_common(resp)


register(WootSource())
