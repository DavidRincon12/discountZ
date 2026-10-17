"""Ofertas de Amazon de DealNews contra su RSS real (fuente «sin verificar»)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from xml.sax.saxutils import escape, unescape

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.infra.http import FakeHttp
from discountz.sources.base import ChannelPage, NoCredentials, SourceContext
from discountz.sources.dealnews import FEED_URL, DealNewsSource, parse_dealnews_feed
from discountz.sources.fixtures import load_fixture
from discountz.sources.registry import load_all

NOW = datetime(2026, 10, 17, 18, 16, tzinfo=UTC)
FIRST_LINK = (
    "https://www.dealnews.com/3-in-1-Ice-Depuffing-Wand-for-45-free-shipping/22269285.html?iref=rss"
)
FIRST_IMAGE = (
    "https://d.dlnws.com/64599/1791568631-draftworks_img_g4809ur3glqb7flbfao.jpeg?h=250&w=250"
)
PRODUCT_LINK = (
    "https://www.dealnews.com/products/Life-Zoom/80-LED-3-in-1-Red-and-Infrared-Light-Therapy-Panel/"
    "545745.html?iref=rss"
)


def ctx(http: object) -> SourceContext:
    return SourceContext(http=http, credentials=NoCredentials(), deadline_monotonic=1e9, now=NOW)  # type: ignore[arg-type]


def feed(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
        'xmlns:dealnews="https://www.dealnews.com/ns/rss/1.0.htm" '
        'xmlns:media="http://search.yahoo.com/mrss/"><channel><title>DealNews</title>'
        f"{''.join(items)}</channel></rss>"
    ).encode()


def item(
    deal_id: int = 22291098,
    *,
    title: str = "Lenovo 530 Wireless Mouse for $10 + free shipping w/ Prime",
    price: str | None = "9.99",
    currency: str = "USD",
    deal_type: str = "deal",
    description: str = "<p>That's the lowest price we could find. Buy Now at Amazon</p>",
    pub: str = "Sat, 17 Oct 2026 07:40:12 -0400",
    expires: str = "2027-01-15T06:40:12-05:00",
    image: str = "https://d.dlnws.com/1/mouse.jpeg?h=250&w=250",
    link: str | None = None,
    guid: str | None = None,
    category: str = "Mice/Trackballs",
) -> str:
    url = link or f"https://www.dealnews.com/Lenovo-530-Wireless-Mouse/{deal_id}.html?iref=rss"
    guid = f"https://www.dealnews.com/{deal_id}.html?iref=rss" if guid is None else guid
    price_tag = (
        "" if price is None else f'<dealnews:price currency="{currency}">{price}</dealnews:price>'
    )
    return (
        f"<item><title>{escape(title)}</title><link>{escape(url)}</link>"
        f"<guid>{escape(guid)}</guid>"
        f"<description><![CDATA[{description}]]></description><pubDate>{pub}</pubDate>"
        f"<dealnews:retailer>Amazon</dealnews:retailer><dealnews:expires>{expires}"
        f"</dealnews:expires><dealnews:dealType>{deal_type}</dealnews:dealType>"
        f"<dealnews:category>{escape(category)}</dealnews:category>{price_tag}"
        f'<media:content url="{escape(image)}" medium="image"/></item>'
    )


def test_real_feed_reads_every_priced_deal() -> None:
    page = parse_dealnews_feed(load_fixture("dealnews", "amazon").body, NOW)
    assert len(page.posts) == 39
    assert (page.skipped, page.unparsed, page.has_more, page.max_id) == (4, 0, False, 22291221)
    assert sum(p.coupon is not None for p in page.posts) == 18
    assert {p.store for p in page.posts} == {"Amazon"}
    assert {p.channel for p in page.posts} == {"dealnews"}


def test_real_feed_first_deal_keeps_the_dealnews_link_untouched() -> None:
    first, second = parse_dealnews_feed(load_fixture("dealnews", "amazon").body, NOW).posts[:2]
    assert first.post_id == 22269285
    assert first.title == "3-in-1 Ice Depuffing Wand for $45 + free shipping"
    assert first.price == Money(4499, Currency.USD)
    assert first.coupon == "4IQ9VRP5"
    assert first.hashtags == ("skin", "care")
    assert first.published_at == datetime(2026, 10, 17, 18, 7, 12, tzinfo=UTC)
    assert str(first.link) == FIRST_LINK  # sus términos: el enlace y el `iref` sin tocar
    assert first.post_url == first.link
    assert str(first.image_url) == FIRST_IMAGE
    assert (second.post_id, second.price, second.coupon) == (
        22289099,
        Money(8750, Currency.USD),
        "H6Q4W7NJ",
    )


def test_product_deal_takes_the_deal_id_from_its_guid() -> None:
    posts = parse_dealnews_feed(load_fixture("dealnews", "amazon").body, NOW).posts
    (panel,) = [p for p in posts if str(p.link) == PRODUCT_LINK]
    assert panel.post_id == 22288673  # su `<link>` lleva el id del producto (545745)
    assert panel.price == Money(15215, Currency.USD)


def test_two_deals_on_the_same_product_keep_their_own_ids() -> None:
    link = "https://www.dealnews.com/products/Life-Zoom/Panel/545745.html?iref=rss"
    page = parse_dealnews_feed(feed(item(22288673, link=link), item(22299999, link=link)), NOW)
    assert [p.post_id for p in page.posts] == [22288673, 22299999]


@pytest.mark.parametrize(
    ("description", "coupon"),
    [
        ('<p>Apply coupon code "4IQ9VRP5" for a savings of $15.</p>', "4IQ9VRP5"),
        ("<p>It drops to $88 when you apply coupon code “4NVYZJPN” at checkout.</p>", "4NVYZJPN"),
        ('<p>You can use the promo code "DNSGENIE3" to cut it to $854.</p>', "DNSGENIE3"),
        ("<p>Clip the 50% off coupon and check out with Subscribe &amp; Save.</p>", None),
    ],
)
def test_coupon_comes_from_the_description(description: str, coupon: str | None) -> None:
    (post,) = parse_dealnews_feed(feed(item(description=description)), NOW).posts
    assert post.coupon == coupon


def test_sale_without_price_is_skipped_quietly() -> None:
    page = parse_dealnews_feed(feed(item(price=None, deal_type="sale")), NOW)
    assert (page.posts, page.skipped, page.unparsed, page.max_id) == ((), 1, 0, 22291098)


def test_deal_without_price_is_a_format_signal() -> None:
    page = parse_dealnews_feed(feed(*(item(i, price=None) for i in range(1, 6))), NOW)
    assert (page.posts, page.skipped, page.unparsed) == ((), 5, 5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expires": "2026-10-17T00:00:00-04:00"},  # vencida
        {"currency": "EUR"},
        {"price": "0.00"},
        {"pub": ""},
        {"title": ""},
        {"link": "https://www.dealnews.com.evil.example/x/22291098.html"},
        {"guid": "https://www.dealnews.com/sin-id.html"},
        {"guid": ""},
    ],
)
def test_items_that_are_not_usable_deals_are_skipped(kwargs: dict[str, str]) -> None:
    page = parse_dealnews_feed(feed(item(**kwargs)), NOW)  # type: ignore[arg-type]
    assert (page.posts, page.skipped) == ((), 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"guid": ""},
        {"guid": "https://www.dealnews.com/sin-id.html"},
        {"link": "https://www.dealnews.com.evil.example/x/22291098.html"},
        {"pub": "2026-10-17T07:40:12-04:00"},  # ISO 8601 en vez de RFC 822
        {"title": ""},
    ],
)
def test_unreadable_items_are_a_format_signal(kwargs: dict[str, str]) -> None:
    page = parse_dealnews_feed(feed(item(**kwargs)), NOW)  # type: ignore[arg-type]
    assert (page.posts, page.unparsed) == ((), 1)


@pytest.mark.parametrize(
    "link",
    [
        "https://www.dealnews.com/Mouse/22291098.html?iref=rss&utm_source=rss&ref=x",
        "https://www.dealnews.com/Mouse/22291098.html?iref=rss#oferta",
        "https://www.dealnews.com/Mouse/22291098.html?iref=rss&q=a%20b",
    ],
)
def test_link_that_validation_would_change_is_not_used(link: str) -> None:
    page = parse_dealnews_feed(feed(item(link=link)), NOW)
    assert (page.posts, page.unparsed) == ((), 1)  # sus términos: no cambiar sus enlaces


def test_real_feed_links_are_kept_byte_for_byte() -> None:
    body = load_fixture("dealnews", "amazon").body
    raw = re.findall(r"<link>(https://www\.dealnews\.com/[^<]+)</link>", body.decode())
    links = {str(p.link) for p in parse_dealnews_feed(body, NOW).posts}
    assert len(links) == 39 and links <= {unescape(r) for r in raw}


def test_expired_deal_is_skipped_quietly() -> None:
    page = parse_dealnews_feed(feed(item(expires="2026-10-17T00:00:00-04:00")), NOW)
    assert (page.posts, page.skipped, page.unparsed) == ((), 1, 0)


def test_feed_without_items_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_dealnews_feed(feed(), NOW)


def test_image_only_from_the_dealnews_cdn() -> None:
    (post,) = parse_dealnews_feed(feed(item(image="https://evil.example/x.jpg")), NOW).posts
    assert post.image_url is None


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
        b"<rss><channel><item><title>&b;</title></item></channel></rss>",
        b"<html>hola",
        b'<?xml version="1.0"?><html><body>Access denied</body></html>',
    ],
)
def test_entities_dtd_and_non_rss_are_parse_errors(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_dealnews_feed(body, NOW)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300), st.text(max_size=300))
def test_parser_never_crashes_on_arbitrary_text(title: str, text: str) -> None:
    try:
        page = parse_dealnews_feed(feed(item(title=title, description=escape(text))), NOW)
    except ParseError:
        return
    assert isinstance(page, ChannelPage)


def test_fetch_posts_reads_the_amazon_feed_without_cursor() -> None:
    fake = FakeHttp({FEED_URL: load_fixture("dealnews", "amazon")})
    source = DealNewsSource()
    assert len(source.fetch_posts(ctx(fake), None).posts) == 39
    source.fetch_posts(ctx(fake), 22291221)
    assert [c[1] for c in fake.calls] == [FEED_URL, FEED_URL]
    assert FEED_URL == "https://www.dealnews.com/s313/Amazon/?rss=1&sort=time"


def test_registered_as_unordered_channel() -> None:
    d = load_all()["dealnews"].descriptor
    assert (d.kind, d.ordered_ids, d.client) == ("channel", False, "httpx")
    assert d.currency is Currency.USD
    assert d.hosts == frozenset({"www.dealnews.com"})
    assert d.image_hosts == frozenset({"d.dlnws.com"})
    assert d.capabilities == frozenset()
