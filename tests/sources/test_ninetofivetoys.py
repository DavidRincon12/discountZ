"""Ofertas de 9to5Toys contra su RSS real (fuente «sin verificar», ADR 0014)."""

from __future__ import annotations

from datetime import UTC, datetime
from xml.sax.saxutils import escape

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.infra.http import FakeHttp
from discountz.sources.base import ChannelPage, NoCredentials, SourceContext
from discountz.sources.fixtures import load_fixture
from discountz.sources.ninetofivetoys import (
    FEED_URL,
    NineToFiveToysSource,
    parse_ninetofivetoys_feed,
    price_from_title,
)
from discountz.sources.registry import load_all

NOW = datetime(2026, 10, 23, 4, 20, tzinfo=UTC)
ANKER_LINK = "https://9to5toys.com/2026/10/22/anker-indoor-outdoor-c31-security-camera/"


def ctx(http: object) -> SourceContext:
    return SourceContext(http=http, credentials=NoCredentials(), deadline_monotonic=1e9, now=NOW)  # type: ignore[arg-type]


def feed(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>9to5Toys</title>'
        f"{''.join(items)}</channel></rss>"
    ).encode()


def item(
    post_id: str = "1238496",
    *,
    title: str = "LISEN’s 140W dual USB-C cable tops off two devices at once, now $16",
    link: str = "https://9to5toys.com/2026/10/22/lisen-140w-cable/",
    pub: str = "Thu, 22 Oct 2026 17:04:00 +0000",
    description: str = (
        '<div class="feat-image"><img src="https://9to5toys.com/wp-content/uploads/sites/5/'
        '2026/10/lisen.jpg?quality=82&#038;w=1600" /></div><p>Amazon now offers the '
        '<a href="https://link.amazon/B0abc">LISEN cable</a> for $16.</p>'
    ),
) -> str:
    return (
        f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
        f"<pubDate>{pub}</pubDate><category><![CDATA[Deals]]></category>"
        f'<guid isPermaLink="false">https://9to5toys.com/?p={post_id}</guid>'
        f"<description><![CDATA[{description}]]></description></item>"
    )


@pytest.mark.parametrize(
    ("title", "minor"),
    [
        ("LISEN’s 140W dual USB-C cable tops off two devices at once, now $16", 1600),
        ("Samsung’s 27-inch 5K Odyssey G8 drops to its best price yet at $600 (Save $350)", 60000),
        ("Upgrade your iPhone Duo setup with a fresh set of AirPods Pro 3 at $50 off", None),
        ("First discount hits Dyson’s new R1 Nurovi for $100 savings to $450", 45000),
        (
            "EcoFlow flash sale drops DELTA 3 Max 440W solar bundle to $1,379 ($220 off), more",
            137900,
        ),
        ("Anker’s security camera lands back at a $40 low, or get two for $76", 4000),
        ("Here are 20 Nike sale picks from $31: Shoes, sunglasses, more", None),
        ("These ocean-style Apple Watch bands are priced from $11.50 right now (Save 38%)", None),
        ("Higround keyboards are up to 60% off with deals from just $24 (Today only)", None),
        ("Here are 40+ new iPhone Duo cases on sale at Amazon starting down at $14", None),
        ("This iOS app is replacing scanners, and it’s on sale for $25 (Reg. up to $199)", 2500),
        ("LEGO’s popular Lucky Cat set is back down at an $18 low (Reg. $30)", 1800),
        ("Score MacBook Neo at $120 under the list price from the Apple refurb store", None),
        ("Score Google’s latest Pixel 11 Pro Fold at up to $419 off today", None),
        ("This set is now $5.99 shipped", 599),
        ("LEGO officially confirms partnership with Daft Punk on new upcoming Ideas set", None),
        ("Pay $0 for this app today", None),
        ("Galaxy Tab bundle with a free $100 Amazon gift card at $799", 79900),
        ("Get $30 back on AirPods Pro 3 at $199", 19900),
        ("Score $100 in savings on this Roku TV, now $299", 29900),
        ("Anker charger drops to $24, plus more", 2400),
        ("Mint Mobile service from scratch at $15/mo", None),
        ("Streaming plan at $1,2 for today", None),
    ],
)
def test_price_from_title(title: str, minor: int | None) -> None:
    expected = None if minor is None else Money(minor, Currency.USD)
    assert price_from_title(title) == expected


def test_real_feed_reads_every_priced_post() -> None:
    page = parse_ninetofivetoys_feed(load_fixture("ninetofivetoys", "feed").body)
    assert len(page.posts) == 21
    assert (page.skipped, page.unparsed, page.has_more, page.max_id) == (29, 0, False, 1238496)
    assert {p.channel for p in page.posts} == {"ninetofivetoys"}
    assert all(p.casillero and p.hashtags == () for p in page.posts)
    assert {p.link.host for p in page.posts} == {"9to5toys.com"}


def test_real_feed_first_priced_post() -> None:
    first = parse_ninetofivetoys_feed(load_fixture("ninetofivetoys", "feed").body).posts[0]
    assert first.post_id == 1238496
    assert first.price == Money(4000, Currency.USD)
    assert first.published_at == datetime(2026, 10, 22, 21, 58, 29, tzinfo=UTC)
    assert str(first.link) == ANKER_LINK
    assert first.post_url == first.link
    assert first.store is None  # su tienda es un acortador de eufy
    assert first.image_url is not None
    assert first.image_url.host == "9to5toys.com"
    assert "&#038;" not in str(first.image_url)


def test_store_is_amazon_when_the_first_store_link_is_amazon() -> None:
    (post,) = parse_ninetofivetoys_feed(feed(item())).posts
    assert post.store == "Amazon"
    other = (
        '<p>See <a href="https://9to5mac.com/x/">review</a>, <a href="https://howl.link/a">x</a>'
    )
    (post,) = parse_ninetofivetoys_feed(feed(item(description=other))).posts
    assert post.store is None


@pytest.mark.parametrize(
    ("text", "coupon"),
    [("<p>Use code TOYS20 at checkout.</p>", "TOYS20"), ("<p>No code needed.</p>", None)],
)
def test_coupon_comes_from_the_description(text: str, coupon: str | None) -> None:
    (post,) = parse_ninetofivetoys_feed(feed(item(description=text))).posts
    assert post.coupon == coupon


def test_post_without_price_is_skipped_quietly() -> None:
    page = parse_ninetofivetoys_feed(feed(item(title="LEGO confirms a Daft Punk Ideas set")))
    assert (page.posts, page.skipped, page.unparsed, page.max_id) == ((), 1, 0, 1238496)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"post_id": "abc"},
        {"link": "https://evil.example/x/"},
        {"title": ""},
        {"pub": "ayer"},
    ],
)
def test_unreadable_items_are_a_format_signal(kwargs: dict[str, str]) -> None:
    page = parse_ninetofivetoys_feed(feed(item(**kwargs)))
    assert (page.posts, page.unparsed) == ((), 1)


def test_image_only_from_9to5toys() -> None:
    text = '<img src="https://evil.example/x.jpg" /><p>now $16</p>'
    (post,) = parse_ninetofivetoys_feed(feed(item(description=text))).posts
    assert post.image_url is None


def test_feed_without_items_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_ninetofivetoys_feed(feed())


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
        parse_ninetofivetoys_feed(body)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300), st.text(max_size=300))
def test_parser_never_crashes_on_arbitrary_text(title: str, text: str) -> None:
    try:
        page = parse_ninetofivetoys_feed(feed(item(title=title, description=escape(text))))
    except ParseError:
        return
    assert isinstance(page, ChannelPage)


def test_fetch_posts_reads_the_feed_without_cursor() -> None:
    fake = FakeHttp({FEED_URL: load_fixture("ninetofivetoys", "feed")})
    source = NineToFiveToysSource()
    assert len(source.fetch_posts(ctx(fake), 1238496).posts) == 21
    assert [c[1] for c in fake.calls] == [FEED_URL]
    assert FEED_URL == "https://9to5toys.com/feed/"


def test_registered_as_unordered_channel() -> None:
    d = load_all()["ninetofivetoys"].descriptor
    assert (d.kind, d.ordered_ids, d.refeatures, d.client) == ("channel", False, False, "httpx")
    assert d.currency is Currency.USD
    assert d.hosts == d.image_hosts == frozenset({"9to5toys.com"})
    assert d.capabilities == frozenset()
