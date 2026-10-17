"""Ofertas de canales de Telegram: duplicados entre canales y tema (spec de canales §3-§4).

Puro: el adaptador entrega `ChannelPost` y estas funciones deciden sin I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
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
    channel: str  # código de la fuente (`tg_…`)
    post_id: int
    published_at: datetime
    title: str
    price: Money
    coupon: str | None
    store: str | None
    hashtags: tuple[str, ...]  # normalizados, sin «#»
    image_url: SafeUrl | None
    link: SafeUrl


@dataclass(frozen=True, slots=True)
class ChannelRule:
    topic: str
    words: frozenset[str]  # palabras normalizadas


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


def assign_channel_topic(post: ChannelPost, rules: Sequence[ChannelRule]) -> str:
    """Tema con más palabras en hashtags y título; a igual número, la regla anterior (§4.2)."""
    words = set(post.hashtags) | set(normalize_title(post.title).split())
    best, best_score = DEFAULT_TOPIC, 0
    for rule in rules:
        score = len(rule.words & words)
        if score > best_score:
            best, best_score = rule.topic, score
    return best
