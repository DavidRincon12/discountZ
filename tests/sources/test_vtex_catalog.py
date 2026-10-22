"""Recorrido del catálogo VTEX: particiones, páginas y datos de tarjeta (spec del rastreo §2.1)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from discountz.domain.errors import ParseError, Unavailable
from discountz.domain.model import ListingRef, SellerClass
from discountz.domain.money import Currency, Money
from discountz.infra.http import FakeHttp, Files, HttpResponse, Method, Params
from discountz.sources.base import (
    Capability,
    CatalogPage,
    CatalogPartition,
    ListingFound,
    NoCredentials,
    SourceContext,
)
from discountz.sources.fixtures import load_fixture
from discountz.sources.vtex import CATALOG_CAP, STORES, VtexSource, card_image

T = datetime(2026, 10, 16, 12, tzinfo=UTC)
SEARCH = "catalog_system/pub/products/search"
_BAND = re.compile(r"^P:\[(\d+) TO (\d+)\]$")


def ctx(http: object) -> SourceContext:
    return SourceContext(http, NoCredentials(), 1e9, T)  # type: ignore[arg-type]


def resp(body: object, *, total: int, start: int = 0, end: int = 0, url: str = "x") -> HttpResponse:
    headers = {"resources": f"{start}-{end}/{total}"}
    return HttpResponse(206, url, headers, json.dumps(body).encode(), 0)


def product(
    sku: str, sellers: list[tuple[str, bool, float, int]], *, ean: str | None = "7701234567890"
) -> dict[str, object]:
    return {
        "productId": "9" + sku,
        "productName": f"Producto {sku}",
        "linkText": f"producto-{sku}",
        "categories": [
            "/Tecnología/Celulares/Smartphones/",
            "/Tecnología/Celulares/",
            "/Tecnología/",
        ],
        "items": [
            {
                "itemId": sku,
                "ean": ean,
                "images": [
                    {"imageUrl": f"https://exitocol.vteximg.com.br/arquivos/ids/{sku}/a.jpg"}
                ],
                "sellers": [
                    {
                        "sellerId": sid,
                        "sellerDefault": default,
                        "commertialOffer": {
                            "Price": price,
                            "ListPrice": price,
                            "PriceWithoutDiscount": price,
                            "AvailableQuantity": qty,
                        },
                    }
                    for sid, default, price, qty in sellers
                ],
            }
        ],
    }


@pytest.mark.parametrize("code", list(STORES))
def test_catalog_page_parses_real_fixture(code: str) -> None:
    source = VtexSource(STORES[code])
    fixture = load_fixture("vtex", f"{code}-catalog")
    fake = _Router(
        lambda q: HttpResponse(206, fixture.url, {"resources": "0-49/10"}, fixture.body, 0)
    )
    page = source.fetch_catalog_page(ctx(fake), "C:/1/", 0)
    assert len(page.items) > 0
    assert not page.has_more
    assert page.split == ()
    for obs in page.items:
        assert obs.price.currency is Currency.COP
        assert obs.price.minor > 0
        assert obs.category_path.startswith("/")
        assert obs.ean is None or obs.ean.isdigit()


def test_pepeganga_marketplace_product_is_marked_as_marketplace() -> None:
    # SKU real con vendedor `homesale` con stock y la tienda (vendedor 1) sin stock.
    fixture = load_fixture("vtex", "pepeganga-catalog")
    fake = _Router(
        lambda q: HttpResponse(206, fixture.url, {"resources": "0-49/10"}, fixture.body, 0)
    )
    page = VtexSource(STORES["pepeganga"]).fetch_catalog_page(ctx(fake), "C:/1/", 0)
    by_id = {o.external_id: o for o in page.items}
    assert by_id["289855"].seller_class is SellerClass.MARKETPLACE
    assert by_id["289855"].price == Money(194_900, Currency.COP)


def test_catalog_page_takes_cheapest_seller_in_stock() -> None:
    body = [
        product(
            "101", [("1", True, 0.0, 0), ("ab12", False, 59900.0, 3), ("cd34", False, 49900.0, 0)]
        ),
        product("102", [("1", True, 0.0, 0)]),  # sin vendedor con stock ⇒ se omite
        product("103", [("1", True, 89900.0, 5), ("ab12", False, 99900.0, 5)], ean="12"),
    ]
    fake = _Router(lambda q: resp(body, total=3, end=2))
    page = VtexSource(STORES["exito"]).fetch_catalog_page(ctx(fake), "C:/1/", 0)
    by_id = {o.external_id: o for o in page.items}
    assert set(by_id) == {"101", "103"}
    assert by_id["101"].price == Money(59_900, Currency.COP)
    assert by_id["101"].seller_class is SellerClass.MARKETPLACE
    assert by_id["101"].category_path == "/Tecnología/Celulares/Smartphones/"
    assert by_id["101"].ean == "7701234567890"
    assert by_id["103"].seller_class is SellerClass.FIRST_PARTY
    assert by_id["103"].ean is None  # EAN fuera de 6-14 dígitos


def test_catalog_has_more_from_resources_header() -> None:
    source = VtexSource(STORES["exito"])
    fake = _Router(lambda q: resp([], total=120, start=int(q["_from"]), end=int(q["_to"])))
    assert source.fetch_catalog_page(ctx(fake), "C:/1/", 0).has_more
    assert source.fetch_catalog_page(ctx(fake), "C:/1/", 1).has_more
    assert not source.fetch_catalog_page(ctx(fake), "C:/1/", 2).has_more
    first = dict(parse_qsl(urlsplit(fake.urls[0]).query))
    assert (first["_from"], first["_to"]) == ("0", "49")
    assert "fq=isAvailablePerSalesChannel_1%3A1" in fake.urls[0]


def test_catalog_stops_at_vtex_cap() -> None:
    fake = _Router(lambda q: resp([], total=2_600, start=int(q["_from"]), end=int(q["_to"])))
    page = VtexSource(STORES["exito"]).fetch_catalog_page(ctx(fake), "C:/1/|P:[5 TO 5]", 49)
    assert not page.has_more  # banda de un solo precio: no se puede partir más


def test_partitions_split_large_leaf_by_price() -> None:
    prices = [1_000 + 37 * i for i in range(6_000)]

    def total(query: Mapping[str, str], fqs: list[str]) -> HttpResponse:
        lo, hi = 0, 10**12
        for fq in fqs:
            m = _BAND.match(fq)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
        n = sum(1 for p in prices if lo <= p <= hi)
        return resp([], total=n, start=int(query["_from"]), end=int(query["_to"]))

    fake = _Router(lambda q: total(q, fake.last_fqs))
    source = VtexSource(STORES["exito"])
    pending, final = ["C:/1/2/"], []
    while pending:
        part = pending.pop()
        page = source.fetch_catalog_page(ctx(fake), part, 0)
        if page.split:
            assert page.items == ()
            pending.extend(page.split)
        else:
            final.append(part)
    counts = []
    for part in final:
        assert part.startswith("C:/1/2/|P:[")
        page = source.fetch_catalog_page(ctx(fake), part, 0)
        counts.append(int(fake.last_total))
        assert page.split == ()
    assert all(c <= CATALOG_CAP for c in counts)
    assert sum(counts) == len(prices)


def test_catalog_partitions_are_root_categories_with_their_path() -> None:
    tree = load_fixture("vtex", "exito-category-tree")
    url = "https://www.exito.com/io/api/catalog_system/pub/category/tree/1"
    fake = FakeHttp({url: tree})
    parts = VtexSource(STORES["exito"]).catalog_partitions(ctx(fake))
    assert CatalogPartition("C:/34185084/", "/Tecnología/") in parts
    assert CatalogPartition("C:/34185082/", "/Mercado/") in parts
    assert len(parts) == len(json.loads(tree.body))


def test_card_facts_from_batch() -> None:
    body = [product("101", [("1", True, 59900.0, 3)]), product("103", [("1", True, 1000.0, 1)])]
    fake = _Router(lambda q: resp(body, total=2, end=1))
    facts = VtexSource(STORES["exito"]).fetch_card_facts(ctx(fake), ["101", "103", "999"])
    assert set(facts) == {"101", "103"}
    f = facts["101"]
    assert f.title == "Producto 101"
    assert f.url == "https://www.exito.com/producto-101/p"
    # Se pide la variante de 1000 px: el original de un vendedor puede pasar de 5 MB.
    assert f.image_url == "https://exitocol.vteximg.com.br/arquivos/ids/101-1000-1000/a.jpg"
    assert "fq=skuId%3A101&fq=skuId%3A103&fq=skuId%3A999" in fake.urls[0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://exitocol.vteximg.com.br/arquivos/ids/35184443/a.jpg?v=6392",
            "https://exitocol.vteximg.com.br/arquivos/ids/35184443-1000-1000/a.jpg?v=6392",
        ),
        (
            "https://exitocol.vteximg.com.br/arquivos/ids/7-500-auto/a.jpg",
            "https://exitocol.vteximg.com.br/arquivos/ids/7-1000-1000/a.jpg",
        ),
        (  # Olímpica publica algunos hosts con punto final
            "https://olimpica.vteximg.com.br./arquivos/ids/9/a.jpg",
            "https://olimpica.vteximg.com.br/arquivos/ids/9-1000-1000/a.jpg",
        ),
        (
            "https://exitocol.vteximg.com.br/otra/ruta.jpg",
            "https://exitocol.vteximg.com.br/otra/ruta.jpg",
        ),
    ],
)
def test_card_image_asks_for_sized_variant(raw: str, expected: str) -> None:
    assert card_image(raw) == expected


def test_descriptor_declares_catalog_and_image_hosts() -> None:
    d = VtexSource(STORES["carulla"]).descriptor
    assert Capability.CATALOG in d.capabilities
    assert d.image_hosts == {"carulla.vteximg.com.br", "carulla.vtexassets.com"}
    assert not d.supports_changed
    assert VtexSource(STORES["exito"]).descriptor.image_hosts == {
        "exitocol.vteximg.com.br",
        "exitocol.vtexassets.com",
    }


def test_catalog_page_rejects_bad_partition() -> None:
    with pytest.raises(ParseError):
        VtexSource(STORES["exito"]).fetch_catalog_page(
            ctx(_Router(lambda q: resp([], total=0))), "x", 0
        )


def test_catalog_page_type() -> None:
    assert CatalogPage((), has_more=False).split == ()


@dataclass
class _Router:
    """HttpClient falso que responde según los parámetros de la consulta."""

    handler: Callable[[dict[str, str]], HttpResponse]
    urls: list[str] = field(default_factory=list)
    last_fqs: list[str] = field(default_factory=list)
    last_total: str = "0"

    def request(
        self,
        method: Method,
        url: str,
        *,
        params: Params | None = None,
        json: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        files: Files | None = None,
    ) -> HttpResponse:
        assert url.endswith(SEARCH)
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        self.urls.append(f"{url}?{urlencode(pairs)}")
        self.last_fqs = [v for k, v in pairs if k == "fq"]
        r = self.handler(dict(pairs))
        self.last_total = r.headers.get("resources", "0/0").rsplit("/", 1)[1]
        return r


def test_catalog_items_by_sku_batch() -> None:
    body = [product("101", [("1", True, 59900.0, 3)]), product("102", [("1", True, 0.0, 0)])]
    fake = _Router(lambda q: resp(body, total=2, end=1))
    items = VtexSource(STORES["exito"]).fetch_catalog_items(ctx(fake), ["101", "102", "x"])
    assert [o.external_id for o in items] == ["101"]
    assert items[0].category_path == "/Tecnología/Celulares/Smartphones/"
    assert "fq=skuId%3A101&fq=skuId%3A102" in fake.urls[0]
    assert "skuId%3Ax" not in fake.urls[0]


def test_sku_batch_over_ten_asks_for_full_page() -> None:
    """VTEX devuelve 10 productos si no se pide rango (medido en Éxito el 2026-10-17)."""
    ids = [str(1000 + i) for i in range(12)]
    body = [product(i, [("1", True, 59900.0, 3)]) for i in ids]
    source = VtexSource(STORES["exito"])
    refs = [ListingRef("exito", i) for i in ids]
    for call in (
        lambda c: source.fetch_listings(c, refs),
        lambda c: source.fetch_catalog_listings(c, refs),
        lambda c: source.fetch_catalog_items(c, ids),
        lambda c: source.fetch_card_facts(c, ids),
    ):
        fake = _Router(lambda q: resp(body, total=12, end=11))
        call(ctx(fake))
        q = dict(parse_qsl(urlsplit(fake.urls[0]).query))
        assert (q["_from"], q["_to"]) == ("0", "11")
    small = _Router(lambda q: resp(body[:2], total=2, end=1))
    source.fetch_listings(ctx(small), refs[:2])
    assert "_to" not in small.urls[0]  # lotes de hasta 10: misma URL que en la v1


def test_catalog_page_without_resources_header_is_parse_error() -> None:
    fake = _Router(lambda q: HttpResponse(200, "x", {}, b"[]", 0))
    with pytest.raises(ParseError):
        VtexSource(STORES["exito"]).fetch_catalog_page(ctx(fake), "C:/1/", 0)


def test_catalog_listings_take_cheapest_seller_in_stock() -> None:
    body = [product("101", [("1", True, 0.0, 0), ("ab12", False, 59900.0, 3)])]
    fake = _Router(lambda q: resp(body, total=1))
    source = VtexSource(STORES["exito"])
    (default,) = source.fetch_listings(ctx(fake), [ListingRef("exito", "101")])
    (cheapest,) = source.fetch_catalog_listings(ctx(fake), [ListingRef("exito", "101")])
    assert isinstance(default, ListingFound) and not default.offer.in_stock
    assert isinstance(cheapest, ListingFound)
    assert cheapest.offer.in_stock
    assert cheapest.offer.price == Money(59_900, Currency.COP)
    assert cheapest.offer.seller_class is SellerClass.MARKETPLACE


def _heavy(limit: int, total: int) -> Callable[[dict[str, str]], HttpResponse]:
    """Catálogo cuyas páginas de más de `limit` productos no caben en el tope de tamaño."""

    def handler(q: dict[str, str]) -> HttpResponse:
        start, end = int(q["_from"]), int(q["_to"])
        if end - start + 1 > limit:
            raise Unavailable("exito", "too_large", 206)
        body = [product(str(1000 + i), [("1", True, 59900.0, 3)]) for i in range(start, end + 1)]
        return resp(body, total=total, start=start, end=end)

    return handler


def test_catalog_page_too_large_is_read_in_halves() -> None:
    """Moda y accesorios de Éxito pesó 42,8 MB por página de 50 el 2026-10-17 (tope: 24 MB)."""
    fake = _Router(_heavy(limit=25, total=120))
    page = VtexSource(STORES["exito"]).fetch_catalog_page(ctx(fake), "C:/1/", 1)
    assert [o.external_id for o in page.items] == [str(1000 + i) for i in range(50, 100)]
    assert page.has_more
    ranges = [dict(parse_qsl(urlsplit(u).query)) for u in fake.urls]
    assert [(r["_from"], r["_to"]) for r in ranges] == [("50", "99"), ("50", "74"), ("75", "99")]


def test_catalog_page_halves_until_it_fits() -> None:
    fake = _Router(_heavy(limit=12, total=50))
    page = VtexSource(STORES["exito"]).fetch_catalog_page(ctx(fake), "C:/1/", 0)
    assert len(page.items) == 50
    assert not page.has_more


def test_single_product_too_large_is_still_an_error() -> None:
    fake = _Router(_heavy(limit=0, total=50))
    with pytest.raises(Unavailable):
        VtexSource(STORES["exito"]).fetch_catalog_page(ctx(fake), "C:/1/", 0)


def test_sku_batch_too_large_is_read_in_halves() -> None:
    ids = [str(1000 + i) for i in range(50)]

    def handler(q: dict[str, str]) -> HttpResponse:
        start, end = int(q["_from"]), int(q["_to"])
        if end - start + 1 > 25:
            raise Unavailable("exito", "too_large", 206)
        asked = fake.last_fqs
        body = [product(f.removeprefix("skuId:"), [("1", True, 59900.0, 3)]) for f in asked]
        return resp(body, total=len(body), end=len(body) - 1)

    fake = _Router(handler)
    items = VtexSource(STORES["exito"]).fetch_catalog_items(ctx(fake), ids)
    assert sorted(o.external_id for o in items) == ids
    assert len(fake.urls) == 3
    facts = VtexSource(STORES["exito"]).fetch_card_facts(ctx(fake), ids)
    assert sorted(facts) == ids


def test_too_large_first_page_of_a_partition_to_split_stops_at_the_total() -> None:
    """Para partir por precio basta el total de la primera mitad que cabe."""
    fake = _Router(_heavy(limit=12, total=40_000))
    page = VtexSource(STORES["exito"]).fetch_catalog_page(ctx(fake), "C:/1/", 0)
    assert page.split
    ranges = [dict(parse_qsl(urlsplit(u).query)) for u in fake.urls]
    assert [(r["_from"], r["_to"]) for r in ranges] == [
        ("0", "49"),
        ("0", "24"),
        ("0", "12"),
        ("0", "6"),
    ]
