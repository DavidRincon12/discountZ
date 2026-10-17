"""Ofertas de canales: duplicados y tema (spec de canales §4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from discountz.domain.community import (
    ChannelPost,
    ChannelRule,
    assign_channel_topic,
    find_duplicate,
    title_tokens,
)
from discountz.domain.model import normalize_title
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl

T = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
USD_30 = Money(3000, Currency.USD)
RULES = (
    ChannelRule("tecnologia", frozenset({"teclado", "reloj", "inteligente", "ps5", "tv"})),
    ChannelRule("hogar", frozenset({"cocina", "cuchillos"})),
    ChannelRule("moda", frozenset({"reloj", "calzado"})),
    ChannelRule("belleza", frozenset({"cuidadofacial", "perfume"})),
)


@dataclass(frozen=True)
class Recent:
    key: str
    title_norm: str
    price_minor: int
    currency: str
    coupon: str | None


def _post(
    title: str,
    price: Money = USD_30,
    *,
    coupon: str | None = None,
    hashtags: tuple[str, ...] = (),
) -> ChannelPost:
    return ChannelPost(
        channel="tg_cajitatech",
        post_id=88168,
        published_at=T,
        title=title,
        price=price,
        coupon=coupon,
        store=None,
        hashtags=hashtags,
        image_url=None,
        link=SafeUrl.parse("https://t.me/cajitatech/88168", frozenset({"t.me"})),
    )


def _recent(
    title: str, price: int = 3000, currency: str = "USD", coupon: str | None = None
) -> Recent:
    return Recent("tg_clubgratis:59510", normalize_title(title), price, currency, coupon)


def test_title_tokens_drop_short_words() -> None:
    assert title_tokens("Armatus para PS 5 de acción") == frozenset({"armatus", "para", "accion"})


def test_same_coupon_is_duplicate_ignoring_case() -> None:
    post = _post("TV OLED LG 55 pulgadas", coupon="TVZE10")
    recent = [_recent("Televisor diferente", price=999_999, coupon="tvze10")]
    assert find_duplicate(post, recent) == "tg_clubgratis:59510"


def test_similar_title_and_price_is_duplicate() -> None:
    recent = [_recent("Armatus - PlayStation 5", price=3150)]
    # Jaccard 2/6 < 0,5: títulos demasiado distintos, aunque el precio coincida.
    assert (
        find_duplicate(_post("Armatus para PlayStation 5 juego de acción roguelike"), recent)
        is None
    )
    near = _post("Armatus PlayStation 5 juego")
    assert find_duplicate(near, recent) == "tg_clubgratis:59510"
    far = _post("Armatus PlayStation 5 juego", Money(4081, Currency.USD))
    assert find_duplicate(far, recent) is None


def test_other_currency_is_not_duplicate() -> None:
    recent = [_recent("Armatus PlayStation 5 juego", price=3000, currency="COP")]
    assert find_duplicate(_post("Armatus PlayStation 5 juego"), recent) is None


def test_topic_by_hashtag_and_title() -> None:
    assert assign_channel_topic(_post("Crema", hashtags=("cuidadofacial",)), RULES) == "belleza"
    assert assign_channel_topic(_post("Teclado mecánico retro"), RULES) == "tecnologia"
    assert assign_channel_topic(_post("Set de 7 cuchillos de cocina"), RULES) == "hogar"


def test_topic_more_matching_words_wins() -> None:
    assert assign_channel_topic(_post("Reloj inteligente"), RULES) == "tecnologia"
    assert (
        assign_channel_topic(_post("Reloj de pulsera"), RULES) == "tecnologia"
    )  # empate: el primero


def test_topic_defaults_to_otros() -> None:
    assert assign_channel_topic(_post("Producto genérico xyz"), RULES) == "otros"
