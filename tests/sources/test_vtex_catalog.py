"""Recorrido del catálogo VTEX: particiones, páginas y datos de tarjeta (spec del rastreo §2.1)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from discountz.domain.errors import ParseError
from discountz.domain.model import SellerClass
from discountz.domain.money import Currency, Money
from discountz.infra.http import FakeHttp, HttpResponse, Method, Params
from discountz.sources.base import Capability, CatalogPage, NoCredentials, SourceContext
from discountz.sources.fixtures import load_fixture
from discountz.sources.vtex import CATALOG_CAP, STORES, VtexSource

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


def test_catalog_partitions_are_root_categories() -> None:
    tree = load_fixture("vtex", "exito-category-tree")
    url = "https://www.exito.com/io/api/catalog_system/pub/category/tree/1"
    fake = FakeHttp({url: tree})
    parts = VtexSource(STORES["exito"]).catalog_partitions(ctx(fake))
    assert "C:/34185084/" in parts  # Tecnología
    assert len(parts) == len(json.loads(tree.body))


def test_card_facts_from_batch() -> None:
    body = [product("101", [("1", True, 59900.0, 3)]), product("103", [("1", True, 1000.0, 1)])]
    fake = _Router(lambda q: resp(body, total=2, end=1))
    facts = VtexSource(STORES["exito"]).fetch_card_facts(ctx(fake), ["101", "103", "999"])
    assert set(facts) == {"101", "103"}
    f = facts["101"]
    assert f.title == "Producto 101"
    assert f.url == "https://www.exito.com/producto-101/p"
    assert f.image_url == "https://exitocol.vteximg.com.br/arquivos/ids/101/a.jpg"
    assert "fq=skuId%3A101&fq=skuId%3A103&fq=skuId%3A999" in fake.urls[0]


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
