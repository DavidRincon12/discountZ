"""Ofertas de canales de Telegram: duplicados entre canales y tema (spec de canales §3-§4).

Puro: el adaptador entrega `ChannelPost` y estas funciones deciden sin I/O.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from types import MappingProxyType
from typing import Protocol

from discountz.domain.model import normalize_title
from discountz.domain.money import Money
from discountz.domain.topics import DEFAULT_TOPIC
from discountz.domain.urls import SafeUrl

DUPLICATE_PRICE_TOL_PCT = 10
DUPLICATE_MIN_JACCARD = 0.5
_MIN_TOKEN_LEN = 3


@dataclass(frozen=True, slots=True)
class ChannelPost:
    channel: str  # código de la fuente (`tg_…`, `dealnews`…)
    post_id: int
    published_at: datetime
    title: str
    price: Money
    coupon: str | None
    store: str | None
    hashtags: tuple[str, ...]  # normalizados, sin «#»
    image_url: SafeUrl | None
    link: SafeUrl
    post_url: SafeUrl | None = None  # la publicación en su fuente; None: `t.me/<canal>/<id>`
    # Se compra por casillero: la fuente es de EE. UU. o la publicación lo dice. Lo de Amazon
    # en los canales de Telegram suele enviar directo a Colombia (David, 2026-10-22).
    casillero: bool = False


# Pesos del tema (spec de canales §4.2): cada hashtag (lo escoge quien publica) pesa como el
# producto del título, que es lo que va antes de «para», «con», «for»…; lo de después es el uso.
HASHTAG_WEIGHT = 2
HEAD_WEIGHT = 2
TAIL_WEIGHT = 1
_CONNECTORS = frozenset({"para", "con", "sin", "for", "with", "w", "without", "compatible"})
_MIN_AFFIX = 4  # un hashtag compuesto se lee por prefijo o sufijo de al menos 4 letras


@dataclass(frozen=True, slots=True)
class ChannelRule:
    topic: str
    words: frozenset[str]  # términos normalizados: palabras o frases («reloj inteligente»)


class RecentLike(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def title_norm(self) -> str: ...
    @property
    def price_minor(self) -> int: ...
    @property
    def currency(self) -> str: ...
    @property
    def coupon(self) -> str | None: ...


def title_tokens(title: str) -> frozenset[str]:
    """Palabras del título normalizado con 3 o más caracteres."""
    return frozenset(t for t in normalize_title(title).split() if len(t) >= _MIN_TOKEN_LEN)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def find_duplicate(post: ChannelPost, recent: Sequence[RecentLike]) -> str | None:
    """Clave de la primera publicación reciente que es la misma oferta (§4.1), o `None`."""
    coupon = post.coupon.casefold() if post.coupon else None
    tokens = title_tokens(post.title)
    for r in recent:
        # Un cupón puede valer para muchos productos: solo cuenta con la misma moneda y precio.
        if r.currency != post.price.currency.value:
            continue
        diff = abs(post.price.minor - r.price_minor) * 100
        if diff > DUPLICATE_PRICE_TOL_PCT * r.price_minor:
            continue
        if coupon and r.coupon and r.coupon.casefold() == coupon:
            return r.key
        if _jaccard(tokens, title_tokens(r.title_norm)) >= DUPLICATE_MIN_JACCARD:
            return r.key
    return None


def stem(token: str) -> str:
    """Singular aproximado en español e inglés (`zapatos` → `zapato`, `relojes` → `reloj`)."""
    if len(token) <= 3 or not token.endswith("s") or token.endswith("ss"):
        return token
    if token.endswith("ces"):
        return token[:-3] + "z"
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and token[-3] in "lrndjsxz":
        return token[:-2]
    return token[:-1]


def _stem_phrase(term: str) -> str:
    return " ".join(stem(t) for t in term.split())


@dataclass(frozen=True, slots=True)
class _Index:
    terms: Mapping[str, str]  # término con cada palabra en singular → tema
    affixes: Mapping[str, str]  # término sin espacios (tal cual y en singular) → tema
    max_words: int
    order: Mapping[str, int]  # tema → posición de su primera regla (desempate)


@lru_cache(maxsize=4)
def _index(rules: tuple[ChannelRule, ...]) -> _Index:
    terms: dict[str, str] = {}
    affixes: dict[str, str] = {}
    order: dict[str, int] = {}
    for pos, rule in enumerate(rules):
        order.setdefault(rule.topic, pos)
        for term in sorted(rule.words):
            terms.setdefault(_stem_phrase(term), rule.topic)
            affixes.setdefault(term.replace(" ", ""), rule.topic)
            affixes.setdefault(_stem_phrase(term).replace(" ", ""), rule.topic)
    longest = max((len(t.split()) for t in terms), default=1)
    return _Index(MappingProxyType(terms), MappingProxyType(affixes), longest, order)


def _hashtag_topic(tag: str, index: _Index) -> str | None:
    """Tema del hashtag entero o, si es compuesto, de su prefijo más largo (luego sufijo)."""
    exact = index.terms.get(stem(tag))
    if exact is not None:
        return exact
    for size in range(len(tag), _MIN_AFFIX - 1, -1):
        if (topic := index.affixes.get(tag[:size])) is not None:
            return topic
    for size in range(len(tag) - 1, _MIN_AFFIX - 1, -1):
        if (topic := index.affixes.get(tag[-size:])) is not None:
            return topic
    return None


def _title_scores(title: str, index: _Index, scores: Counter[str]) -> None:
    """Suma cada término del título; la frase más larga gana sobre sus palabras sueltas."""
    tokens = [stem(t) for t in normalize_title(title).split()]
    weight, i = HEAD_WEIGHT, 0
    while i < len(tokens):
        for size in range(min(index.max_words, len(tokens) - i), 0, -1):
            topic = index.terms.get(" ".join(tokens[i : i + size]))
            if topic is not None:
                scores[topic] += weight
                i += size
                break
        else:
            if tokens[i] in _CONNECTORS:  # «para perros» puede ser una frase del vocabulario
                weight = TAIL_WEIGHT
            i += 1


def _best(scores: Counter[str], index: _Index) -> str:
    if not scores:
        return DEFAULT_TOPIC
    return min(scores, key=lambda t: (-scores[t], index.order[t]))


def assign_channel_topic(post: ChannelPost, rules: Sequence[ChannelRule]) -> str:
    """Tema con más peso entre hashtags y título; a igual peso, la regla anterior (§4.2)."""
    index = _index(tuple(rules))
    scores: Counter[str] = Counter()
    for tag in post.hashtags:
        if (topic := _hashtag_topic(tag, index)) is not None:
            scores[topic] += HASHTAG_WEIGHT
    _title_scores(post.title, index, scores)
    return _best(scores, index)


def topic_for_title(title: str, rules: Sequence[ChannelRule]) -> str:
    """El tema que tendría una oferta de canal con ese título y sin hashtags (`/interes`)."""
    index = _index(tuple(rules))
    scores: Counter[str] = Counter()
    _title_scores(title, index, scores)
    return _best(scores, index)
