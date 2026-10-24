"""Homecenter por el JSON de sus páginas de categoría (spec de Homecenter, ADR 0016)."""

from __future__ import annotations

import gzip
import json
import re

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.homecenter import parse_category, parse_product

_NEXT = re.compile(rb'(<script id="__NEXT_DATA__" type="application/json">)(.*?)(</script>)', re.S)


def _body(name: str) -> bytes:
    return load_fixture("homecenter", name).body


def _edit(name: str, change: object) -> bytes:
    """La fixture con su `__NEXT_DATA__` modificado por `change(datos)`."""
    body = _body(name)
    m = _NEXT.search(body)
    assert m is not None
    data = json.loads(m.group(2))
    change(data)  # type: ignore[operator]
    return body[: m.start(2)] + json.dumps(data).encode() + body[m.end(2) :]


def _results(data: dict[str, object]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = data["props"]["pageProps"]["searchProps"]["searchData"][  # type: ignore[index]
        "results"
    ]
    return found


def test_fixtures_are_recorded() -> None:
    assert (FIXTURES_DIR / "homecenter" / "category-page.html.gz").exists()
    assert gzip.decompress((FIXTURES_DIR / "homecenter" / "category-page.html.gz").read_bytes())


def test_parse_category_prefers_internet_price_over_normal() -> None:
    page = parse_category(_body("category-page"))
    combo = next(i for i in page.items if i.external_id == "877508")
    assert combo.price == Money(244900, Currency.COP)


def test_parse_category_ignores_the_card_price() -> None:
    page = parse_category(_body("category-page"))
    welder = next(i for i in page.items if i.external_id == "915074")
    assert welder.price == Money(499900, Currency.COP)  # CMR 449.900 exige tarjeta


def test_parse_category_uses_normal_when_it_is_the_only_price() -> None:
    page = parse_category(_body("category-page"))
    drill = next(i for i in page.items if i.external_id == "543286")
    assert drill.price == Money(638900, Currency.COP)


def test_parse_category_drops_sponsored_products() -> None:
    page = parse_category(_body("category-page"))
    assert [i.external_id for i in page.items] == [
        "877508",
        "3033371",
        "915074",
        "281433",
        "543286",
    ]


def test_parse_category_reads_total_title_image_and_link() -> None:
    page = parse_category(_body("category-page"))
    assert page.total == 8868
    welder = next(i for i in page.items if i.external_id == "915074")
    assert welder.title == "Soldador Inversor 160AMP + Máscara Fotosensible Bauker"
    assert welder.image_url == "https://media.falabella.com/sodimacCO/915074/public"
    assert welder.path == (
        "/homecenter-co/product/915074/soldador-inversor-160amp-mascara-fotosensible-bauker/915074/"
    )


def test_parse_category_drops_a_repeated_id() -> None:
    def duplicate(data: dict[str, object]) -> None:
        rows = _results(data)
        rows.append(dict(rows[0]))

    page = parse_category(_edit("category-page", duplicate))
    assert [i.external_id for i in page.items].count("877508") == 1


def test_parse_category_drops_a_product_with_only_the_card_price() -> None:
    def only_card(data: dict[str, object]) -> None:
        _results(data)[0]["prices"] = [
            {"type": "CMR", "priceWithoutFormatting": 100000, "price": "100.000"}
        ]

    page = parse_category(_edit("category-page", only_card))
    assert "877508" not in {i.external_id for i in page.items}
    assert len(page.items) == 4


def test_parse_category_keeps_the_readable_ones_when_one_is_broken() -> None:
    def broken(data: dict[str, object]) -> None:
        _results(data)[1]["productId"] = "no-es-un-id"

    page = parse_category(_edit("category-page", broken))
    assert "3033371" not in {i.external_id for i in page.items}
    assert len(page.items) == 4


def test_parse_category_without_any_readable_product_is_a_parse_error() -> None:
    def all_broken(data: dict[str, object]) -> None:
        for row in _results(data):
            row["prices"] = []
            row["isPromoted"] = False

    with pytest.raises(ParseError) as exc:
        parse_category(_edit("category-page", all_broken))
    assert exc.value.code == "no_valid_items"


def test_parse_category_without_next_data_is_a_parse_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse_category(b"<html><body>nada</body></html>")
    assert exc.value.code == "no_next_data"


def test_parse_category_with_an_empty_page_is_not_an_error() -> None:
    def empty(data: dict[str, object]) -> None:
        data["props"]["pageProps"]["searchProps"]["searchData"]["results"] = []  # type: ignore[index]

    page = parse_category(_edit("category-page", empty))
    assert page.items == ()
    assert page.total == 8868


def test_parse_product_reads_price_stock_and_seller() -> None:
    info = parse_product(_body("product"))
    assert info.price == Money(499900, Currency.COP)
    assert info.in_stock is True
    assert info.marketplace is False
    assert info.title == "Soldador Inversor 160AMP + Máscara Fotosensible Bauker"


def test_parse_product_flags_marketplace_sellers() -> None:
    assert parse_product(_body("product-marketplace")).marketplace is True


def test_parse_product_without_stock_in_any_shipping() -> None:
    assert parse_product(_body("product-out-of-stock")).in_stock is False


def test_parse_product_without_next_data_is_a_parse_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse_product(b"<html></html>")
    assert exc.value.code == "no_next_data"
