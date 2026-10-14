"""Adaptador Newegg contra fixtures (spec §4.2 «Newegg» y §4.3).

Las fichas son reales (2026-10-14, grabadas desde la red de David tras seguir el 301 de
`/p/<item>`); en ambas el buy box es de un vendedor externo. La búsqueda sigue SINTÉTICA (ver su
`.meta.json`): Newegg responde 403 con su CAPTCHA a la nube de la sesión y también a la red de
David.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.model import ExtractionPath, ListingRef, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, FakeHttp, HttpResponse
from discountz.sources.base import ListingFound, NoCredentials, SourceContext
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.newegg import (
    NeweggSource,
    parse_newegg_detail,
    parse_newegg_independent,
    parse_newegg_search,
)
from discountz.sources.registry import load_all

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
FICHAS = ["N82E16820147861", "N82E16814137771"]


def expected(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "newegg" / f"{name}.expected.json").read_text()
    )
    return data


def body(name: str) -> bytes:
    return load_fixture("newegg", name).body


def usd(minor: int | None) -> Money | None:
    return None if minor is None else Money(minor, Currency.USD)


@pytest.mark.parametrize("item", FICHAS)
def test_detail_fixture_matches_expected(item: str) -> None:
    exp = expected(f"ficha_{item}")
    o = parse_newegg_detail(body(f"ficha_{item}"), item, T)
    assert o is not None
    assert o.ref == ListingRef("newegg", exp["external_id"])
    assert o.title == exp["title"]
    assert o.price == usd(exp["price"])
    assert o.list_price == usd(exp["list_price"])
    assert o.in_stock is exp["in_stock"]
    assert o.seller_class == SellerClass(exp["seller_class"])
    assert o.unit_key == exp["unit_key"]
    assert o.url_path == exp["url_path"]
    assert o.extraction is ExtractionPath.EMBEDDED_JSON


@pytest.mark.parametrize("item", FICHAS)
def test_independent_route_matches_main_route(item: str) -> None:
    """`__initialState__` y el precio del buy box coinciden en todas las fichas."""
    exp = expected(f"ficha_{item}")
    main = parse_newegg_detail(body(f"ficha_{item}"), item, T)
    o = parse_newegg_independent(body(f"ficha_{item}"), item, T)
    assert main is not None and o is not None
    assert o.price == usd(exp["independent_price"]) == main.price
    assert o.in_stock is main.in_stock
    assert o.seller_class == main.seller_class
    assert o.extraction is ExtractionPath.HTML


def test_search_skips_sponsored_and_duplicates() -> None:
    exp = expected("busqueda_ssd")
    offers = parse_newegg_search(body("busqueda_ssd"), T)
    assert [o.ref.external_id for o in offers] == exp["organic_ids"]
    assert [o.seller_class.value for o in offers] == exp["seller_classes"]
    first = offers[0]
    assert first.price == usd(exp["first"]["price"])
    assert first.list_price == usd(exp["first"]["list_price"])
    assert first.in_stock
    (oos,) = [o for o in offers if o.ref.external_id == exp["out_of_stock_id"]]
    assert oos.price is None and not oos.in_stock


def _state(final_price: str, seller: str, instock: str = "true") -> bytes:
    detail = (
        '{"Item":"N82E16826104567","FinalPrice":PRICE,"Instock":STOCK,'
        '"Seller":{"SellerName":"SELLER"},"Description":{"Title":"Logitech Mouse"}}'
    )
    detail = detail.replace("PRICE", final_price).replace("SELLER", seller)
    detail = detail.replace("STOCK", instock)
    return f'<script defer="">window.__initialState__ = {{"ItemDetail":{detail}}}</script>'.encode()


def test_detail_without_initial_state_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_newegg_detail(b"<html><body><h1>hola</h1></body></html>", "N82E16820147861", T)


def test_detail_with_malformed_price_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_newegg_detail(_state('"1,299.99"', "Newegg"), "N82E16826104567", T)


def test_detail_sold_by_newegg_is_first_party() -> None:
    o = parse_newegg_detail(_state("24.5", "Newegg"), "N82E16826104567", T)
    assert o is not None and o.price == usd(2450) and o.seller_class is SellerClass.FIRST_PARTY
    assert o.title == "Logitech Mouse" and o.in_stock


def test_detail_out_of_stock_has_no_price() -> None:
    o = parse_newegg_detail(_state("24.5", "Amigos", "false"), "N82E16826104567", T)
    assert o is not None and o.price is None and not o.in_stock
    assert o.seller_class is SellerClass.MARKETPLACE


def test_independent_without_buy_box_price_is_none() -> None:
    html = b"""<h1 class="product-title">SSD</h1><div class="combo-section">
      <div class="price-current">$<strong>9</strong><sup>.99</sup></div></div>"""
    assert parse_newegg_independent(html, "N82E16820147861", T) is None


def test_parse_url_variants() -> None:
    src = NeweggSource()
    hosts = src.descriptor.hosts

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, hosts))

    assert parse(
        "https://www.newegg.com/samsung-990-pro/p/N82E16820147861?Item=N82E16820147861"
    ) == (ListingRef("newegg", "N82E16820147861"))
    assert parse("https://www.newegg.com/p/9SIA7S6K0E6253") == ListingRef(
        "newegg", "9SIA7S6K0E6253"
    )
    assert parse("https://newegg.com/p/N82E16820147861") == ListingRef("newegg", "N82E16820147861")
    assert parse("https://www.newegg.com/p/pl?d=ssd") is None
    assert parse("https://www.newegg.com/p/1Z4-009F-001H8") is None


def test_fetch_listings_reads_canonical_url() -> None:
    url = "https://www.newegg.com/p/N82E16820147861"
    fake = FakeHttp({url: load_fixture("newegg", "ficha_N82E16820147861")})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = NeweggSource().fetch_listings(ctx, [ListingRef("newegg", "N82E16820147861")])
    assert isinstance(out, ListingFound)
    assert out.offer.price == usd(39999)


def test_fetch_follows_one_redirect_to_the_slug_url() -> None:
    """`/p/<item>` responde 301 sin cuerpo hacia `/<slug>/p/<item>` (medido el 2026-10-14)."""
    short = "https://www.newegg.com/p/N82E16820147861"
    target = "https://www.newegg.com/samsung-990-evo/p/N82E16820147861"
    fake = FakeHttp(
        {
            short: HttpResponse(301, short, {"Location": target}, b"", 1),
            target: load_fixture("newegg", "ficha_N82E16820147861"),
        }
    )
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = NeweggSource().fetch_listings(ctx, [ListingRef("newegg", "N82E16820147861")])
    assert isinstance(out, ListingFound)
    assert out.offer.price == usd(39999)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/samsung/p/N82E16820147861",
        "http://www.newegg.com/samsung/p/N82E16820147861",
        "https://www.newegg.com/samsung/p/N82E16814137771",
        "https://www.newegg.com/areyouahuman?itn=1",
    ],
)
def test_fetch_rejects_redirects_off_the_same_item(location: str) -> None:
    short = "https://www.newegg.com/p/N82E16820147861"
    fake = FakeHttp({short: HttpResponse(301, short, {"location": location}, b"", 1)})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = NeweggSource().fetch_listings(ctx, [ListingRef("newegg", "N82E16820147861")])
    assert not isinstance(out, ListingFound)
    assert len(fake.calls) == 1


def test_confirm_reuses_the_detail_body() -> None:
    url = "https://www.newegg.com/p/N82E16820147861"
    fake = FakeHttp({url: load_fixture("newegg", "ficha_N82E16820147861")})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    src = NeweggSource()
    (out,) = src.fetch_listings(ctx, [ListingRef("newegg", "N82E16820147861")])
    assert isinstance(out, ListingFound)
    confirmed = src.confirm(ctx, out.offer)
    assert confirmed is not None and confirmed.price == usd(39999)
    assert len(fake.calls) == 1


def test_detect_block_captcha_page() -> None:
    src = NeweggSource()
    captcha = HttpResponse(
        403, "https://www.newegg.com/p/N82E16820147861", {}, b"<title>CAPTCHA page</title>", 1
    )
    assert src.detect_block(captcha) is BlockKind.HTTP_403
    soft = HttpResponse(
        200, "https://www.newegg.com/areyouahuman?itn=1", {}, b"<title>Are you a human?</title>", 1
    )
    assert src.detect_block(soft) is BlockKind.CAPTCHA
    assert src.detect_block(load_fixture("newegg", "ficha_N82E16820147861")) is None


def test_registered() -> None:
    assert "newegg" in load_all()
