"""Homecenter por el JSON de sus páginas de categoría (spec de Homecenter, ADR 0016)."""

from __future__ import annotations

import gzip
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
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.homecenter import HOSTS, HomecenterSource, parse_category, parse_product

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


# --- fuente --------------------------------------------------------------------------------

BASE = "https://www.homecenter.com.co"
T = datetime(2026, 10, 24, 12, tzinfo=UTC)
WELDER = "915074"
WELDER_SLUG = "soldador-inversor-160amp-mascara-fotosensible-bauker"
WELDER_PATH = f"/homecenter-co/product/{WELDER}/{WELDER_SLUG}/{WELDER}/"
BIG = f"{BASE}/homecenter-co/category/cat1670400/herramientas-electricas-e-inalambricas/"
SMALL = f"{BASE}/homecenter-co/category/cat300017/destornilladores-electricos-y-llaves-de-impacto/"


def ctx(http: object) -> SourceContext:
    return SourceContext(http, NoCredentials(), 1e9, T)  # type: ignore[arg-type]


def fx(name: str, status: int = 200) -> HttpResponse:
    got = load_fixture("homecenter", name)
    return HttpResponse(status, got.url, {}, got.body, 0)


def routes(**extra: HttpResponse) -> FakeHttp:
    table: dict[str, HttpResponse | list[HttpResponse]] = {
        BIG: fx("category-page"),
        f"{SMALL}?currentpage=5": fx("category-last"),
        BASE + WELDER_PATH: fx("product"),
        **extra,
    }
    return FakeHttp(table)


def test_descriptor_matches_the_spec() -> None:
    d = HomecenterSource().descriptor
    assert (d.code, d.kind, d.currency) == ("homecenter", "store", Currency.COP)
    assert d.hosts == frozenset({"www.homecenter.com.co"})
    assert d.image_hosts == frozenset({"media.falabella.com"})
    assert d.capabilities == frozenset({Capability.DETAIL, Capability.CONFIRM, Capability.CATALOG})
    assert d.max_batch == 1


def test_partitions_are_the_thirteen_profile_categories() -> None:
    parts = HomecenterSource().catalog_partitions(ctx(FakeHttp({})))
    assert len(parts) == 13
    big = next(p for p in parts if p.key == "cat1670400")
    assert big.path == "/herramientas y ferreteria/herramientas electricas e inalambricas/"
    assert len({p.key for p in parts}) == 13


def test_first_page_has_no_query_and_more_pages() -> None:
    http = routes()
    page = HomecenterSource().fetch_catalog_page(ctx(http), "cat1670400", 0)
    assert [c[1] for c in http.calls] == [BIG]
    assert page.has_more is True
    first = page.items[0]
    assert first.external_id == "877508"
    assert first.price == Money(244900, Currency.COP)
    assert first.seller_class is SellerClass.FIRST_PARTY
    assert first.ean is None
    assert (
        first.category_path == "/herramientas y ferreteria/herramientas electricas e inalambricas/"
    )
    assert first.title is not None


def test_last_page_has_no_more() -> None:
    http = routes()
    page = HomecenterSource().fetch_catalog_page(ctx(http), "cat300017", 4)
    assert [c[1] for c in http.calls] == [f"{SMALL}?currentpage=5"]
    assert page.has_more is False
    assert len(page.items) == 3


def test_unknown_partition_is_a_parse_error() -> None:
    with pytest.raises(ParseError) as exc:
        HomecenterSource().fetch_catalog_page(ctx(routes()), "cat999", 0)
    assert exc.value.code == "bad_partition"


def test_listing_confirmation_reads_the_cached_link() -> None:
    http = routes()
    src = HomecenterSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat1670400", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("homecenter", WELDER)])
    assert [x[1] for x in http.calls][-1] == BASE + WELDER_PATH
    assert isinstance(out[0], ListingFound)
    offer = out[0].offer
    assert offer.price == Money(499900, Currency.COP)
    assert offer.in_stock is True
    assert offer.seller_class is SellerClass.FIRST_PARTY
    assert offer.list_price is None
    assert offer.url_path == WELDER_PATH


def test_listing_confirmation_follows_the_canonical_redirect_once() -> None:
    bare = f"{BASE}/homecenter-co/product/{WELDER}/x/{WELDER}/"
    moved = HttpResponse(301, bare, {"location": BASE + WELDER_PATH}, b"", 0)
    http = routes(**{bare: moved})
    out = HomecenterSource().fetch_catalog_listings(ctx(http), [ListingRef("homecenter", WELDER)])
    assert [x[1] for x in http.calls] == [bare, BASE + WELDER_PATH]
    assert isinstance(out[0], ListingFound)


def test_redirect_to_a_foreign_host_is_a_failure() -> None:
    bare = f"{BASE}/homecenter-co/product/{WELDER}/x/{WELDER}/"
    moved = HttpResponse(301, bare, {"location": "https://evil.example/x"}, b"", 0)
    out = HomecenterSource().fetch_catalog_listings(
        ctx(routes(**{bare: moved})), [ListingRef("homecenter", WELDER)]
    )
    assert isinstance(out[0], ListingFailed)


def test_marketplace_listing_is_gone() -> None:
    http = routes(**{BASE + WELDER_PATH: fx("product-marketplace")})
    src = HomecenterSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat1670400", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("homecenter", WELDER)])
    assert isinstance(out[0], ListingGone)


def test_out_of_stock_listing_is_found_without_stock() -> None:
    http = routes(**{BASE + WELDER_PATH: fx("product-out-of-stock")})
    src = HomecenterSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat1670400", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("homecenter", WELDER)])
    assert isinstance(out[0], ListingFound)
    assert out[0].offer.in_stock is False


def test_unreadable_listing_is_a_failure_not_gone() -> None:
    http = routes(**{BASE + WELDER_PATH: HttpResponse(200, "x", {}, b"<html></html>", 0)})
    src = HomecenterSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat1670400", 0)
    out = src.fetch_catalog_listings(c, [ListingRef("homecenter", WELDER)])
    assert isinstance(out[0], ListingFailed)
    assert isinstance(out[0].error, ParseError)


class Gone:
    def request(self, *args: object, **kwargs: object) -> HttpResponse:
        raise NotFound("homecenter", "not_found", 404)


def test_a_404_listing_is_gone() -> None:
    out = HomecenterSource().fetch_catalog_listings(ctx(Gone()), [ListingRef("homecenter", WELDER)])
    assert isinstance(out[0], ListingGone)


def test_card_facts_come_from_the_listing_page_already_read() -> None:
    http = routes()
    src = HomecenterSource()
    c = ctx(http)
    src.fetch_catalog_page(c, "cat1670400", 0)
    calls = len(http.calls)
    facts = src.fetch_card_facts(c, [WELDER, "123"])
    assert len(http.calls) == calls  # sin red
    assert set(facts) == {WELDER}
    assert facts[WELDER].url == BASE + WELDER_PATH
    assert facts[WELDER].image_url == "https://media.falabella.com/sodimacCO/915074/public"
    assert facts[WELDER].title.startswith("Soldador Inversor")


def test_there_is_no_hot_tier_nor_changed_feed_nor_search() -> None:
    src = HomecenterSource()
    c = ctx(FakeHttp({}))
    assert src.fetch_catalog_items(c, ["1"]) == []
    assert src.fetch_changed(c, T, 0) == CatalogPage((), False)
    assert (
        src.search(c, SearchSpec("homecenter", "taladro", (), None, None, "otros"), 0).offers == ()
    )
    assert src.fetch_leads(c) == []


def test_parse_url_reads_the_product_id_without_network() -> None:
    src = HomecenterSource()

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, HOSTS))

    assert parse(BASE + WELDER_PATH) == ListingRef("homecenter", WELDER)
    assert parse(f"{BASE}/homecenter-co/product/{WELDER}/x/{WELDER}/") == ListingRef(
        "homecenter", WELDER
    )
    assert parse(BIG) is None
    assert parse(BASE + "/homecenter-co/search/?Ntt=taladro") is None


def test_confirm_returns_the_offer_or_none() -> None:
    src = HomecenterSource()
    c = ctx(routes())
    src.fetch_catalog_page(c, "cat1670400", 0)
    listed = src.fetch_catalog_listings(c, [ListingRef("homecenter", WELDER)])[0]
    assert isinstance(listed, ListingFound)
    assert src.confirm(c, listed.offer) == listed.offer
    gone = HomecenterSource()
    c2 = ctx(routes(**{BASE + WELDER_PATH: fx("product-marketplace")}))
    gone.fetch_catalog_page(c2, "cat1670400", 0)
    assert gone.confirm(c2, listed.offer) is None


def test_block_detection_ignores_the_canonical_redirect() -> None:
    src = HomecenterSource()
    assert (
        src.detect_block(HttpResponse(301, BASE + "/", {"location": BASE + "/x/"}, b"", 0)) is None
    )
    assert src.detect_block(HttpResponse(403, BASE + "/", {}, b"", 0)) is BlockKind.HTTP_403
