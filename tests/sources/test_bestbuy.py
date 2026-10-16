"""Adaptador Best Buy US por la API oficial (spec del rastreo §2.1 y §4.4).

Las fixtures `api-*` son sintéticas (ver su `"nota"`) hasta que David grabe las reales con su clave.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlencode, urlsplit

import pytest
from pydantic import SecretStr

from discountz.domain.errors import CredentialUnavailable, SourceError
from discountz.domain.model import ExtractionPath, ListingRef, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.scheduling import SourceBudget
from discountz.domain.urls import SafeUrl
from discountz.infra.http import HttpResponse, Method, PacedClient, Params
from discountz.sources.base import (
    Capability,
    ListingFailed,
    ListingFound,
    ListingGone,
    NoCredentials,
    SourceContext,
)
from discountz.sources.bestbuy import BestBuySource
from discountz.sources.fixtures import load_fixture
from discountz.sources.registry import load_all

T = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
KEY = "s3cr3tK3y"


class Key:
    def bearer(self, provider: str) -> SecretStr:
        assert provider == "bestbuy"
        return SecretStr(KEY)

    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]:
        raise CredentialUnavailable(provider, "missing")


@dataclass
class Api:
    """HttpClient falso: responde según el recurso (`products(...)` o `products/<sku>.json`)."""

    handler: Callable[[str, dict[str, str]], HttpResponse]
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

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
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        resource = unquote(urlsplit(url).path).removeprefix("/v1/")
        query = dict(pairs)
        self.calls.append((resource, query))
        return self.handler(resource, query)


def fixture(name: str) -> Callable[[str, dict[str, str]], HttpResponse]:
    resp = load_fixture("bestbuy_us", name)
    return lambda resource, query: resp


def ctx(http: object, creds: object | None = None) -> SourceContext:
    return SourceContext(http, creds or Key(), 1e9, T)  # type: ignore[arg-type]


def usd(minor: int) -> Money:
    return Money(minor, Currency.USD)


def test_parse_products_api() -> None:
    api = Api(fixture("api-products"))
    refs = [ListingRef("bestbuy_us", s) for s in ("6680742", "6385543", "1234567")]
    out = BestBuySource().fetch_listings(ctx(api), refs)
    found = {o.offer.ref.external_id: o.offer for o in out if isinstance(o, ListingFound)}
    assert set(found) == {"6680742", "6385543"}
    ssd = found["6680742"]
    assert ssd.price == usd(18_999)
    assert ssd.list_price == usd(24_999)
    assert ssd.in_stock
    assert ssd.seller_class is SellerClass.FIRST_PARTY
    assert ssd.extraction is ExtractionPath.API_JSON
    assert ssd.url_path == "/site/6680742.p?skuId=6680742"
    pods = found["6385543"]
    assert not pods.in_stock
    assert pods.price is None
    assert isinstance(out[2], ListingGone)
    resource, query = api.calls[0]
    assert resource == "products(sku in(6680742,6385543,1234567))"
    assert query["apiKey"] == KEY
    assert query["format"] == "json"


def test_catalog_path_joins_category_names() -> None:
    api = Api(fixture("api-catalog"))
    page = BestBuySource().fetch_catalog_page(ctx(api), "all", 0)
    by_id = {o.external_id: o for o in page.items}
    assert set(by_id) == {"1000006", "1000017", "1000020"}  # sin el no disponible
    hdmi = by_id["1000006"]
    assert hdmi.category_path == "/TV & Home Theater/TV & Home Theater Accessories/HDMI Cables/"
    assert hdmi.price == usd(999)
    assert hdmi.ean == "600603149567"
    assert by_id["1000020"].ean is None
    assert by_id["1000020"].seller_class is SellerClass.MARKETPLACE
    assert page.has_more  # página 1 de 104
    resource, query = api.calls[0]
    assert resource == "products(active=true&orderable!=NotAvailable)"
    assert (query["sort"], query["pageSize"], query["page"]) == ("sku.asc", "100", "1")
    assert "categoryPath.name" in query["show"]


def test_catalog_last_page_has_no_more() -> None:
    data = json.loads(load_fixture("bestbuy_us", "api-catalog").body)
    data |= {"currentPage": 104, "totalPages": 104}
    resp = HttpResponse(200, "x", {}, json.dumps(data).encode(), 0)
    page = BestBuySource().fetch_catalog_page(ctx(Api(lambda r, q: resp)), "all", 103)
    assert not page.has_more


def test_catalog_partitions_single() -> None:
    assert list(BestBuySource().catalog_partitions(ctx(Api(fixture("api-catalog"))))) == ["all"]


def test_changed_uses_price_update_date() -> None:
    api = Api(fixture("api-changed"))
    since = datetime(2026, 10, 16, 11, 0, tzinfo=UTC)
    page = BestBuySource().fetch_changed(ctx(api), since, 0)
    assert [o.external_id for o in page.items] == ["6680742"]
    assert page.items[0].price == usd(15_999)
    assert not page.has_more
    resource, query = api.calls[0]
    # Best Buy publica las fechas en hora del centro de EE. UU. sin zona: margen de 6 h.
    floor = (since - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")
    assert resource == f"products(priceUpdateDate>{floor}&active=true)"
    assert query["sort"] == "sku.asc"


def test_card_facts_from_api() -> None:
    api = Api(fixture("api-products"))
    facts = BestBuySource().fetch_card_facts(ctx(api), ["6680742"])
    f = facts["6680742"]
    assert f.title.startswith("Samsung - 990 PRO")
    assert f.url == "https://www.bestbuy.com/site/6680742.p?skuId=6680742"
    assert f.image_url is not None
    assert urlsplit(f.image_url).hostname in BestBuySource().descriptor.image_hosts


def test_confirm_reads_single_product() -> None:
    data = json.loads(load_fixture("bestbuy_us", "api-products").body)["products"][0]
    resp = HttpResponse(200, "x", {}, json.dumps(data).encode(), 0)
    api = Api(lambda r, q: resp)
    src = BestBuySource()
    (found,) = src.fetch_listings(
        ctx(Api(fixture("api-products"))), [ListingRef("bestbuy_us", "6680742")]
    )
    assert isinstance(found, ListingFound)
    confirmed = src.confirm(ctx(api), found.offer)
    assert confirmed is not None
    assert confirmed.price == usd(18_999)
    assert api.calls[0][0] == "products/6680742.json"


def test_search_sanitizes_keywords() -> None:
    api = Api(fixture("api-products"))
    spec = SearchSpec("bestbuy_us", "ssd 2tb) & (sku=1", (), None, None, "tecnologia")
    page = BestBuySource().search(ctx(api), spec, 0)
    assert page.offers
    assert api.calls[0][0] == "products(search=ssd&search=2tb&search=sku1)"


def test_missing_key_fails_listing_without_request() -> None:
    api = Api(fixture("api-products"))
    (out,) = BestBuySource().fetch_listings(
        ctx(api, NoCredentials()), [ListingRef("bestbuy_us", "6680742")]
    )
    assert isinstance(out, ListingFailed)
    assert isinstance(out.error, CredentialUnavailable)
    assert api.calls == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b'{"errorCode":"403","errorMessage":"Over Quota"}', "quota"),
        (b'{"errorCode":"403","errorMessage":"The provided API Key is invalid."}', "bad_key"),
    ],
)
def test_api_key_never_in_errors(body: bytes, code: str) -> None:
    url = f"https://api.bestbuy.com/v1/products(sku in(6680742))?{urlencode({'apiKey': KEY})}"
    resp = HttpResponse(403, url, {}, body, 0)
    src = BestBuySource()
    budget = SourceBudget("bestbuy_us", 0, 0, 10, 100, timedelta(hours=6), timedelta(hours=12), 100)
    paced = PacedClient(
        Api(lambda r, q: resp),
        budget,
        lambda s: None,
        lambda: 0.0,
        random.Random(1),
        src.detect_block,
        source="bestbuy_us",
    )
    (out,) = src.fetch_listings(ctx(paced), [ListingRef("bestbuy_us", "6680742")])
    assert isinstance(out, ListingFailed)
    assert isinstance(out.error, SourceError)
    assert out.error.code == code
    assert KEY not in str(out.error)
    assert KEY not in repr(out.error)
    assert paced.blocked is None  # cuota o clave: no es un bloqueo anti-bots


def test_parse_url_unchanged() -> None:
    src = BestBuySource()
    hosts = src.descriptor.hosts

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, hosts))

    ref = ListingRef("bestbuy_us", "6680742")
    assert parse("https://www.bestbuy.com/site/samsung-990/6680742.p?skuId=6680742") == ref
    assert parse("https://www.bestbuy.com/site/6680742.p?skuId=6680742") == ref
    assert parse("https://www.bestbuy.com/product/samsung-990/JJGRF3QVGW/sku/6680742") == ref
    assert parse("https://www.bestbuy.com/product/samsung-990/JJGRF3QVGW") is None
    assert parse("https://www.bestbuy.com/site/searchpage.jsp?st=ssd") is None


def test_registered_as_api_source() -> None:
    d = load_all()["bestbuy_us"].descriptor
    assert d.client == "httpx"
    assert d.auth == "api_key"
    assert d.max_batch == 100
    assert d.supports_changed
    assert d.capabilities == {
        Capability.DETAIL,
        Capability.SEARCH,
        Capability.CONFIRM,
        Capability.CATALOG,
    }
    assert "pisces.bbystatic.com" in d.image_hosts
