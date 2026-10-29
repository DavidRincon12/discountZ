"""Mercado Libre por la API de su catálogo (spec de Mercado Libre, ADR 0018)."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.money import Currency, Money
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.mercadolibre import (
    MeliSeller,
    parse_leader,
    parse_product,
    parse_search,
    parse_sellers,
)

_FIXTURES = (
    "search-ean",
    "search-ean-no-zeros",
    "search-empty",
    "items",
    "items-none",
    "product",
    "user",
)
_NOT_OFFICIAL = {272545453, 324693682, 3339126792, 222480760, 1200436618, 629159107}


def _body(name: str) -> bytes:
    return load_fixture("mercadolibre", name).body


def _edit(name: str, change: Callable[[dict[str, object]], object]) -> bytes:
    """La fixture con su JSON modificado por `change(datos)`."""
    data = json.loads(_body(name))
    change(data)
    return json.dumps(data).encode()


def _rows(data: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = data["results"]  # type: ignore[assignment]
    return rows


def _first_picture(url: str) -> Callable[[dict[str, object]], None]:
    def change(data: dict[str, object]) -> None:
        pictures: list[dict[str, object]] = data["pictures"]  # type: ignore[assignment]
        pictures[0]["url"] = url

    return change


def _code(call: Callable[[], object]) -> str:
    with pytest.raises(ParseError) as err:
        call()
    return err.value.code


def test_fixtures_are_recorded_without_token() -> None:
    for name in _FIXTURES:
        raw = gzip.decompress((FIXTURES_DIR / "mercadolibre" / f"{name}.json.gz").read_bytes())
        assert isinstance(json.loads(raw), dict)
        assert b"access_token" not in raw


def test_parse_search_returns_first_product_id() -> None:
    assert parse_search(_body("search-ean")) == "MCO39410080"
    assert parse_search(_body("search-empty")) is None
    other_site = _edit("search-ean", lambda d: _rows(d).insert(0, {"id": "MLA123"}))
    assert parse_search(other_site) == "MCO39410080"
    assert _code(lambda: parse_search(b"<html>")) == "bad_search"


def test_parse_sellers_orders_eligible_by_price() -> None:
    sellers = parse_sellers(_body("items"))
    prices = [s.price.minor for s in sellers]
    assert len(sellers) == 14
    assert prices == sorted(prices)
    assert all(s.price.currency is Currency.COP for s in sellers)
    assert sellers[0] == MeliSeller(
        297702699, Money(439900, Currency.COP), official=True, free_shipping=True
    )
    assert {s.seller_id for s in sellers if not s.official} == _NOT_OFFICIAL


def test_parse_sellers_skips_used_international_and_other_currency() -> None:
    def change(data: dict[str, object]) -> None:
        rows = _rows(data)
        rows[0]["condition"] = "used"
        rows[1]["international_delivery_mode"] = "CBT"
        rows[2]["currency_id"] = "USD"

    sellers = parse_sellers(_edit("items", change))
    assert len(sellers) == 11
    assert {s.seller_id for s in sellers}.isdisjoint({297702699, 205607103, 272545453})


def test_parse_sellers_returns_nothing_when_no_seller_is_eligible() -> None:
    def change(data: dict[str, object]) -> None:
        for row in _rows(data):
            row["condition"] = "used"

    assert parse_sellers(_edit("items", change)) == []


def test_parse_sellers_skips_an_invalid_entry() -> None:
    def change(data: dict[str, object]) -> None:
        rows = _rows(data)
        del rows[0]["seller_id"]
        rows[1]["price"] = "457424"

    sellers = parse_sellers(_edit("items", change))
    assert len(sellers) == 12
    assert 205607103 not in {s.seller_id for s in sellers}
    assert sellers[0].price == Money(485000, Currency.COP)


def test_parse_sellers_skips_zero_and_absurd_prices() -> None:
    def change(data: dict[str, object]) -> None:
        rows = _rows(data)
        rows[0]["price"] = 0
        rows[1]["price"] = 1e30

    sellers = parse_sellers(_edit("items", change))
    assert len(sellers) == 12
    assert {s.seller_id for s in sellers}.isdisjoint({297702699, 205607103})


def test_parse_sellers_rounds_decimal_prices() -> None:
    def change(data: dict[str, object]) -> None:
        _rows(data)[1]["price"] = 457424.5

    sellers = parse_sellers(_edit("items", change))
    assert next(s for s in sellers if s.seller_id == 205607103).price == Money(457425, Currency.COP)


def test_parse_sellers_rejects_a_body_without_results() -> None:
    assert _code(lambda: parse_sellers(b"{}")) == "bad_items"
    assert _code(lambda: parse_sellers(b"<html>")) == "bad_items"


def test_parse_sellers_rejects_when_no_entry_validates() -> None:
    def change(data: dict[str, object]) -> None:
        for row in _rows(data):
            row["price"] = "439900"

    assert _code(lambda: parse_sellers(_edit("items", change))) == "bad_items"


def test_parse_product_reads_name_and_first_cdn_picture() -> None:
    product = parse_product(_body("product"))
    assert product.name.startswith("Taladro/Atornillador")
    assert product.image_url == (
        "https://http2.mlstatic.com/D_NQ_NP_939054-MLA100928655228_122025-F.jpg"
    )
    for url in ("https://evil.example/x.jpg", "http://http2.mlstatic.com/x.jpg"):
        assert parse_product(_edit("product", _first_picture(url))).image_url is None
    assert _code(lambda: parse_product(_edit("product", lambda d: d.pop("name")))) == "bad_product"
    assert _code(lambda: parse_product(_edit("product", lambda d: d.update(name=" ")))) == (
        "bad_product"
    )


def test_parse_leader_only_platinum_or_gold_in_green() -> None:
    def user(status: str | None, level: str | None) -> bytes:
        return _edit(
            "user",
            lambda d: d["seller_reputation"].update(  # type: ignore[attr-defined]
                power_seller_status=status, level_id=level
            ),
        )

    assert parse_leader(user("platinum", "5_green")) == "platinum"
    assert parse_leader(user("gold", "5_green")) == "gold"
    for status, level in (("silver", "5_green"), ("platinum", "4_light_green"), (None, None)):
        assert parse_leader(user(status, level)) is None
    assert parse_leader(_body("user")) is None
    assert _code(lambda: parse_leader(b"<html>")) == "bad_user"
