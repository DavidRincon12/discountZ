"""Adaptador Walmart US contra fixtures reales del 2026-10-13 (spec §4.2 «Walmart»)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from discountz.domain.errors import NotFound, ParseError
from discountz.domain.guards import batch_is_constant
from discountz.domain.model import ListingRef, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.sources.base import ListingFailed, ListingFound, NoCredentials, SourceContext
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.registry import load_all
from discountz.sources.walmart import (
    WalmartSource,
    parse_walmart_detail,
    parse_walmart_independent,
    parse_walmart_search,
)

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
FICHAS = ["13573063573", "19912811513", "5113175776"]


def expected(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "walmart_us" / f"{name}.expected.json").read_text()
    )
    return data


def body(name: str) -> bytes:
    return load_fixture("walmart_us", name).body


def usd(minor: int | None) -> Money | None:
    return None if minor is None else Money(minor, Currency.USD)


@pytest.mark.parametrize("item", FICHAS)
def test_detail_fixture_matches_expected(item: str) -> None:
    exp = expected(f"ficha_{item}")
    o = parse_walmart_detail(body(f"ficha_{item}"), item, T)
    assert o is not None
    assert o.ref == ListingRef("walmart_us", exp["external_id"])
    assert o.title == exp["title"]
    assert o.price == usd(exp["price"])
    assert o.list_price == usd(exp["list_price"])
    assert o.in_stock is exp["in_stock"]
    assert o.seller_class == SellerClass(exp["seller_class"])
    assert o.unit_key == exp["unit_key"]
    assert o.url_path == exp["url_path"]


def test_protection_plan_price_is_never_the_product_price() -> None:
    """El 24,00 del spike era «3-Year Plan - $24.00»: la ficha da 199,00, no 24,00."""
    raw = body("ficha_13573063573")
    assert b"3-Year Plan - $24.00" in raw
    o = parse_walmart_detail(raw, "13573063573", T)
    assert o is not None and o.price == usd(19_900)


@pytest.mark.parametrize("item", FICHAS)
def test_independent_route_matches_main_route_when_present(item: str) -> None:
    exp = expected(f"ficha_{item}")
    main = parse_walmart_detail(body(f"ficha_{item}"), item, T)
    o = parse_walmart_independent(body(f"ficha_{item}"), item, T)
    if exp["independent_price"] is None:
        assert o is None  # «See price in cart»: sin precio visible no hay ruta
        return
    assert main is not None and o is not None
    assert o.price == main.price == usd(exp["independent_price"])
    assert o.seller_class is main.seller_class
    assert o.in_stock and o.title == main.title


def test_search_skips_sponsored_preowned_and_duplicates() -> None:
    exp = expected("busqueda_ssd")
    offers = parse_walmart_search(body("busqueda_ssd"), T)
    ids = [o.ref.external_id for o in offers]
    assert ids[: len(exp["first_ids"])] == exp["first_ids"]
    assert len(ids) == len(set(ids))
    assert not set(exp["sponsored_only_ids"]) & set(ids)
    first = offers[0]
    assert first.price == usd(exp["first"]["price"])
    assert first.seller_class == SellerClass(exp["first"]["seller_class"])
    assert first.in_stock is exp["first"]["in_stock"]
    assert not batch_is_constant(offers)


def test_detail_without_next_data_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_walmart_detail(b"<html><title>Robot or human?</title></html>", "13573063573", T)


def test_detail_with_other_currency_is_parse_error() -> None:
    data = {
        "props": {
            "pageProps": {
                "initialData": {
                    "data": {
                        "product": {
                            "usItemId": "13573063573",
                            "name": "SSD",
                            "availabilityStatus": "IN_STOCK",
                            "sellerName": "Walmart.com",
                            "priceInfo": {"currentPrice": {"price": 199, "currencyUnit": "CAD"}},
                        }
                    }
                }
            }
        }
    }
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    with pytest.raises(ParseError):
        parse_walmart_detail(html.encode(), "13573063573", T)


def test_parse_url_variants() -> None:
    src = WalmartSource()
    hosts = src.descriptor.hosts

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, hosts))

    assert parse("https://www.walmart.com/ip/WD-BLACK-1TB/13573063573?classType=REGULAR") == (
        ListingRef("walmart_us", "13573063573")
    )
    assert parse("https://www.walmart.com/ip/13573063573") == ListingRef(
        "walmart_us", "13573063573"
    )
    assert parse("https://www.walmart.com/search?q=ssd") is None


def test_fetch_follows_one_validated_redirect() -> None:
    canonical = "https://www.walmart.com/ip/13573063573"
    slug = "/ip/WD-BLACK-1TB/13573063573"
    redirect = HttpResponse(301, canonical, {"Location": slug}, b"redirecting...", 1)
    fake = FakeHttp(
        {
            canonical: redirect,
            f"https://www.walmart.com{slug}": load_fixture("walmart_us", "ficha_13573063573"),
        }
    )
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = WalmartSource().fetch_listings(ctx, [ListingRef("walmart_us", "13573063573")])
    assert isinstance(out, ListingFound) and out.offer.price == usd(19_900)


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/ip/x/13573063573", "/ip/x/99999999", "/blocked?url=Lw=="],
)
def test_fetch_rejects_foreign_redirects(location: str) -> None:
    canonical = "https://www.walmart.com/ip/13573063573"
    fake = FakeHttp({canonical: HttpResponse(302, canonical, {"Location": location}, b"", 1)})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = WalmartSource().fetch_listings(ctx, [ListingRef("walmart_us", "13573063573")])
    assert isinstance(out, ListingFailed) and isinstance(out.error, NotFound)
    assert len(fake.calls) == 1


def test_registered_and_curl() -> None:
    src = load_all()["walmart_us"]
    assert src.descriptor.client == "curl"
