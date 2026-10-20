"""Alkosto por su índice de Algolia (spec de Alkosto §2 y §7, ADR 0009)."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.sources.alkosto import parse_api_key, parse_objects, parse_query
from discountz.sources.fixtures import load_fixture

TV = "7705946474559"


def _body(name: str) -> bytes:
    return load_fixture("alkosto", name).body


def _band() -> dict[str, object]:
    data: dict[str, object] = json.loads(_body("query-band"))
    return data


def _with_hit(**changes: object) -> bytes:
    """La franja grabada con el primer hit (el TV) modificado; `None` borra el campo."""
    data = _band()
    hits = data["hits"]
    assert isinstance(hits, list)
    hit = dict(hits[0])
    for key, value in changes.items():
        if value is None:
            hit.pop(key, None)
        else:
            hit[key] = value
    hits[0] = hit
    return json.dumps(data).encode()


def test_parse_api_key_reads_home() -> None:
    assert parse_api_key(_body("home")) == "CLAVE_DE_PRUEBA"


def test_parse_api_key_missing_is_parse_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse_api_key(b"<html><script>var x = 1;</script></html>")
    assert exc.value.code == "api_key"


def test_parse_query_uses_lowest_price_not_list_price() -> None:
    items, _ = parse_query(_with_hit(pricevalue_cop_double=3899900.0))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.price == Money(1649900, Currency.COP)


def test_parse_query_reads_stock_ean_url_image_path() -> None:
    items, hits = parse_query(_body("query-band"))
    assert hits == 327
    assert len(items) == 30
    tv = next(i for i in items if i.external_id == TV)
    assert tv.in_stock is True
    assert tv.ean == TV
    assert tv.url.startswith("https://www.alkosto.com/")
    assert tv.url.endswith(f"/p/{TV}")
    assert tv.image_url is not None
    assert tv.image_url.startswith("https://cdn.dam.alkosto.com/")
    assert tv.category_path == "/tv/smart tv/"
    assert tv.title.startswith("TV KALLEY 60")


def test_parse_query_falls_back_to_750_image() -> None:
    items, _ = parse_query(_with_hit(**{"img-820wx820h_string": None}))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.image_url is not None
    assert "w=750" in tv.image_url


@given(st.sampled_from([None, 0, -5, 12.5, "1649900"]))
def test_parse_query_skips_bad_price(price: object) -> None:
    items, _ = parse_query(_with_hit(lowestprice_double=price))
    ids = {i.external_id for i in items}
    assert TV not in ids
    assert len(ids) == 29


@pytest.mark.parametrize("url", ["https://evil.com/p/1", "", "//evil.com/p/1"])
def test_parse_query_skips_foreign_url(url: str) -> None:
    items, _ = parse_query(_with_hit(url_es_string=url))
    assert TV not in {i.external_id for i in items}
    assert len(items) == 29


def test_parse_query_out_of_stock() -> None:
    items, _ = parse_query(_with_hit(stocklevel_int=0))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.in_stock is False
    assert tv.price is None


def test_parse_query_ean_only_with_gtin_length() -> None:
    items, _ = parse_query(_with_hit(code_string="ABC123"))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.ean is None


def test_parse_objects_omits_missing() -> None:
    found = parse_objects(_body("objects"))
    assert len(found) == 2
    assert "0000000000000" not in found
    assert all(item.external_id == key for key, item in found.items())


def test_parse_query_rejects_garbage() -> None:
    with pytest.raises(ParseError):
        parse_query(b'{"message": "nada"}')
    with pytest.raises(ParseError):
        parse_query(b"<html>")
