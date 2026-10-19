"""Canales de ofertas de Telegram contra páginas reales de `t.me/s` (spec de canales §3)."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.sources.base import ChannelPage, NoCredentials, SourceContext
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.registry import load_all
from discountz.sources.telegram_channels import (
    CHANNELS,
    TelegramChannelSource,
    parse_channel_page,
)

NOW = datetime(2026, 10, 17, 15, 40, tzinfo=UTC)
PHOTO = "https://cdn4.telesco.pe/file/abc.jpg"


def ctx(http: object) -> SourceContext:
    return SourceContext(http=http, credentials=NoCredentials(), deadline_monotonic=1e9, now=NOW)  # type: ignore[arg-type]


def post(
    channel: str,
    post_id: int,
    text: str,
    *,
    when: str | None = "2026-10-17T15:00:00+00:00",
    photo: str | None = PHOTO,
) -> str:
    body = html.escape(text).replace("\n", "<br/>")
    wrap = (
        f'<a class="tgme_widget_message_photo_wrap" href="https://t.me/{channel}/{post_id}" '
        f"style=\"width:720px;background-image:url('{photo}')\"></a>"
        if photo
        else ""
    )
    date = (
        f'<a class="tgme_widget_message_date" href="https://t.me/{channel}/{post_id}">'
        f'<time datetime="{when}" class="time">15:00</time></a>'
        if when
        else ""
    )
    return (
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" '
        f'data-post="{channel}/{post_id}">{wrap}'
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{body}</div>'
        f"{date}</div></div>"
    )


def page(*posts: str) -> bytes:
    return (
        '<html><body><section class="tgme_channel_history js-message_history">'
        + "".join(posts)
        + "</section></body></html>"
    ).encode()


CAJITA = (
    "#Cocina\n\n💰$12 USD - Set de 7 cuchillos de cocina 🛍\n\n"
    "Aplicar ABC123 para obtener el descuento\n\nOferta ➡️ {link}"
)


def caj(post_id: int = 88200, link: str = "https://pccajita.link/abc", **kw: Any) -> str:
    return post("cajitatech", post_id, CAJITA.format(link=link), **kw)


@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("tg_cajitatech", "cajitatech"),
        ("tg_descuentostech", "descuentostech"),
        ("tg_clubgratis", "clubgratis"),
        ("tg_descuentoshome", "descuentoshome"),
        ("tg_ofertu", "ofertu"),
    ],
)
def test_parse_matches_expected(code: str, name: str) -> None:
    exp = json.loads((FIXTURES_DIR / "telegram" / f"{name}.expected.json").read_text())
    result = parse_channel_page(code, load_fixture("telegram", name).body, NOW)
    got = [
        {
            "post_id": p.post_id,
            "price_minor": p.price.minor,
            "currency": p.price.currency.value,
            "coupon": p.coupon,
            "store": p.store,
            "title_start": p.title[: len(e["title_start"])],
        }
        for p, e in zip(result.posts, exp["deals"], strict=False)
    ]
    assert got == exp["deals"]
    assert len(result.posts) == len(exp["deals"])
    assert result.max_id == exp["max_id"]
    assert result.skipped == exp["skipped"]
    assert result.has_more is True
    assert all(p.channel == code for p in result.posts)
    assert all(p.image_url is not None for p in result.posts)
    assert all(len(p.title) <= 200 for p in result.posts)


def test_real_fixture_hashtags_and_links() -> None:
    result = parse_channel_page("tg_cajitatech", load_fixture("telegram", "cajitatech").body, NOW)
    first = result.posts[0]
    assert first.hashtags == ("accesoriostecnologia",)
    assert str(first.link) == "https://pccajita.link/q7oqj"
    assert first.published_at == datetime(2026, 10, 17, 5, 20, 19, tzinfo=UTC)
    dt = parse_channel_page(
        "tg_descuentostech", load_fixture("telegram", "descuentostech").body, NOW
    )
    assert dt.posts[0].link.host == "www.facebook.com"


def test_descuentostech_thousands_dot() -> None:
    text = "#Board ✨Por solo✨ USD$1.999✅\n\nPlaca de prueba\n\n👉 Ver oferta: https://www.facebook.com/x"
    result = parse_channel_page("tg_descuentostech", page(post("DescuentosTech", 1, text)), NOW)
    assert result.posts[0].price == Money(199_900, Currency.USD)


@pytest.mark.parametrize(
    ("line", "price", "coupon"),
    [
        ("➡️  💰 4.499.910", Money(4_499_910, Currency.COP), None),
        ("➡️ 💰 $19.783 (precio varía)", Money(19_783, Currency.COP), None),
        ("➡️  💰 275.000 aplicar cupón PREHOT", Money(275_000, Currency.COP), "PREHOT"),
        ("➡️  💰12.74", Money(1274, Currency.USD), None),
        ("➡️ 6.5", Money(650, Currency.USD), None),
        ("➡️  💰130  precio incluye envio.", Money(13_000, Currency.USD), None),
        ("$1,999.00", Money(199_900, Currency.USD), None),
        ("➡️  💰1299", Money(129_900, Currency.USD), None),
    ],
)
def test_clubgratis_currency_inference(line: str, price: Money, coupon: str | None) -> None:
    text = (
        f"{line}\n\nProducto de prueba\n#Tag\n\n Comprar en Amazon https://joylink.io/clubgratis/x"
    )
    result = parse_channel_page("tg_clubgratis", page(post("Clubgratis", 7, text)), NOW)
    assert [(p.price, p.coupon, p.title) for p in result.posts] == [
        (price, coupon, "Producto de prueba")
    ]


def test_descuentoshome_reads_coupon_only_when_it_has_a_code() -> None:
    text = (
        "#Hogar ✨Por Solo✨ $1.299✅ Envío gratis con Prime 🚚\n{cupon}\n\n"
        "Juego de sábanas King\n \n¿Viste una oferta? 👉 ¡Envíamela!📩\n\n"
        "🛒 Comprar: https://www.facebook.com/permalink.php?id=1&story_fbid=2"
    )
    posts = [
        post("DescuentosHome", 1, text.format(cupon="Aplicando Cupón: AUUCCF58 50% descuento🏷")),
        post("DescuentosHome", 2, text.format(cupon="Aplicando Cupón 10% descuento🏷")),
    ]
    result = parse_channel_page("tg_descuentoshome", page(*posts), NOW)
    assert [(p.price, p.coupon, p.title) for p in result.posts] == [
        (Money(129_900, Currency.USD), "AUUCCF58", "Juego de sábanas King"),
        (Money(129_900, Currency.USD), None, "Juego de sábanas King"),
    ]
    assert result.posts[0].link.host == "www.facebook.com"


def ofertu(post_id: int, code: str | None) -> str:
    coupon = f"🔻 Código: {code}🏷️\n" if code else ""
    text = (
        f"Luz nocturna de huevo\n💲 $ 38.152💰\n{coupon}"
        "🚚 ¡Elegible para envío gratis!🚀\n🔗 https://joylink.io/amazon/x"
    )
    return post("ofertu", post_id, text)


def test_ofertu_sends_only_posts_with_code() -> None:
    result = parse_channel_page("tg_ofertu", page(ofertu(1, "BOX4WZR6"), ofertu(2, None)), NOW)
    assert [(p.post_id, p.price, p.coupon, p.title) for p in result.posts] == [
        (1, Money(38_152, Currency.COP), "BOX4WZR6", "Luz nocturna de huevo")
    ]
    assert str(result.posts[0].link) == "https://joylink.io/amazon/x"
    assert result.skipped == 1


def test_ofertu_page_without_codes_is_not_a_format_change() -> None:
    result = parse_channel_page("tg_ofertu", page(*(ofertu(i, None) for i in range(1, 21))), NOW)
    assert result.posts == ()
    assert result.skipped == 20
    assert result.unparsed == 0


def test_non_deals_are_skipped() -> None:
    result = parse_channel_page(
        "tg_clubgratis",
        page(
            post("Clubgratis", 1, "Usuario reporta llegada:\nLos AirPods en 179 dolaritos"),
            post("Clubgratis", 2, "📦 El Madrugón: hasta 40% de descuento"),
            post(
                "Clubgratis", 3, "➡️  💰12.74\n\nProducto\n\n Comprar en Amazon https://joylink.io/x"
            ),
        ),
        NOW,
    )
    assert [p.post_id for p in result.posts] == [3]
    assert result.skipped == 2
    assert result.max_id == 3
    assert result.has_more is False


def test_image_only_from_telesco() -> None:
    result = parse_channel_page(
        "tg_cajitatech", page(caj(photo="//telegram.org/img/emoji/40/F09F92B0.png")), NOW
    )
    assert result.posts[0].image_url is None


@pytest.mark.parametrize(
    "link",
    [
        "http://amzn.to/x",
        "tg://resolve?domain=x",
        "javascript:alert(1)",
        "https://evil.example/x",
    ],
)
def test_link_outside_allowlist_falls_back_to_post(link: str) -> None:
    result = parse_channel_page("tg_cajitatech", page(caj(88200, link)), NOW)
    assert str(result.posts[0].link) == "https://t.me/cajitatech/88200"


def test_allowed_link_is_kept() -> None:
    result = parse_channel_page("tg_cajitatech", page(caj(88200, "https://pccajita.link/abc")), NOW)
    assert str(result.posts[0].link) == "https://pccajita.link/abc"


def test_post_without_time_or_price_is_skipped() -> None:
    no_price = post("cajitatech", 3, "#Despensa\n\nOlímpica - Leches en oferta 🛍")
    zero = post("cajitatech", 4, CAJITA.format(link="x").replace("$12", "$0"))
    result = parse_channel_page("tg_cajitatech", page(caj(2, when=None), no_price, zero), NOW)
    assert result.posts == ()
    assert result.skipped == 3


def test_empty_page_with_posts_markup_missing_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_channel_page("tg_cajitatech", b"<html><body>Telegram</body></html>", NOW)
    empty = parse_channel_page(
        "tg_cajitatech", load_fixture("telegram", "cajitatech-empty").body, NOW
    )
    assert empty == ChannelPage((), None, False, 0)


def test_page_with_prices_but_zero_deals_is_parse_error() -> None:
    posts = [post("cajitatech", i, f"Nuevo formato: cuesta ${i}.99 dólares") for i in range(1, 21)]
    with pytest.raises(ParseError):
        parse_channel_page("tg_cajitatech", page(*posts), NOW)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=400))
def test_parser_never_crashes_on_arbitrary_text(text: str) -> None:
    try:
        result = parse_channel_page("tg_clubgratis", page(post("Clubgratis", 9, text)), NOW)
    except ParseError:
        return
    assert isinstance(result, ChannelPage)


def test_fetch_posts_url() -> None:
    body = page(caj(88161))
    after = "https://t.me/s/cajitatech?after=88160"
    fake = FakeHttp(
        {
            after: HttpResponse(200, after, {}, body, 0),
            "https://t.me/s/cajitatech": HttpResponse(200, "x", {}, body, 0),
        }
    )
    source = TelegramChannelSource("tg_cajitatech")
    assert source.fetch_posts(ctx(fake), 88160).max_id == 88161
    source.fetch_posts(ctx(fake), None)
    assert [c[1] for c in fake.calls] == [after, "https://t.me/s/cajitatech"]


def test_registry_has_five_channels() -> None:
    channels = {c for c, s in load_all().items() if s.descriptor.kind == "channel"}
    feeds = {"dealnews", "slickdeals_amazon"}
    assert channels - feeds == set(CHANNELS)
    assert dict(CHANNELS) == {
        "tg_descuentostech": "DescuentosTech",
        "tg_clubgratis": "Clubgratis",
        "tg_cajitatech": "cajitatech",
        "tg_descuentoshome": "DescuentosHome",
        "tg_ofertu": "ofertu",
    }
    from discountz.app.render import source_name

    assert [source_name(c) for c in sorted(CHANNELS)] == [
        "cajitatech",
        "Clubgratis",
        "DescuentosHome",
        "DescuentosTech",
        "OFERTU",
    ]
