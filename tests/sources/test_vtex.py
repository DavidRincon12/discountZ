from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.model import ListingRef, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.sources.base import (
    ListingFailed,
    ListingFound,
    ListingGone,
    NeedsResolution,
    NoCredentials,
    SourceContext,
)
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.vtex import (
    STORES,
    VtexSource,
    parse_vtex_products,
    parse_vtex_simulation,
)

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
FICHAS = [
    ("exito", "102058783"),
    ("carulla", "715872"),
    ("olimpica", "1100253933"),
    ("panamericana", "659232"),
    ("jumbo", "436375"),
    ("pepeganga", "325550"),
]


def expected(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES_DIR / "vtex" / f"{name}.expected.json").read_text())
    return data


def raw(name: str) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(load_fixture("vtex", name).body)
    return data


def body(data: object) -> bytes:
    return json.dumps(data).encode()


@pytest.mark.parametrize(("code", "sku"), FICHAS)
def test_detail_fixture_matches_expected(code: str, sku: str) -> None:
    name = f"{code}_ficha_{sku}"
    exp = expected(name)
    offers = [
        o
        for o in parse_vtex_products(load_fixture("vtex", name).body, STORES[code], T)
        if o.ref.external_id == sku
    ]
    assert len(offers) == 1
    o = offers[0]
    assert o.ref == ListingRef(code, exp["external_id"])
    assert o.parent_external_id == exp["parent_external_id"]
    assert o.title == exp["title"]
    assert o.price == Money(exp["price"], Currency.COP)
    assert o.list_price == Money(exp["list_price"], Currency.COP)
    assert o.in_stock is exp["in_stock"]
    assert o.seller_class == SellerClass(exp["seller_class"])
    assert o.unit_key == exp["unit_key"]
    assert o.url_path == exp["url_path"]


@pytest.mark.parametrize(("code", "sku"), FICHAS)
def test_simulation_fixture_matches_expected(code: str, sku: str) -> None:
    exp = expected(f"{code}_simulacion_{sku}")
    o = parse_vtex_simulation(
        load_fixture("vtex", f"{code}_simulacion_{sku}").body, STORES[code], sku, T
    )
    assert o is not None
    assert o.price == Money(exp["price"], Currency.COP)
    assert o.in_stock is exp["in_stock"]


@pytest.mark.parametrize(
    "name",
    [
        "exito_busqueda_ssd",
        "carulla_busqueda_cafe",
        "olimpica_busqueda_televisor",
        "panamericana_busqueda_portatil",
        "jumbo_busqueda_televisor",
        "pepeganga_busqueda_lego",
    ],
)
def test_search_fixtures_parse_every_product(name: str) -> None:
    code = name.split("_")[0]
    offers = parse_vtex_products(
        load_fixture("vtex", name).body, STORES[code], T, max_per_product=3
    )
    assert len(offers) >= len(raw(name))
    assert all(o.price is None or o.price.minor >= 1_000 for o in offers)


def _product_with_items(n: int) -> list[dict[str, Any]]:
    product = raw("panamericana_ficha_659232")[0]
    base = product["items"][0]
    items = []
    for i in range(n):
        item = json.loads(json.dumps(base))
        item["itemId"] = str(900 + i)
        items.append(item)
    product["items"] = items
    return [product]


def test_zero_price_means_no_price_and_out_of_stock() -> None:
    data = raw("panamericana_ficha_659232")
    offer = data[0]["items"][0]["sellers"][0]["commertialOffer"]
    offer["Price"], offer["AvailableQuantity"] = 0, 0
    o = parse_vtex_products(body(data), STORES["panamericana"], T)[0]
    assert o.price is None and o.in_stock is False


def test_list_price_is_max_of_list_and_price_without_discount() -> None:
    data = raw("panamericana_ficha_659232")
    offer = data[0]["items"][0]["sellers"][0]["commertialOffer"]
    offer["ListPrice"], offer["PriceWithoutDiscount"] = 150000, 199000
    o = parse_vtex_products(body(data), STORES["panamericana"], T)[0]
    assert o.list_price == Money(199_000, Currency.COP)


def test_non_default_or_non_1_seller_is_marketplace() -> None:
    o = parse_vtex_products(
        load_fixture("vtex", "olimpica_ficha_1100253933").body, STORES["olimpica"], T
    )
    assert {x.seller_class for x in o} == {SellerClass.MARKETPLACE}


def test_search_keeps_at_most_three_in_stock_items_per_product() -> None:
    offers = parse_vtex_products(
        body(_product_with_items(5)), STORES["panamericana"], T, max_per_product=3
    )
    assert len(offers) == 3
    assert len(parse_vtex_products(body(_product_with_items(5)), STORES["panamericana"], T)) == 5


def test_simulation_cents_are_divided_with_decimal() -> None:
    sim = {
        "items": [{"id": "1", "sellingPrice": 12345, "availability": "available", "seller": "1"}]
    }
    o = parse_vtex_simulation(body(sim), STORES["exito"], "1", T)
    assert o is not None and o.price == Money(123, Currency.COP)  # 123,45 → 123 (ROUND_HALF_UP)


def test_simulation_without_items_is_none() -> None:
    assert parse_vtex_simulation(body({"items": []}), STORES["exito"], "1", T) is None


def test_truncated_json_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_vtex_products(
            load_fixture("vtex", "exito_ficha_102058783").body[:500], STORES["exito"], T
        )
    with pytest.raises(ParseError):
        parse_vtex_products(body([{"productId": "1"}]), STORES["exito"], T)


def ctx(routes: dict[str, HttpResponse | list[HttpResponse]]) -> tuple[SourceContext, FakeHttp]:
    fake = FakeHttp(routes)
    return SourceContext(fake, NoCredentials(), 1e9, T), fake


def test_parse_url_variants() -> None:
    src = VtexSource(STORES["exito"])
    url = SafeUrl.parse(
        "https://www.exito.com/tv-55-samsung-123/p?skuId=987&utm_source=x", src.descriptor.hosts
    )
    assert src.parse_url(url) == NeedsResolution("exito", "slug:tv-55-samsung-123|sku:987")
    url = SafeUrl.parse("https://www.exito.com/tv-55-samsung-123/p", src.descriptor.hosts)
    assert src.parse_url(url) == NeedsResolution("exito", "slug:tv-55-samsung-123")
    assert src.parse_url(SafeUrl.parse("https://www.exito.com/ayuda", src.descriptor.hosts)) is None


def test_fetch_listings_batches_and_marks_missing_as_gone() -> None:
    store = STORES["exito"]
    ficha = load_fixture("vtex", "exito_ficha_102058783")
    url = f"{store.api_base}/catalog_system/pub/products/search?fq=skuId%3A102058783&fq=skuId%3A1"
    c, fake = ctx({url: ficha})
    out = VtexSource(store).fetch_listings(
        c, [ListingRef("exito", "102058783"), ListingRef("exito", "1")]
    )
    assert isinstance(out[0], ListingFound) and isinstance(out[1], ListingGone)
    assert len(fake.calls) == 1


def test_confirm_uses_simulation() -> None:
    store = STORES["exito"]
    offer = parse_vtex_products(load_fixture("vtex", "exito_ficha_102058783").body, store, T)[0]
    url = f"{store.api_base}/checkout/pub/orderForms/simulation?sc=1"
    detail = f"{store.api_base}/catalog_system/pub/products/search?fq=skuId%3A102058783"
    c, fake = ctx(
        {
            url: load_fixture("vtex", "exito_simulacion_102058783"),
            detail: load_fixture("vtex", "exito_ficha_102058783"),
        }
    )
    confirmed = VtexSource(store).confirm(c, offer)
    assert confirmed is not None and confirmed.price == offer.price
    sent = fake.calls[-1][2]
    assert sent == {
        "items": [{"id": "102058783", "quantity": 1, "seller": "14080"}],
        "country": "COL",
    }


def test_resolve_slug_with_sku() -> None:
    store = STORES["panamericana"]
    url = f"{store.api_base}/catalog_system/pub/products/search/datafono-bold-plus-659232/p"
    c, _ = ctx({url: load_fixture("vtex", "panamericana_ficha_659232")})
    ref = VtexSource(store).resolve(
        c, NeedsResolution("panamericana", "slug:datafono-bold-plus-659232")
    )
    assert ref == ListingRef("panamericana", "659232")


def test_registered_six_stores() -> None:
    from discountz.sources.registry import load_all

    assert set(STORES) <= set(load_all())
    assert set(STORES) == {"exito", "carulla", "olimpica", "panamericana", "jumbo", "pepeganga"}


def test_new_stores_point_to_their_vtex_accounts() -> None:
    assert (STORES["jumbo"].host, STORES["jumbo"].api_base, STORES["jumbo"].account) == (
        "www.jumbocolombia.com",
        "https://www.jumbocolombia.com/api",
        "jumbocolombiaio",
    )
    assert (STORES["pepeganga"].host, STORES["pepeganga"].account) == (
        "www.pepeganga.com",
        "pepeganga",
    )
    for code, sku in (("jumbo", "436375"), ("pepeganga", "325550")):
        product = raw(f"{code}_ficha_{sku}")[0]
        host = product["items"][0]["images"][0]["imageUrl"].split("/")[2]
        assert host in VtexSource(STORES[code]).descriptor.image_hosts


def test_expected_files_exist_for_every_detail() -> None:
    folder = Path(FIXTURES_DIR / "vtex")
    for code, sku in FICHAS:
        assert (folder / f"{code}_ficha_{sku}.expected.json").exists()
        assert gzip.decompress((folder / f"{code}_ficha_{sku}.json.gz").read_bytes())


def test_fetch_listings_keeps_parse_errors_as_parse_kind() -> None:
    store = STORES["exito"]
    url = f"{store.api_base}/catalog_system/pub/products/search?fq=skuId%3A1"
    c, _ = ctx({url: HttpResponse(200, url, {}, b"[{", 5)})
    out = VtexSource(store).fetch_listings(c, [ListingRef("exito", "1")])
    assert isinstance(out[0], ListingFailed)
    assert isinstance(out[0].error, ParseError)
