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
    ChannelRule("tecnologia", frozenset({"teclado", "reloj inteligente", "ps5", "tv", "laptop"})),
    ChannelRule("hogar", frozenset({"cocina", "cuchillo", "ropa de cama"})),
    ChannelRule("moda", frozenset({"reloj", "calzado", "zapato", "mochila", "ropa"})),
    ChannelRule("belleza", frozenset({"cuidadofacial", "perfume"})),
    ChannelRule("bebes", frozenset({"juguete"})),
)
PET_FIRST = (ChannelRule("mascotas", frozenset({"para perro"})), *RULES)


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
    recent = [_recent("Televisor diferente", price=3100, coupon="tvze10")]
    assert find_duplicate(post, recent) == "tg_clubgratis:59510"


def test_shared_coupon_on_other_product_is_not_duplicate() -> None:
    # cajitatech 88165 (proyector, USD 39) y 88180 (carro, USD 14) traen el mismo cupón.
    post = _post("Carro Rodante SUNALLY de 3 niveles", Money(1400, Currency.USD), coupon="VVC3X6BP")
    recent = [_recent("proyector mini 4k", price=3900, coupon="VVC3X6BP")]
    assert find_duplicate(post, recent) is None
    cop = [_recent("carro rodante sunally", price=1400, currency="COP", coupon="VVC3X6BP")]
    assert find_duplicate(post, cop) is None


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


def test_topic_phrase_beats_its_words() -> None:
    assert assign_channel_topic(_post("Reloj inteligente 1.57 con llamadas"), RULES) == "tecnologia"
    assert assign_channel_topic(_post("Reloj Casio vintage"), RULES) == "moda"


def test_topic_matches_plurals() -> None:
    assert assign_channel_topic(_post("Zapatos PUMA Seeker"), RULES) == "moda"
    assert assign_channel_topic(_post("Set de 7 cuchillos"), RULES) == "hogar"


def test_topic_hashtag_matches_by_longest_prefix() -> None:
    assert assign_channel_topic(_post("Nova 4", hashtags=("zapatoshombre",)), RULES) == "moda"
    assert assign_channel_topic(_post("Lacoste", hashtags=("ropadecama",)), RULES) == "hogar"


def test_topic_sums_hashtags_and_title() -> None:
    post = _post("Teclado de piano", hashtags=("calzado", "zapatoshombre"))
    assert assign_channel_topic(post, RULES) == "moda"  # 2 hashtags pesan más que el producto


def test_topic_words_after_connector_weigh_less() -> None:
    assert assign_channel_topic(_post("Mochila escolar para laptop"), RULES) == "moda"
    assert assign_channel_topic(_post("Laptop 15 con mochila"), RULES) == "tecnologia"


def test_topic_phrase_may_start_with_connector() -> None:
    post = _post("Juguete interactivo para perros")
    assert assign_channel_topic(post, RULES) == "bebes"
    assert assign_channel_topic(post, PET_FIRST) == "mascotas"  # empate: la regla anterior


def test_topic_defaults_to_otros() -> None:
    assert assign_channel_topic(_post("Producto genérico xyz"), RULES) == "otros"
