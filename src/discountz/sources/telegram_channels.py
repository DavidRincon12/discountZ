"""Canales públicos de ofertas de Telegram vía `t.me/s/<canal>` (spec de canales §2-§3).

Se lee la vista web pública, sin cuenta. Cada canal tiene su formato y su parser; lo común
(publicaciones, hora, foto, texto y enlaces) se saca una vez. El precio es el que publica el
canal: la oferta llega «sin verificar».
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from selectolax.lexbor import LexborHTMLParser, LexborNode

from discountz.domain.community import ChannelPost
from discountz.domain.errors import ParseError, UnsafeUrl
from discountz.domain.model import normalize_title
from discountz.domain.money import Currency, Money, parse_price
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import ChannelPage, PostsOnlySource, SourceContext, SourceDescriptor
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import SOURCES, register

CHANNELS: Mapping[str, str] = MappingProxyType(
    {
        "tg_descuentostech": "DescuentosTech",
        "tg_clubgratis": "Clubgratis",
        "tg_cajitatech": "cajitatech",
        "tg_descuentoshome": "DescuentosHome",
        "tg_ofertu": "ofertu",
    }
)
# Canales cuyos posts sin código son catálogo y no oferta: se leen pero no se envían.
REQUIRES_COUPON = frozenset({"tg_ofertu"})
PAGE_SIZE = 20  # publicaciones por página de `t.me/s`
TITLE_MAX = 200
# Con al menos tantas publicaciones con precio y ninguna oferta, el formato cambió (§3).
FORMAT_BROKEN_MIN = 5
IMAGE_HOSTS = frozenset(f"cdn{i}.telesco.pe" for i in range(1, 6))
# Destinos de «Ver oferta»; además valen los hosts de las fuentes registradas. Un enlace a otro
# host se reemplaza por la publicación en Telegram. El programa nunca descarga estos enlaces.
LINK_HOSTS = frozenset(
    {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "pccajita.link",
        "joylink.io",
        "amzn.to",
        "temu.to",
        "www.mercadolibre.com.co",
        "articulo.mercadolibre.com.co",
        "www.samsung.com",
        "www.apple.com",
        "www.ebay.com",
    }
)


def channel_link_hosts() -> frozenset[str]:
    """Hosts que valen para «Ver oferta»: `LINK_HOSTS` y los de las fuentes registradas."""
    return LINK_HOSTS | {h for s in SOURCES.values() for h in s.descriptor.hosts}


_NO_MESSAGES = "tme_no_messages_found"
_HISTORY = "tgme_channel_history"
_BG_URL_RE = re.compile(r"background-image:url\('([^']+)'\)")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_HASHTAG_RE = re.compile(r"#(\w+)")
_PRICE_HINT_RE = re.compile(r"\$\s*\d|💰|USD\s*\$?\s*\d")
_LEADING_JUNK_RE = re.compile(r"^[^\w\"'¡¿]+")
# «*Casillero» (DescuentosTech), «COMPRA CON CASILLERO 🇺🇸» (Clubgratis)
_CASILLERO_RE = re.compile(r"\bcasilleros?\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Raw:
    """Lo común de una publicación, antes de las reglas del canal."""

    post_id: int
    published_at: datetime | None
    text: str
    image_url: SafeUrl | None


@dataclass(frozen=True, slots=True)
class _Deal:
    title: str
    price: Money
    coupon: str | None
    store: str | None
    link: str | None


def _safe(raw: str | None, hosts: frozenset[str]) -> SafeUrl | None:
    if not raw:
        return None
    try:
        return SafeUrl.parse(raw.strip(), hosts)
    except UnsafeUrl:
        return None


def _money(raw: str, currency: Currency) -> Money | None:
    try:
        return Money(parse_price(raw, currency), currency)
    except ParseError:
        return None


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _clean_title(line: str) -> str:
    title = _LEADING_JUNK_RE.sub("", " ".join(line.split()))
    return title[:TITLE_MAX]


def _first_line(paragraph: str) -> str:
    return paragraph.split("\n", 1)[0]


# ───────────── Reglas por canal (§3)

_DT_PRICE_RE = re.compile(r"USD\s*\$\s*([\d.,]+)")
_DT_COUPON_RE = re.compile(r"Código:\s*([A-Za-z0-9]{3,})")
_DT_LINK_RE = re.compile(r"Ver oferta:\s*(https?://\S+)")


def _descuentostech(text: str, hashtags: Sequence[str]) -> _Deal | None:
    paragraphs = _paragraphs(text)
    for i, para in enumerate(paragraphs):
        m = _DT_PRICE_RE.search(para)
        if m is None:
            continue
        price = _money(m.group(1), Currency.USD)
        rest = paragraphs[i + 1 :]
        if price is None or not rest:
            return None
        coupon = _DT_COUPON_RE.search(para)
        link = _DT_LINK_RE.search(text)
        return _Deal(
            title=_clean_title(_first_line(rest[0])),
            price=price,
            coupon=coupon.group(1) if coupon else None,
            store="eBay" if "ebay" in hashtags else None,
            link=link.group(1) if link else None,
        )
    return None


_CAJ_RE = re.compile(r"💰\s*\$\s*([\d.,]+)\s*(USD|COP)\s*-\s*(.+?)\s*🛍")
_CAJ_COUPON_RE = re.compile(r"Aplicar\s+([A-Za-z0-9]{3,})\s+para obtener")
_CAJ_LINK_RE = re.compile(r"Oferta\s*➡️\s*(https?://\S+)")


def _cajitatech(text: str, hashtags: Sequence[str]) -> _Deal | None:
    m = _CAJ_RE.search(text)
    if m is None:
        return None
    price = _money(m.group(1), Currency(m.group(2)))
    if price is None:
        return None
    coupon = _CAJ_COUPON_RE.search(text)
    link = _CAJ_LINK_RE.search(text)
    return _Deal(
        title=_clean_title(m.group(3)),
        price=price,
        coupon=coupon.group(1) if coupon else None,
        store=None,
        link=link.group(1) if link else None,
    )


_CG_PRICE_RE = re.compile(r"(?:💰|➡️)\s*(?:💰\s*)?(\$?\s*[\d][\d.,]*)(?![\d.,]*\s*%)")
_CG_BARE_PRICE_RE = re.compile(r"^\$\s*[\d][\d.,]*$")
_CG_COUPON_RE = re.compile(r"(?:CODE:\s*|cup[oó]n\s+)([A-Za-z0-9]{3,})", re.IGNORECASE)
_CG_COUPON_ONLY_RE = re.compile(
    r"\W*(?:aplicar\s+)?(?:CODE:\s*|cup[oó]n\s+)[A-Za-z0-9]{3,}\W*", re.IGNORECASE
)
_CG_STORE_RE = re.compile(r"Comprar en\s+([^\n]+?)\s*(https?://\S+)")
# Líneas que no son el producto: avisos entre el precio y el título, y el enlace de compra.
_CG_NOTE_RE = re.compile(
    r"(?:aplicar\s+cup[oó]n|precio\s+incluye\s+env[ií]o|env[ií]o\s+(?:directo|gratis)|comprar\s+en)\b",
    re.IGNORECASE,
)
_COP_GROUPS_RE = re.compile(r"\d{1,3}(?:\.\d{3})+")


def _clubgratis_money(raw: str) -> Money | None:
    """Puntos de miles ⇒ COP; entero, decimales de 1-2 dígitos o coma de miles ⇒ USD."""
    number = raw.replace("$", "").strip().rstrip(".,")
    currency = Currency.COP if _COP_GROUPS_RE.fullmatch(number) else Currency.USD
    return _money(number, currency)


def _is_clubgratis_title(line: str) -> bool:
    text = _HASHTAG_RE.sub("", line).strip()
    return bool(text) and not (_CG_COUPON_ONLY_RE.fullmatch(line) or _CG_NOTE_RE.match(text))


def _clubgratis(text: str, hashtags: Sequence[str]) -> _Deal | None:
    paragraphs = _paragraphs(text)
    for i, para in enumerate(paragraphs):
        m = _CG_PRICE_RE.search(para) or _CG_BARE_PRICE_RE.fullmatch(para)
        if m is None:
            continue
        price = _clubgratis_money(m.group(1) if m.re is _CG_PRICE_RE else m.group(0))
        if price is None:
            return None
        # El título puede ir en la misma línea que sigue al precio o en un párrafo posterior.
        tail = para[m.end() :].split("\n", 1)[1:] if m.re is _CG_PRICE_RE else []
        lines = [line for p in [*tail, *paragraphs[i + 1 :]] for line in p.split("\n")]
        rest = [line for line in lines if _is_clubgratis_title(line)]
        if not rest:
            return None
        coupon = _CG_COUPON_RE.search(text)
        store = _CG_STORE_RE.search(text)
        return _Deal(
            title=_clean_title(rest[0]),
            price=price,
            coupon=coupon.group(1) if coupon else None,
            store=store.group(1).strip().title() if store else None,
            link=store.group(2) if store else None,
        )
    return None


_DH_PRICE_RE = re.compile(r"Por\s*Solo\s*✨\s*\$\s*([\d.,]+)", re.IGNORECASE)
# «Aplicando Cupón: AUUCCF58 50%» trae código; «Aplicando Cupón 10%», no.
_DH_COUPON_RE = re.compile(r"Cup[oó]n:\s*([A-Za-z0-9]{4,})\b(?!\s*%)")
_DH_LINK_RE = re.compile(r"Comprar:\s*(https?://\S+)")


def _descuentoshome(text: str, hashtags: Sequence[str]) -> _Deal | None:
    paragraphs = _paragraphs(text)
    for i, para in enumerate(paragraphs):
        m = _DH_PRICE_RE.search(para)
        if m is None:
            continue
        price = _money(m.group(1), Currency.USD)
        rest = paragraphs[i + 1 :]
        if price is None or not rest:
            return None
        coupon = _DH_COUPON_RE.search(para)
        link = _DH_LINK_RE.search(text)
        return _Deal(
            title=_clean_title(_first_line(rest[0])),
            price=price,
            coupon=coupon.group(1) if coupon else None,
            store=None,
            link=link.group(1) if link else None,
        )
    return None


_OF_PRICE_RE = re.compile(r"💲\s*\$\s*([\d.]+)")
_OF_COUPON_RE = re.compile(r"Código:\s*([A-Za-z0-9]{4,})")
_OF_LINK_RE = re.compile(r"🔗\s*(https?://\S+)")


def _ofertu(text: str, hashtags: Sequence[str]) -> _Deal | None:
    """Título en la primera línea y precio en pesos con punto de miles («💲 $ 38.152»)."""
    m = _OF_PRICE_RE.search(text)
    if m is None:
        return None
    price = _money(m.group(1).rstrip("."), Currency.COP)
    if price is None:
        return None
    coupon = _OF_COUPON_RE.search(text)
    link = _OF_LINK_RE.search(text)
    return _Deal(
        title=_clean_title(_first_line(text.strip())),
        price=price,
        coupon=coupon.group(1) if coupon else None,
        store=None,
        link=link.group(1) if link else None,
    )


_PARSERS: Mapping[str, Callable[[str, Sequence[str]], _Deal | None]] = MappingProxyType(
    {
        "tg_descuentostech": _descuentostech,
        "tg_cajitatech": _cajitatech,
        "tg_clubgratis": _clubgratis,
        "tg_descuentoshome": _descuentoshome,
        "tg_ofertu": _ofertu,
    }
)


# ───────────── Lo común de la página


def _text(node: LexborNode | None) -> str:
    if node is None:
        return ""
    return html.unescape(_TAG_RE.sub("", _BR_RE.sub("\n", node.html or "")))


def _published(node: LexborNode) -> datetime | None:
    tag = node.css_first("time[datetime]")
    raw = tag.attributes.get("datetime") if tag is not None else None
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def _image(node: LexborNode) -> SafeUrl | None:
    wrap = node.css_first("a.tgme_widget_message_photo_wrap")
    style = wrap.attributes.get("style") if wrap is not None else None
    m = _BG_URL_RE.search(style or "")
    return _safe(m.group(1), IMAGE_HOSTS) if m else None


def _raw_posts(channel: str, body: bytes) -> list[_Raw]:
    tree = LexborHTMLParser(body)
    posts: list[_Raw] = []
    prefix = f"{channel.lower()}/"
    for node in tree.css("div.tgme_widget_message[data-post]"):
        ref = (node.attributes.get("data-post") or "").lower()
        if not ref.startswith(prefix) or not ref[len(prefix) :].isdigit():
            continue
        posts.append(
            _Raw(
                post_id=int(ref[len(prefix) :]),
                published_at=_published(node),
                text=_text(node.css_first(".tgme_widget_message_text")),
                image_url=_image(node),
            )
        )
    return posts


def _hashtags(text: str) -> tuple[str, ...]:
    tags = ("".join(normalize_title(t).split()) for t in _HASHTAG_RE.findall(text))
    return tuple(dict.fromkeys(t for t in tags if t and t != "ad"))


def parse_channel_page(code: str, body: bytes, now: datetime) -> ChannelPage:
    """Ofertas de una página de `t.me/s/<canal>`; lanza `ParseError` si el formato cambió."""
    channel = CHANNELS[code]
    raws = _raw_posts(channel, body)
    head = body.decode("utf-8", errors="replace")
    if not raws:
        if _NO_MESSAGES in head or (_HISTORY in head and "tgme_widget_message" not in head):
            return ChannelPage((), None, False, 0)
        raise ParseError("format", loc=code)
    link_hosts = channel_link_hosts()
    post_url_hosts = frozenset({"t.me"})
    deals: list[ChannelPost] = []
    priced = parsed = 0
    for raw in sorted(raws, key=lambda r: r.post_id):
        hashtags = _hashtags(raw.text)
        if _PRICE_HINT_RE.search(raw.text):
            priced += 1
        deal = _PARSERS[code](raw.text, hashtags) if raw.published_at else None
        if deal is None or not deal.title or raw.published_at is None:
            continue
        parsed += 1
        if code in REQUIRES_COUPON and deal.coupon is None:
            continue
        fallback = SafeUrl.parse(f"https://t.me/{channel}/{raw.post_id}", post_url_hosts)
        deals.append(
            ChannelPost(
                channel=code,
                post_id=raw.post_id,
                published_at=raw.published_at,
                title=deal.title,
                price=deal.price,
                coupon=deal.coupon,
                store=deal.store,
                hashtags=hashtags,
                image_url=raw.image_url,
                link=_safe(deal.link, link_hosts) or fallback,
                casillero=_CASILLERO_RE.search(raw.text) is not None,
            )
        )
    # Un post filtrado por `REQUIRES_COUPON` sí se entendió: no cuenta como formato roto.
    if not parsed and priced >= FORMAT_BROKEN_MIN:
        raise ParseError("format", loc=code)
    return ChannelPage(
        posts=tuple(deals),
        max_id=max(r.post_id for r in raws),
        has_more=len(raws) >= PAGE_SIZE,
        skipped=len(raws) - len(deals),
        unparsed=0 if parsed else priced,
    )


class TelegramChannelSource(PostsOnlySource):
    """Un canal público. Solo implementa la lectura de publicaciones (`ChannelSource`)."""

    def __init__(self, code: str) -> None:
        self.channel = CHANNELS[code]
        self.descriptor = SourceDescriptor(
            code=code,
            kind="channel",
            currency=Currency.USD,
            hosts=frozenset({"t.me"}),
            client="httpx",
            capabilities=frozenset(),
            max_batch=1,
            external_id_re=re.compile(r"^\d{1,12}$"),
            image_hosts=IMAGE_HOSTS,
        )

    def fetch_posts(self, ctx: SourceContext, after_id: int | None) -> ChannelPage:
        params = {"after": str(after_id)} if after_id is not None else None
        resp = ctx.http.request("GET", f"https://t.me/s/{self.channel}", params=params)
        return parse_channel_page(self.descriptor.code, resp.body, ctx.now)

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


for _code in CHANNELS:
    register(TelegramChannelSource(_code))
