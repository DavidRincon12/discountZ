"""Adaptador Amazon US contra fixtures reales (spec §4.2, §4.3 y §3.6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.model import ListingRef, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, FakeHttp, HttpResponse
from discountz.sources.amazon import (
    AmazonSource,
    parse_amazon_detail,
    parse_amazon_independent,
    parse_amazon_search,
)
from discountz.sources.base import ListingFound, NeedsResolution, NoCredentials, SourceContext
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.registry import load_all

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
FICHAS = ["B0BHJF2VRN", "B0F43QH751", "B07MFZY2F2"]


def expected(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "amazon_us" / f"{name}.expected.json").read_text()
    )
    return data


def body(name: str) -> bytes:
    return load_fixture("amazon_us", name).body


def usd(minor: int | None) -> Money | None:
    return None if minor is None else Money(minor, Currency.USD)


@pytest.mark.parametrize("asin", FICHAS)
def test_detail_fixture_matches_expected(asin: str) -> None:
    exp = expected(f"ficha_{asin}")
    o = parse_amazon_detail(body(f"ficha_{asin}"), asin, T)
    assert o is not None
    assert o.ref == ListingRef("amazon_us", exp["external_id"])
    assert o.title == exp["title"]
    assert o.price == usd(exp["price"])
    assert o.list_price == usd(exp["list_price"])
    assert o.in_stock is exp["in_stock"]
    assert o.seller_class == SellerClass(exp["seller_class"])
    assert o.unit_key == exp["unit_key"]
    assert o.url_path == exp["url_path"]


@pytest.mark.parametrize("asin", FICHAS)
def test_independent_route_matches_expected(asin: str) -> None:
    exp = expected(f"ficha_{asin}")
    o = parse_amazon_independent(body(f"ficha_{asin}"), asin, T)
    if exp["independent_price"] is None:
        assert o is None  # sin twister-plus-price-data-price no hay ruta independiente
    else:
        assert o is not None and o.price == usd(exp["independent_price"])


def test_search_skips_sponsored_and_duplicates() -> None:
    exp = expected("busqueda_ssd")
    offers = parse_amazon_search(body("busqueda_ssd"), T)
    assert [o.ref.external_id for o in offers] == exp["organic_asins"]
    first = offers[0]
    assert first.price == usd(exp["first"]["price"])
    assert first.list_price == usd(exp["first"]["list_price"])
    assert first.in_stock


def test_search_result_without_main_price_is_out_of_stock() -> None:
    html = b"""<div data-component-type="s-search-result" data-asin="B0AAAAAAAA">
      <h2><span>Disco SSD 1TB</span></h2>
      <div class="a-color-secondary">More Buying Choices <span class="a-color-base">$45.00</span>
      (3 used &amp; new offers)</div><a>See options</a></div>"""
    (o,) = parse_amazon_search(html, T)
    assert o.price is None
    assert not o.in_stock


def test_detail_with_cop_text_is_parse_error() -> None:
    """U14: «COP 520,000» en una tienda USD es ParseError (G2), nunca 52.000.000 ¢."""
    html = b"""<span id="productTitle">Monitor</span><input id="ASIN" value="B0AAAAAAAA">
      <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">COP 520,000
      </span></span></div>"""
    with pytest.raises(ParseError):
        parse_amazon_detail(html, "B0AAAAAAAA", T)


def test_detail_without_title_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_amazon_detail(b"<html><body>hola</body></html>", "B0AAAAAAAA", T)


def test_detail_follows_observed_asin() -> None:
    o = parse_amazon_detail(body("ficha_B0BHJF2VRN"), "B0ZZZZZZZZ", T)
    assert o is not None and o.ref.external_id == "B0BHJF2VRN"  # G7: el observado manda


def test_pack_title_sets_unit_key() -> None:
    html = b"""<span id="productTitle">Pilas AA (24 Pack)</span>
      <input id="ASIN" value="B0AAAAAAAA"><div id="corePrice_feature_div">
      <span class="a-price"><span class="a-offscreen">$19.99</span></span></div>
      <div id="availability"><span>In Stock</span></div>"""
    o = parse_amazon_detail(html, "B0AAAAAAAA", T)
    assert o is not None and o.unit_key == "pk|24"


def test_detect_block_captcha_and_akamai() -> None:
    src = AmazonSource()
    captcha = HttpResponse(
        200,
        "https://www.amazon.com/errors/validateCaptcha",
        {},
        b"<title>Robot Check</title>Enter the characters you see below",
        1,
    )
    assert src.detect_block(captcha) is BlockKind.CAPTCHA
    assert src.detect_block(load_fixture("amazon_us", "bloqueo_akamai")) is BlockKind.AKAMAI
    assert src.detect_block(load_fixture("amazon_us", "ficha_B0BHJF2VRN")) is None


def test_parse_url_variants() -> None:
    src = AmazonSource()
    hosts = src.descriptor.hosts

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, hosts))

    assert parse("https://www.amazon.com/Samsung-990/dp/B0BHJF2VRN/ref=sr_1_3?th=1") == ListingRef(
        "amazon_us", "B0BHJF2VRN"
    )
    assert parse("https://www.amazon.com/gp/product/B0BHJF2VRN") == ListingRef(
        "amazon_us", "B0BHJF2VRN"
    )
    assert parse("https://a.co/d/abc123") == NeedsResolution("amazon_us", "short:aco/d/abc123")
    assert parse("https://www.amazon.com/s?k=ssd") is None


def test_fetch_listings_reads_canonical_dp() -> None:
    url = "https://www.amazon.com/dp/B0BHJF2VRN"
    fake = FakeHttp({url: load_fixture("amazon_us", "ficha_B0BHJF2VRN")})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = AmazonSource().fetch_listings(ctx, [ListingRef("amazon_us", "B0BHJF2VRN")])
    assert isinstance(out, ListingFound)
    assert out.offer.price == usd(25999)


def test_confirm_uses_twister_price() -> None:
    url = "https://www.amazon.com/dp/B0BHJF2VRN"
    fake = FakeHttp({url: load_fixture("amazon_us", "ficha_B0BHJF2VRN")})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    src = AmazonSource()
    o = parse_amazon_detail(body("ficha_B0BHJF2VRN"), "B0BHJF2VRN", T)
    assert o is not None
    confirmed = src.confirm(ctx, o)
    assert confirmed is not None and confirmed.price == usd(25999)


def test_registered() -> None:
    assert "amazon_us" in load_all()
