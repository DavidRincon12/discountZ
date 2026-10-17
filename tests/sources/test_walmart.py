"""Adaptador Walmart US contra fixtures reales del 2026-10-13 (spec §4.2 «Walmart»)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from discountz.domain.errors import CredentialUnavailable, NotFound, ParseError
from discountz.domain.guards import batch_is_constant
from discountz.domain.model import ListingRef, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import FakeHttp, Files, HttpResponse, Method, Params
from discountz.sources.base import (
    ListingFailed,
    ListingFound,
    ListingGone,
    NoCredentials,
    SourceContext,
)
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.registry import load_all
from discountz.sources.walmart import (
    WalmartSource,
    parse_walmart_detail,
    parse_walmart_independent,
    parse_walmart_search,
)

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
BASE_URL = "https://www.walmart.com"
FICHAS = ["13573063573", "19912811513", "5113175776"]


def expected(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES_DIR / "walmart_us" / f"{name}.expected.json").read_text()
    )
    return data


def body(name: str) -> bytes:
    return load_fixture("walmart_us", name).body


def usd(minor: int | None) -> Money | None:
    return None if minor is None else Money(minor, Currency.USD)


@pytest.mark.parametrize("item", FICHAS)
def test_detail_fixture_matches_expected(item: str) -> None:
    exp = expected(f"ficha_{item}")
    o = parse_walmart_detail(body(f"ficha_{item}"), item, T)
    assert o is not None
    assert o.ref == ListingRef("walmart_us", exp["external_id"])
    assert o.title == exp["title"]
    assert o.price == usd(exp["price"])
    assert o.list_price == usd(exp["list_price"])
    assert o.in_stock is exp["in_stock"]
    assert o.seller_class == SellerClass(exp["seller_class"])
    assert o.unit_key == exp["unit_key"]
    assert o.url_path == exp["url_path"]


def test_protection_plan_price_is_never_the_product_price() -> None:
    """El 24,00 del spike era «3-Year Plan - $24.00»: la ficha da 199,00, no 24,00."""
    raw = body("ficha_13573063573")
    assert b"3-Year Plan - $24.00" in raw
    o = parse_walmart_detail(raw, "13573063573", T)
    assert o is not None and o.price == usd(19_900)


@pytest.mark.parametrize("item", FICHAS)
def test_independent_route_matches_main_route_when_present(item: str) -> None:
    exp = expected(f"ficha_{item}")
    main = parse_walmart_detail(body(f"ficha_{item}"), item, T)
    o = parse_walmart_independent(body(f"ficha_{item}"), item, T)
    if exp["independent_price"] is None:
        assert o is None  # «See price in cart»: sin precio visible no hay ruta
        return
    assert main is not None and o is not None
    assert o.price == main.price == usd(exp["independent_price"])
    assert o.seller_class is main.seller_class
    assert o.in_stock and o.title == main.title


def test_search_skips_sponsored_preowned_and_duplicates() -> None:
    exp = expected("busqueda_ssd")
    offers = parse_walmart_search(body("busqueda_ssd"), T)
    ids = [o.ref.external_id for o in offers]
    assert ids[: len(exp["first_ids"])] == exp["first_ids"]
    assert len(ids) == len(set(ids))
    assert not set(exp["sponsored_only_ids"]) & set(ids)
    first = offers[0]
    assert first.price == usd(exp["first"]["price"])
    assert first.seller_class == SellerClass(exp["first"]["seller_class"])
    assert first.in_stock is exp["first"]["in_stock"]
    assert not batch_is_constant(offers)


def test_detail_without_next_data_is_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_walmart_detail(b"<html><title>Robot or human?</title></html>", "13573063573", T)


def test_detail_with_other_currency_is_parse_error() -> None:
    data = {
        "props": {
            "pageProps": {
                "initialData": {
                    "data": {
                        "product": {
                            "usItemId": "13573063573",
                            "name": "SSD",
                            "availabilityStatus": "IN_STOCK",
                            "sellerName": "Walmart.com",
                            "priceInfo": {"currentPrice": {"price": 199, "currencyUnit": "CAD"}},
                        }
                    }
                }
            }
        }
    }
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    with pytest.raises(ParseError):
        parse_walmart_detail(html.encode(), "13573063573", T)


def test_parse_url_variants() -> None:
    src = WalmartSource()
    hosts = src.descriptor.hosts

    def parse(raw: str) -> object:
        return src.parse_url(SafeUrl.parse(raw, hosts))

    assert parse("https://www.walmart.com/ip/WD-BLACK-1TB/13573063573?classType=REGULAR") == (
        ListingRef("walmart_us", "13573063573")
    )
    assert parse("https://www.walmart.com/ip/13573063573") == ListingRef(
        "walmart_us", "13573063573"
    )
    assert parse("https://www.walmart.com/search?q=ssd") is None


def test_fetch_follows_one_validated_redirect() -> None:
    canonical = "https://www.walmart.com/ip/13573063573"
    slug = "/ip/WD-BLACK-1TB/13573063573"
    redirect = HttpResponse(301, canonical, {"Location": slug}, b"redirecting...", 1)
    fake = FakeHttp(
        {
            canonical: redirect,
            f"https://www.walmart.com{slug}": load_fixture("walmart_us", "ficha_13573063573"),
        }
    )
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = WalmartSource().fetch_listings(ctx, [ListingRef("walmart_us", "13573063573")])
    assert isinstance(out, ListingFound) and out.offer.price == usd(19_900)


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/ip/x/13573063573", "/ip/x/99999999", "/blocked?url=Lw=="],
)
def test_fetch_rejects_foreign_redirects(location: str) -> None:
    canonical = "https://www.walmart.com/ip/13573063573"
    fake = FakeHttp({canonical: HttpResponse(302, canonical, {"Location": location}, b"", 1)})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = WalmartSource().fetch_listings(ctx, [ListingRef("walmart_us", "13573063573")])
    assert isinstance(out, ListingFailed) and isinstance(out.error, NotFound)
    assert len(fake.calls) == 1


def test_registered_and_curl() -> None:
    src = load_all()["walmart_us"]
    assert src.descriptor.client == "curl"


# ───────────── Modo API de afiliados (spec del rastreo §4.5): sintético hasta la aprobación.

API = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2"
SIGNED = {
    "WM_CONSUMER.ID": "c0ffee",
    "WM_CONSUMER.INTIMESTAMP": "1",
    "WM_SEC.KEY_VERSION": "1",
    "WM_SEC.AUTH_SIGNATURE": "c2ln",
}


class WalmartKeys:
    def bearer(self, provider: str) -> SecretStr:
        raise CredentialUnavailable(provider, "missing")

    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]:
        assert provider == "walmart"
        return SIGNED

    def public_id(self, provider: str) -> str:
        assert provider == "walmart"
        return "pub9"


@dataclass
class Recorder:
    resp: HttpResponse
    calls: list[tuple[str, dict[str, str], dict[str, str]]] = field(default_factory=list)

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
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        self.calls.append((url, dict(pairs), dict(headers or {})))
        return self.resp


def test_api_mode_when_credentials() -> None:
    rec = Recorder(load_fixture("walmart_us", "api-items"))
    ctx = SourceContext(rec, WalmartKeys(), 1e9, T)
    refs = [ListingRef("walmart_us", i) for i in ("5113175776", "19912811513", "13573063573")]
    out = WalmartSource().fetch_listings(ctx, refs)
    assert all(isinstance(o, ListingFound) for o in out)
    (call,) = rec.calls
    url, query, headers = call
    assert url == f"{API}/items"
    assert query["ids"] == "5113175776,19912811513,13573063573"
    assert query["publisherId"] == "pub9"
    assert SIGNED.items() <= headers.items()


def test_parse_api_items() -> None:
    rec = Recorder(load_fixture("walmart_us", "api-items"))
    ctx = SourceContext(rec, WalmartKeys(), 1e9, T)
    refs = [ListingRef("walmart_us", i) for i in ("5113175776", "19912811513", "13573063573")]
    offers = {
        o.offer.ref.external_id: o.offer
        for o in WalmartSource().fetch_listings(ctx, refs)
        if isinstance(o, ListingFound)
    }
    tv = offers["5113175776"]
    assert tv.price == Money(12_999, Currency.USD)
    assert tv.list_price == Money(19_800, Currency.USD)
    assert tv.in_stock
    assert tv.seller_class is SellerClass.FIRST_PARTY
    hub = offers["19912811513"]
    assert hub.in_stock  # «Limited Supply» sigue en stock
    assert hub.seller_class is SellerClass.MARKETPLACE
    assert not offers["13573063573"].in_stock
    assert offers["13573063573"].price is None


def test_api_batches_of_twenty() -> None:
    rec = Recorder(HttpResponse(200, "x", {}, b'{"items": []}', 0))
    ctx = SourceContext(rec, WalmartKeys(), 1e9, T)
    refs = [ListingRef("walmart_us", str(10_000 + i)) for i in range(45)]
    out = WalmartSource().fetch_listings(ctx, refs)
    assert len(rec.calls) == 3
    assert len(out) == 45
    assert all(isinstance(o, ListingGone) for o in out)


def test_api_search_and_card_facts() -> None:
    rec = Recorder(load_fixture("walmart_us", "api-search"))
    ctx = SourceContext(rec, WalmartKeys(), 1e9, T)
    spec = SearchSpec("walmart_us", "tv 50", (), None, None, "tecnologia")
    page = WalmartSource().search(ctx, spec, 0)
    assert {o.ref.external_id for o in page.offers} == {"5113175776", "19912811513", "13573063573"}
    url, query, _ = rec.calls[0]
    assert url == f"{API}/search"
    assert query["query"] == "tv 50"
    rec2 = Recorder(load_fixture("walmart_us", "api-items"))
    facts = WalmartSource().fetch_card_facts(
        SourceContext(rec2, WalmartKeys(), 1e9, T), ["5113175776"]
    )
    assert facts["5113175776"].url == "https://www.walmart.com/ip/5113175776"
    assert (
        facts["5113175776"].image_url
        == "https://i5.walmartimages.com/asr/abc.jpeg?odnHeight=450&odnWidth=450"
    )


def test_html_mode_without_credentials() -> None:
    canonical = f"{BASE_URL}/ip/5113175776"
    fake = FakeHttp({canonical: load_fixture("walmart_us", "ficha_5113175776")})
    ctx = SourceContext(fake, NoCredentials(), 1e9, T)
    (out,) = WalmartSource().fetch_listings(ctx, [ListingRef("walmart_us", "5113175776")])
    assert isinstance(out, ListingFound)
    assert fake.calls[0][1] == canonical


def test_descriptor_image_hosts() -> None:
    assert WalmartSource().descriptor.image_hosts == {"i5.walmartimages.com"}


def test_api_confirm_rereads_item() -> None:
    rec = Recorder(load_fixture("walmart_us", "api-items"))
    ctx = SourceContext(rec, WalmartKeys(), 1e9, T)
    src = WalmartSource()
    (found,) = src.fetch_listings(ctx, [ListingRef("walmart_us", "5113175776")])
    assert isinstance(found, ListingFound)
    confirmed = src.confirm(ctx, found.offer)
    assert confirmed is not None
    assert confirmed.price == Money(12_999, Currency.USD)
    assert rec.calls[-1][1]["ids"] == "5113175776"
    assert all(url.startswith(API) for url, _, _ in rec.calls)
