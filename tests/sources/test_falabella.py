"""Falabella por el JSON de sus listados filtrados por vendedor (spec de Falabella, ADR 0017)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from discountz.domain.errors import NotFound, ParseError
from discountz.domain.model import ListingRef, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, FakeHttp, HttpResponse
from discountz.sources.base import (
    Capability,
    CatalogPage,
    ListingFailed,
    ListingFound,
    ListingGone,
    NoCredentials,
    SourceContext,
)
from discountz.sources.falabella import (
    CATEGORIES,
    HOSTS,
    FalabellaSource,
    parse_category,
    parse_product,
)
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


# --- fuente --------------------------------------------------------------------------------

BASE = "https://www.falabella.com.co"
T = datetime(2026, 10, 24, 12, tzinfo=UTC)
LAPTOP = "73557831"
LAPTOP_PATH = f"/falabella-co/product/{LAPTOP}/port-14-ip-slim3-core5-16gb-512gb"
COMPUTERS = f"{BASE}/falabella-co/category/cat171006/Computadores"
FILTER = "f.derived.variant.sellerId=FALABELLA"


def ctx(http: object) -> SourceContext:
    return SourceContext(http, NoCredentials(), 1e9, T)  # type: ignore[arg-type]


def fx(name: str, status: int = 200) -> HttpResponse:
    got = load_fixture("falabella", name)
    return HttpResponse(status, got.url, {}, got.body, 0)


def routes(**extra: HttpResponse) -> FakeHttp:
    table: dict[str, HttpResponse | list[HttpResponse]] = {
        f"{COMPUTERS}?{FILTER}&page=1": fx("category-page"),
        f"{COMPUTERS}?{FILTER}&page=5": fx("category-last"),
        BASE + LAPTOP_PATH: fx("product"),
        **extra,
    }
    return FakeHttp(table)


def test_descriptor_matches_the_spec() -> None:
    d = FalabellaSource().descriptor
    assert (d.code, d.kind, d.currency) == ("falabella", "store", Currency.COP)
    assert d.hosts == frozenset({"www.falabella.com.co"})
    assert d.image_hosts == frozenset({"media.falabella.com.co"})
    assert d.capabilities == frozenset({Capability.DETAIL, Capability.CONFIRM, Capability.CATALOG})
    assert d.max_batch == 1


def test_partitions_are_the_fourteen_profile_categories() -> None:
    parts = FalabellaSource().catalog_partitions(ctx(FakeHttp({})))
    assert len(parts) == 14
    assert len({p.key for p in parts}) == 14
    computers = next(p for p in parts if p.key == "cat171006")
    assert computers.path == "/tecnologia/computadores/"
    assert all(p.path.startswith("/") and p.path.endswith("/") for p in parts)


def test_first_page_asks_for_the_store_seller_and_has_more_pages() -> None:
    http = routes()
    page = FalabellaSource().fetch_catalog_page(ctx(http), "cat171006", 0)
    assert [c[1] for c in http.calls] == [f"{COMPUTERS}?{FILTER}&page=1"]
    assert page.has_more is True  # 48 de 39.723 en la fixture sin filtro
    first = page.items[0]
    assert first.external_id == "73557834"
    assert first.price == Money(749900, Currency.COP)
    assert first.seller_class is SellerClass.FIRST_PARTY
    assert first.ean is None
    assert first.category_path == "/tecnologia/computadores/"
    assert first.title is not None


def test_last_page_has_no_more() -> None:
    http = routes()
    page = FalabellaSource().fetch_catalog_page(ctx(http), "cat171006", 4)
    assert [c[1] for c in http.calls] == [f"{COMPUTERS}?{FILTER}&page=5"]
    assert page.has_more is False  # 5 × 48 ≥ 219
    assert len(page.items) == 3


def test_a_filter_with_one_product_redirects_to_it_and_is_an_empty_page() -> None:
    url = f"{COMPUTERS}?{FILTER}&page=1"
    moved = HttpResponse(301, url, {"location": BASE + LAPTOP_PATH}, b"", 0)
    page = FalabellaSource().fetch_catalog_page(ctx(routes(**{url: moved})), "cat171006", 0)
    assert page == CatalogPage((), False)


def test_a_page_past_the_end_redirects_to_no_result_and_is_an_empty_page() -> None:
    url = f"{COMPUTERS}?{FILTER}&page=9"
    moved = HttpResponse(307, url, {"location": BASE + "/falabella-co/noResult?Ntt="}, b"", 0)
    page = FalabellaSource().fetch_catalog_page(ctx(routes(**{url: moved})), "cat171006", 8)
    assert page == CatalogPage((), False)


def test_television_category_uses_its_canonical_slug() -> None:
    tv = next(c for c in CATEGORIES if c.key == "cat1360967")
    assert tv.slug == "Televisores-y-Video"


def test_any_other_redirect_is_a_parse_error() -> None:
    url = f"{COMPUTERS}?{FILTER}&page=1"
    moved = HttpResponse(301, url, {"location": BASE + "/falabella-co/otra"}, b"", 0)
    with pytest.raises(ParseError) as exc:
        FalabellaSource().fetch_catalog_page(ctx(routes(**{url: moved})), "cat171006", 0)
    assert exc.value.code == "bad_redirect"


def test_unknown_partition_is_a_parse_error() -> None:
    with pytest.raises(ParseError) as exc:
        FalabellaSource().fetch_catalog_page(ctx(routes()), "cat999", 0)
    assert exc.value.code == "bad_partition"


def test_listing_confirmation_reads_the_cached_link() -> None:
    http = routes()
    src = FalabellaSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat171006", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("falabella", LAPTOP)])
    assert [x[1] for x in http.calls][-1] == BASE + LAPTOP_PATH
    assert isinstance(out[0], ListingFound)
    offer = out[0].offer
    assert offer.price == Money(2599900, Currency.COP)
    assert offer.in_stock is True
    assert offer.seller_class is SellerClass.FIRST_PARTY
    assert offer.list_price is None
    assert offer.url_path == LAPTOP_PATH


def test_listing_confirmation_without_a_cached_link_uses_the_bare_id() -> None:
    bare = f"{BASE}/falabella-co/product/{LAPTOP}"
    http = routes(**{bare: fx("product")})
    out = FalabellaSource().fetch_catalog_listings(ctx(http), [ListingRef("falabella", LAPTOP)])
    assert [x[1] for x in http.calls] == [bare]
    assert isinstance(out[0], ListingFound)
    assert out[0].offer.url_path == LAPTOP_PATH  # la ruta canónica sale de la ficha


def test_marketplace_listing_is_gone() -> None:
    http = routes(**{BASE + LAPTOP_PATH: fx("product-marketplace")})
    src = FalabellaSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat171006", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("falabella", LAPTOP)])
    assert isinstance(out[0], ListingGone)


def test_out_of_stock_listing_is_found_without_stock() -> None:
    http = routes(**{BASE + LAPTOP_PATH: fx("product-out-of-stock")})
    src = FalabellaSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat171006", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("falabella", LAPTOP)])
    assert isinstance(out[0], ListingFound)
    assert out[0].offer.in_stock is False


def test_unreadable_listing_is_a_failure_not_gone() -> None:
    http = routes(**{BASE + LAPTOP_PATH: HttpResponse(200, "x", {}, b"<html></html>", 0)})
    src = FalabellaSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat171006", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("falabella", LAPTOP)])
    assert isinstance(out[0], ListingFailed)
    assert isinstance(out[0].error, ParseError)


class Gone:
    def request(self, *args: object, **kwargs: object) -> HttpResponse:
        raise NotFound("falabella", "not_found", 404)


def test_a_404_listing_is_gone() -> None:
    out = FalabellaSource().fetch_catalog_listings(ctx(Gone()), [ListingRef("falabella", LAPTOP)])
    assert isinstance(out[0], ListingGone)


def test_card_facts_come_from_the_listing_page_already_read() -> None:
    http = routes()
    src = FalabellaSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat171006", 0)
    calls = len(http.calls)
    facts = src.fetch_card_facts(c, [LAPTOP, "123"])
    assert len(http.calls) == calls  # sin red
    assert set(facts) == {LAPTOP}
    assert facts[LAPTOP].url == BASE + LAPTOP_PATH
    assert (
        facts[LAPTOP].image_url == "https://media.falabella.com.co/falabellaCO/73557831_01/public"
    )
    assert facts[LAPTOP].title.startswith("Portátil")


def test_there_is_no_hot_tier_nor_changed_feed_nor_search() -> None:
    src = FalabellaSource()
    c = ctx(FakeHttp({}))
    assert src.fetch_catalog_items(c, ["1"]) == []
    assert src.fetch_changed(c, T, 0) == CatalogPage((), False)
    assert (
        src.search(c, SearchSpec("falabella", "taladro", (), None, None, "otros"), 0).offers == ()
    )
    assert src.fetch_leads(c) == []


def test_parse_url_reads_the_product_id_without_network() -> None:
    src = FalabellaSource()

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, HOSTS))

    assert parse(BASE + LAPTOP_PATH) == ListingRef("falabella", LAPTOP)
    assert parse(f"{BASE}/falabella-co/product/{LAPTOP}") == ListingRef("falabella", LAPTOP)
    assert parse(COMPUTERS) is None
    assert parse(BASE + "/falabella-co/search?Ntt=taladro") is None


def test_confirm_returns_the_offer_or_none() -> None:
    src = FalabellaSource()
    c = ctx(routes())
    src.fetch_catalog_page(c, "cat171006", 0)
    listed = src.fetch_catalog_listings(c, [ListingRef("falabella", LAPTOP)])[0]
    assert isinstance(listed, ListingFound)
    assert src.confirm(c, listed.offer) == listed.offer
    gone = FalabellaSource()
    c2 = ctx(routes(**{BASE + LAPTOP_PATH: fx("product-marketplace")}))
    gone.fetch_catalog_page(c2, "cat171006", 0)
    assert gone.confirm(c2, listed.offer) is None


def test_block_detection() -> None:
    src = FalabellaSource()
    assert (
        src.detect_block(HttpResponse(301, BASE + "/", {"location": BASE + "/x"}, b"", 0)) is None
    )
    assert src.detect_block(HttpResponse(403, BASE + "/", {}, b"", 0)) is BlockKind.HTTP_403
