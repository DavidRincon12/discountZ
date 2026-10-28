"""Ofertas de Woot por su API oficial (canal «sin verificar», ADR 0015)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from discountz.domain.errors import CredentialUnavailable, ParseError
from discountz.domain.money import Currency, Money
from discountz.infra.http import HttpResponse
from discountz.sources.base import NoCredentials, SourceContext
from discountz.sources.fixtures import load_fixture
from discountz.sources.registry import load_all
from discountz.sources.woot import (
    CODE,
    FEEDS,
    IMAGE_HOSTS,
    WootSource,
    offer_post_id,
    parse_woot_feeds,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
RECORDED = datetime(2026, 10, 28, 23, 23, tzinfo=UTC)  # grabación de los feeds reales
OFFER_ID = "2a3deba2-6f5a-43df-875a-ea84709c9a30"


def offer(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "Categories": ["COMPUTERS", "COMPUTERS/Laptops"],
        "Condition": None,
        "EndDate": "2026-10-25T05:00:00+00:00",
        "IsSoldOut": False,
        "IsAvailableOnMobileAppOnly": False,
        "ListPrice": {"Minimum": 899.99, "Maximum": 899.99},
        "OfferId": OFFER_ID,
        "Photo": "https://d3gqasl9vmjfd8.cloudfront.net/c14c0b0d.jpg",
        "SalePrice": {"Minimum": 549.99, "Maximum": 549.99},
        "StartDate": "2026-10-23T05:00:00+00:00",
        "Title": 'Lenovo IdeaPad Slim 5 16" Laptop',
        "Url": "https://computers.woot.com/offers/lenovo-ideapad-slim-5-16-laptop",
    }
    base.update(changes)
    return base


def feed(*items: dict[str, Any]) -> bytes:
    page = {"Items": list(items), "MarketingName": "Computers", "TotalPages": 1}
    return json.dumps(page).encode()


def test_offer_becomes_a_post_bought_through_the_casillero() -> None:
    page = parse_woot_feeds([feed(offer())], NOW)
    (post,) = page.posts
    assert post.channel == CODE == "woot"
    assert post.post_id == offer_post_id(OFFER_ID)
    assert post.price == Money(549_99, Currency.USD)
    assert post.published_at == datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
    assert post.title == 'Lenovo IdeaPad Slim 5 16" Laptop'
    assert post.store == "Woot"
    assert post.casillero is True
    assert post.coupon is None
    assert str(post.link) == "https://computers.woot.com/offers/lenovo-ideapad-slim-5-16-laptop"
    assert post.post_url == post.link
    assert str(post.image_url) == "https://d3gqasl9vmjfd8.cloudfront.net/c14c0b0d.jpg"
    assert post.hashtags == ("computers", "laptops")
    assert page.max_id == post.post_id
    assert page.has_more is False


def test_price_keeps_the_cents_exactly() -> None:
    sale = {"Minimum": 0.29, "Maximum": 0.29}
    (post,) = parse_woot_feeds([feed(offer(SalePrice=sale))], NOW).posts
    assert post.price == Money(29, Currency.USD)  # con float, 0.29 * 100 da 28.999…


def test_price_range_uses_the_minimum() -> None:
    sale = {"Minimum": 24.99, "Maximum": 39.99}
    (post,) = parse_woot_feeds([feed(offer(SalePrice=sale))], NOW).posts
    assert post.price == Money(24_99, Currency.USD)


def test_post_id_is_stable_positive_and_fits_bigint() -> None:
    a = offer_post_id(OFFER_ID)
    assert a is not None
    assert a == offer_post_id(OFFER_ID.upper())
    assert 0 < a < 2**63
    assert a != offer_post_id("33fb06a8-6c83-47c6-bcdc-ddf8d6780dea")


@pytest.mark.parametrize("raw", ["", "no-es-uuid", "00000000-0000-0000-0000-000000000000", None, 7])
def test_unusable_offer_id_has_no_post_id(raw: object) -> None:
    assert offer_post_id(raw) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"IsSoldOut": True},
        {"IsAvailableOnMobileAppOnly": True},
        {"EndDate": "2026-10-23T11:59:00+00:00"},
        {"Condition": "Refurbished"},
        {"Condition": "Used - Very Good"},
        # valores reales de los feeds (2026-10-28)
        {"Condition": "New; Open Box"},
        {"Condition": "Grade A Refurbished"},
        {"Condition": "Like-New"},
        {"Condition": "Scratch & Dent"},
    ],
)
def test_offers_that_cannot_be_bought_new_on_the_web_are_skipped_quietly(
    changes: dict[str, Any],
) -> None:
    page = parse_woot_feeds([feed(offer(**changes))], NOW)
    assert page.posts == ()
    assert page.skipped == 1
    assert page.unparsed == 0


@pytest.mark.parametrize("condition", ["New", "NEW", "New "])  # las tres salen en los feeds
def test_new_condition_is_kept(condition: str) -> None:
    assert len(parse_woot_feeds([feed(offer(Condition=condition))], NOW).posts) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"OfferId": "x"},
        {"Title": "  "},
        {"SalePrice": None},
        {"SalePrice": {"Minimum": "gratis"}},
        {"SalePrice": {"Minimum": 0}},
        {"StartDate": "2026-10-23T05:00:00"},  # sin zona
        {"Url": "https://woot.evil.com/offers/x"},
        {"Url": "http://computers.woot.com/offers/x"},
    ],
)
def test_unreadable_offers_are_a_format_signal(changes: dict[str, Any]) -> None:
    page = parse_woot_feeds([feed(offer(**changes))], NOW)
    assert page.posts == ()
    assert page.unparsed == 1


def test_photo_from_another_host_is_dropped() -> None:
    (post,) = parse_woot_feeds([feed(offer(Photo="https://evil.example/a.jpg"))], NOW).posts
    assert post.image_url is None


def test_same_offer_in_two_feeds_counts_once() -> None:
    other = offer(OfferId="33fb06a8-6c83-47c6-bcdc-ddf8d6780dea")
    page = parse_woot_feeds([feed(offer(), other), feed(offer())], NOW)
    assert len(page.posts) == 2
    assert page.skipped == 0


def test_empty_feed_is_an_empty_page() -> None:
    page = parse_woot_feeds([feed(), feed()], NOW)
    assert page.posts == ()
    assert page.max_id is None
    assert page.unparsed == 0


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"Items": {}}', b'{"Message": "x"}'])
def test_body_that_is_not_a_feed_page_is_a_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_woot_feeds([body], NOW)


@pytest.mark.parametrize(
    ("name", "posts", "skipped"),
    [("computers", 261, 347), ("electronics", 482, 264), ("tools", 1481, 101)],
)
def test_real_feed_is_read_without_format_errors(name: str, posts: int, skipped: int) -> None:
    page = parse_woot_feeds([load_fixture("woot", name).body], RECORDED)
    assert (len(page.posts), page.skipped, page.unparsed) == (posts, skipped, 0)
    assert all(p.price.currency is Currency.USD and p.price.minor > 0 for p in page.posts)
    assert all(p.image_url is not None for p in page.posts)  # todas las fotos en su CDN


def test_real_offer_keeps_its_fields() -> None:
    page = parse_woot_feeds([load_fixture("woot", "computers").body], RECORDED)
    wanted = offer_post_id("a3692bc8-970b-4d9e-85b5-e6073e844654")
    (post,) = (p for p in page.posts if p.post_id == wanted)
    assert post.title == "PowerA MOGA XP5-A Plus Bluetooth Controller"
    assert post.price == Money(14_99, Currency.USD)
    assert post.published_at == datetime(2026, 10, 26, 5, 0, tzinfo=UTC)
    assert str(post.link) == (
        "https://computers.woot.com/offers/powera-moga-xp5-a-plus-bluetooth-controller"
    )
    assert post.image_url is not None
    assert post.image_url.host == "d3gqasl9vmjfd8.cloudfront.net"
    assert post.hashtags == ("pc", "peripherals", "accessories")


def test_real_feeds_together_count_shared_offers_once() -> None:
    bodies = [load_fixture("woot", feed_name.lower()).body for feed_name in FEEDS]
    alone = [parse_woot_feeds([body], RECORDED) for body in bodies]
    together = parse_woot_feeds(bodies, RECORDED)
    unique = {p.post_id for page in alone for p in page.posts}
    assert sum(len(page.posts) for page in alone) > len(together.posts)  # hay ofertas repetidas
    assert len(together.posts) == len(unique) == 2204


class _Recorder:
    """Transporte que guarda URL y cabeceras de cada petición."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body, self.status = body, status
        self.calls: list[tuple[str, dict[str, str]]] = []

    def request(self, method: str, url: str, **kw: Any) -> HttpResponse:
        self.calls.append((url, dict(kw.get("headers") or {})))
        return HttpResponse(self.status, url, {}, self.body, 0)


class _Key:
    def bearer(self, provider: str) -> SecretStr:
        assert provider == "woot"
        return SecretStr("clave")


def _ctx(http: object, credentials: object) -> SourceContext:
    return SourceContext(http, credentials, 1e9, NOW)  # type: ignore[arg-type]


def test_source_reads_the_three_feeds_with_the_api_key() -> None:
    http = _Recorder(feed(offer()))
    page = WootSource().fetch_posts(_ctx(http, _Key()), None)
    assert [u for u, _ in http.calls] == [f"https://developer.woot.com/feed/{f}" for f in FEEDS]
    assert FEEDS == ("Computers", "Electronics", "Tools")
    assert all(h == {"Accept": "application/json", "x-api-key": "clave"} for _, h in http.calls)
    assert len(page.posts) == 1  # la misma oferta en los tres feeds


def test_source_without_key_is_unavailable_before_asking() -> None:
    http = _Recorder(feed())
    with pytest.raises(CredentialUnavailable):
        WootSource().fetch_posts(_ctx(http, NoCredentials()), None)
    assert http.calls == []


def test_forbidden_answer_is_a_bad_key_not_a_bot_block() -> None:
    resp = HttpResponse(403, "https://developer.woot.com/feed/Computers", {}, b'{"message":""}', 0)
    with pytest.raises(CredentialUnavailable) as exc:
        WootSource().detect_block(resp)
    assert exc.value.code == "bad_key"


def test_descriptor_is_an_unordered_usd_channel_and_registered() -> None:
    d = WootSource().descriptor
    assert (d.code, d.kind, d.currency, d.auth) == ("woot", "channel", Currency.USD, "api_key")
    assert d.ordered_ids is False
    assert d.refeatures is True
    assert d.image_hosts == IMAGE_HOSTS
    assert "computers.woot.com" in d.hosts
    assert "woot" in load_all()
