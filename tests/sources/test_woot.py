"""Ofertas de Woot por su API oficial (canal «sin verificar», ADR 0015)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.sources.fixtures import load_fixture
from discountz.sources.woot import CODE, offer_post_id, parse_woot_feeds

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
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
    ],
)
def test_offers_that_cannot_be_bought_new_on_the_web_are_skipped_quietly(
    changes: dict[str, Any],
) -> None:
    page = parse_woot_feeds([feed(offer(**changes))], NOW)
    assert page.posts == ()
    assert page.skipped == 1
    assert page.unparsed == 0


def test_new_condition_is_kept() -> None:
    assert len(parse_woot_feeds([feed(offer(Condition="New"))], NOW).posts) == 1


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


def test_fixture_feed_reads_its_offers() -> None:
    page = parse_woot_feeds([load_fixture("woot", "computers").body], NOW)
    assert len(page.posts) == 2
    assert page.skipped == 1  # una agotada
    assert all(p.price.currency is Currency.USD for p in page.posts)
