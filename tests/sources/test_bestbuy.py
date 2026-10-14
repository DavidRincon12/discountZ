"""Adaptador Best Buy US (spec §4.2 «Best Buy»).

La búsqueda es real (2026-10-13) y las fichas también (2026-10-14, grabadas con `record-fixture`
desde la red de David: desde la nube de la sesión responden 502 o se cuelgan).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.guards import batch_is_constant
from discountz.domain.model import ListingRef, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.sources.base import ListingFound, NoCredentials, SourceContext
from discountz.sources.bestbuy import (
    BestBuySource,
    parse_bestbuy_detail,
    parse_bestbuy_independent,
    parse_bestbuy_search,
)
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.registry import load_all

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
FICHAS = ["6680742", "6385543"]


def expected(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "bestbuy_us" / f"{name}.expected.json").read_text()
    )
    return data


def body(name: str) -> bytes:
    return load_fixture("bestbuy_us", name).body


def usd(minor: int | None) -> Money | None:
    return None if minor is None else Money(minor, Currency.USD)


@pytest.mark.parametrize("sku", FICHAS)
def test_detail_fixture_matches_expected(sku: str) -> None:
    exp = expected(f"ficha_{sku}")
    o = parse_bestbuy_detail(body(f"ficha_{sku}"), sku, T)
    assert o is not None
    assert o.ref == ListingRef("bestbuy_us", exp["external_id"])
    assert o.title == exp["title"]
    assert o.price == usd(exp["price"])
    assert o.list_price == usd(exp["list_price"])
    assert o.in_stock is exp["in_stock"]
    assert o.seller_class == SellerClass(exp["seller_class"])
    assert o.unit_key == exp["unit_key"]
    assert o.url_path == exp["url_path"]


@pytest.mark.parametrize("sku", FICHAS)
def test_independent_route_matches_main_route(sku: str) -> None:
    """El precio del estado Apollo coincide con el JSON-LD; el de la garantía nunca entra."""
    exp = expected(f"ficha_{sku}")
    main = parse_bestbuy_detail(body(f"ficha_{sku}"), sku, T)
    o = parse_bestbuy_independent(body(f"ficha_{sku}"), sku, T)
    assert main is not None and o is not None
    assert o.price == main.price == usd(exp["independent_price"])
    assert o.in_stock is main.in_stock
    assert o.seller_class is SellerClass.FIRST_PARTY


_APOLLO = (
    '<html><h1 class="text-5">Samsung - Internal SSD 990 1TB</h1><script>(window[Symbol.for('
    '"ApolloSSRDataTransport")] ??= []).push({"events":[{"type":"next","value":{"data":'
    '{"productBySkuId":{"price":{"__typename":"ItemPrice","customerPrice":PRICE,'
    '"mobileContracts":null,"skuId":"SKU","totalSavings":40},"displayableRegularPrice":229.99,'
    '"fulfillmentOptions":{"buttonStates":[{"buttonState":"BUTTON"}]}}}}}]})</script></html>'
)


def _apollo(price: str, sku: str, button: str) -> bytes:
    html = _APOLLO.replace("PRICE", price).replace("SKU", sku).replace("BUTTON", button)
    return html.encode()


def test_independent_route_reads_apollo_price_of_the_same_sku() -> None:
    o = parse_bestbuy_independent(_apollo("189.99", "6680742", "ADD_TO_CART"), "6680742", T)
    assert o is not None
    assert (o.price, o.list_price, o.in_stock) == (usd(18_999), usd(22_999), True)
    assert o.title == "Samsung - Internal SSD 990 1TB"


def test_independent_route_ignores_other_skus_and_sold_out() -> None:
    other = _apollo("49.99", "6440012", "ADD_TO_CART")
    assert parse_bestbuy_independent(other, "6680742", T) is None
    o = parse_bestbuy_independent(_apollo("189.99", "6680742", "SOLD_OUT"), "6680742", T)
    assert o is not None and o.in_stock is False and o.price is None


def test_real_search_has_no_prices_and_never_the_plus_membership() -> None:
    """El 49,99 constante del spike era la membresía Plus: sin precio en el HTML no hay oferta."""
    exp = expected("busqueda_ssd")
    raw = body("busqueda_ssd")
    assert raw.count(b'class="product-list-item grid-view"') == exp["product_cells"]
    offers = parse_bestbuy_search(raw, T)
    assert len(offers) == exp["offers"]
    assert all(o.price != usd(4_999) for o in offers)


def test_search_reads_priced_cells() -> None:
    html = b"""<ul>
      <li class="product-list-item" data-testid="6680742"><a class="product-list-item-link"
        href="https://www.bestbuy.com/product/samsung-990/JJGRF3QVGW/sku/6680742">
        <h3 class="product-title" title="Samsung - Internal SSD 990 1TB">Samsung</h3></a>
        <div class="priceView-customer-price"><span aria-hidden="true">$99.99</span></div></li>
      <li class="product-list-item" data-testid="6523595"><h3 class="product-title"
        title="Samsung - 990 PRO 2TB Internal SSD">x</h3>
        <div class="priceView-customer-price"><span aria-hidden="true">$169.99</span></div>
        <button class="add-to-cart-button">Sold Out</button></li>
      <li class="product-list-item" data-testid="6385543"><h3 class="product-title"
        title="PNY CS900 1TB">x</h3></li>
      <li class="product-list-item" data-testid="sponsored"><h3 class="product-title"
        title="Anuncio">x</h3></li>
    </ul>"""
    offers = parse_bestbuy_search(html, T)
    assert [(o.ref.external_id, o.price, o.in_stock) for o in offers] == [
        ("6680742", usd(9_999), True),
        ("6523595", None, False),
    ]
    assert all(o.seller_class is SellerClass.FIRST_PARTY for o in offers)
    assert not batch_is_constant(offers)


def test_detail_without_product_json_ld_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_bestbuy_detail(b"<html><h1>hola</h1></html>", "6680742", T)


def test_parse_url_variants() -> None:
    src = BestBuySource()
    hosts = src.descriptor.hosts

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, hosts))

    ref = ListingRef("bestbuy_us", "6680742")
    assert parse("https://www.bestbuy.com/site/samsung-990/6680742.p?skuId=6680742") == ref
    assert parse("https://www.bestbuy.com/site/6680742.p?skuId=6680742") == ref
    assert parse("https://www.bestbuy.com/product/samsung-990/JJGRF3QVGW/sku/6680742") == ref
    assert parse("https://www.bestbuy.com/product/samsung-990/JJGRF3QVGW") is None
    assert parse("https://www.bestbuy.com/site/searchpage.jsp?st=ssd") is None


def test_fetch_follows_one_redirect_to_the_product_page() -> None:
    canonical = "https://www.bestbuy.com/site/6680742.p?skuId=6680742&intl=nosplash"
    target = "https://www.bestbuy.com/product/samsung-990/JJGRF3QVGW?intl=nosplash"
    fake = FakeHttp(
        {
            canonical: HttpResponse(301, canonical, {"location": target}, b"", 1),
            target: load_fixture("bestbuy_us", "ficha_6680742"),
        }
    )
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = BestBuySource().fetch_listings(ctx, [ListingRef("bestbuy_us", "6680742")])
    assert isinstance(out, ListingFound) and out.offer.price == usd(18_999)


def test_registered_curl_and_gated() -> None:
    src = load_all()["bestbuy_us"]
    assert src.descriptor.client == "curl"
