"""Falabella por el JSON de sus listados filtrados por vendedor (spec de Falabella, ADR 0017)."""

from __future__ import annotations

import json
import re

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.sources.falabella import parse_category, parse_product
from discountz.sources.fixtures import load_fixture

_NEXT = re.compile(rb'(<script id="__NEXT_DATA__" type="application/json">)(.*?)(</script>)', re.S)


def _body(name: str) -> bytes:
    return load_fixture("falabella", name).body


def _page(rows: list[dict[str, object]], count: int = 1) -> bytes:
    data = {"props": {"pageProps": {"results": rows, "pagination": {"count": count}}}}
    return (
        b'<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data).encode()
        + b"</script>"
    )


def _row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "productId": "100",
        "displayName": "Taladro",
        "url": "https://www.falabella.com.co/falabella-co/product/100/taladro",
        "sellerId": "FALABELLA_COLOMBIA",
        "sellerName": "Falabella",
        "isSponsored": False,
        "mediaUrls": ["https://media.falabella.com.co/falabellaCO/101_01/public"],
        "prices": [
            {"type": "internetPrice", "crossed": False, "price": ["199.900"]},
            {"type": "normalPrice", "crossed": True, "price": ["299.900"]},
        ],
    }
    row.update(over)
    return row


def _price(type_: str, *values: str, crossed: bool = False) -> dict[str, object]:
    return {"type": type_, "crossed": crossed, "price": list(values)}


def test_parse_category_keeps_only_what_the_store_sells_and_dedupes() -> None:
    page = parse_category(_body("category-page"))
    assert [i.external_id for i in page.items] == ["73557834", "73557831"]  # sin marketplace
    assert page.total == 39723


def test_parse_category_ignores_the_card_price() -> None:
    page = parse_category(_body("category-page"))
    macbook = next(i for i in page.items if i.external_id == "73557834")
    assert macbook.price == Money(749900, Currency.COP)  # CMR 699.900 exige tarjeta
    assert page.items[1].price == Money(2599900, Currency.COP)  # el «antes» tachado no cuenta


def test_parse_category_takes_the_lowest_of_two_values() -> None:
    page = parse_category(_body("category-ropa"))
    assert page.items[0].price == Money(43990, Currency.COP)


def test_parse_category_reads_the_event_price() -> None:
    rows = [
        _row(
            prices=[
                _price("eventPrice", "999.900"),
                _price("normalPrice", "1.999.900", crossed=True),
            ]
        )
    ]
    assert parse_category(_page(rows)).items[0].price == Money(999900, Currency.COP)


def test_parse_category_never_takes_the_crossed_price_as_the_price() -> None:
    rows = [_row(prices=[_price("normalPrice", "299.900", crossed=True)]), _row(productId="101")]
    page = parse_category(_page(rows))
    assert [i.external_id for i in page.items] == ["101"]


def test_parse_category_uses_an_uncrossed_normal_price_when_alone() -> None:
    rows = [_row(prices=[_price("normalPrice", "89.900")])]
    assert parse_category(_page(rows)).items[0].price == Money(89900, Currency.COP)


def test_parse_category_drops_card_only_products() -> None:
    rows = [_row(prices=[_price("cmrPrice", "50.000")]), _row(productId="101")]
    assert [i.external_id for i in parse_category(_page(rows)).items] == ["101"]


def test_parse_category_keeps_a_link_and_a_safe_image() -> None:
    item = parse_category(_page([_row()])).items[0]
    assert item.url == "/falabella-co/product/100/taladro"
    assert item.image_url == "https://media.falabella.com.co/falabellaCO/101_01/public"


def test_parse_category_drops_an_image_from_another_host() -> None:
    row = _row(mediaUrls=["https://cdn.ejemplo.com/a.jpg"])
    assert parse_category(_page([row])).items[0].image_url is None


def test_parse_category_ignores_a_page_of_pure_marketplace() -> None:
    rows = [_row(sellerId="SC1", sellerName="Otro")]
    page = parse_category(_page(rows))
    assert page.items == ()


def test_parse_category_with_only_broken_own_rows_is_a_format_change() -> None:
    with pytest.raises(ParseError, match="no_valid_items"):
        parse_category(_page([_row(prices=[]), _row(productId="x!", displayName="")]))


def test_parse_category_without_next_data() -> None:
    with pytest.raises(ParseError, match="no_next_data"):
        parse_category(b"<html></html>")


def test_parse_category_of_a_single_result_is_an_empty_page() -> None:
    body = _body("product")  # el filtro con 1 producto responde con la ficha
    page = parse_category(body)
    assert page.items == ()
    assert page.single


def test_parse_category_with_zero_results() -> None:
    page = parse_category(_page([], count=0))
    assert page.items == () and page.total == 0 and not page.single


def test_parse_product_reads_price_stock_and_seller() -> None:
    info = parse_product(_body("product"))
    assert info.price == Money(2599900, Currency.COP)
    assert info.in_stock and not info.marketplace
    assert (
        info.url
        == "https://www.falabella.com.co/falabella-co/product/73557831/port-14-ip-slim3-core5-16gb-512gb"
    )


def test_parse_product_marks_the_marketplace() -> None:
    info = parse_product(_body("product-marketplace"))
    assert info.marketplace
    assert info.price == Money(2029900, Currency.COP)  # sin la tarjeta CMR


def test_parse_product_out_of_stock() -> None:
    assert not parse_product(_body("product-out-of-stock")).in_stock


def test_parse_product_without_offerings_is_an_error() -> None:
    body = _NEXT.sub(
        lambda m: m.group(1) + m.group(2).replace(b'"offerings"', b'"x"') + m.group(3),
        _body("product"),
    )
    with pytest.raises(ParseError, match="no_seller"):
        parse_product(body)
