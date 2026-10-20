from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from discountz.domain.errors import UnsafeUrl
from discountz.domain.model import Lead, ListingRef, Offer, SearchSpec
from discountz.domain.money import Currency
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    ListingOutcome,
    NeedsResolution,
    SearchPage,
    SourceContext,
    SourceDescriptor,
)
from discountz.sources.fixtures import load_fixture, save_fixture, scrub
from discountz.sources.registry import load_all, route_url

ASIN_PATH = re.compile(r"^/(?:[^/]+/)?(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)")


class FakeAmazon:
    descriptor = SourceDescriptor(
        code="amazon_us",
        kind="store",
        currency=Currency.USD,
        hosts=frozenset({"www.amazon.com", "amazon.com", "a.co", "amzn.to"}),
        client="curl",
        capabilities=frozenset({Capability.DETAIL}),
        max_batch=1,
        external_id_re=re.compile(r"^[A-Z0-9]{10}$"),
    )

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        if url.host in ("a.co", "amzn.to"):
            return NeedsResolution("amazon_us", f"short:{url.host}{url.path}")
        m = ASIN_PATH.match(url.path)
        return ListingRef("amazon_us", m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise NotImplementedError

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        raise NotImplementedError

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        raise NotImplementedError

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        return None

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return None


SOURCES = {"amazon_us": FakeAmazon()}


def test_route_amazon_dp_url() -> None:
    source, ref = route_url("https://www.amazon.com/dp/B0EXAMPLE1?tag=x", SOURCES)
    assert source.descriptor.code == "amazon_us"
    assert ref == ListingRef("amazon_us", "B0EXAMPLE1")


def test_route_short_link_needs_resolution() -> None:
    _, ref = route_url("https://a.co/d/xyz", SOURCES)
    assert ref == NeedsResolution("amazon_us", "short:a.co/d/xyz")


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.ebay.com/itm/1",
        "https://www.amazon.com/gp/help",
        "http://www.amazon.com/dp/B0EXAMPLE1",
    ],
)
def test_unroutable_urls_raise(raw: str) -> None:
    with pytest.raises(UnsafeUrl):
        route_url(raw, SOURCES)


def test_fixture_roundtrip(tmp_path: Path) -> None:
    resp = HttpResponse(206, "https://www.exito.com/api?x=1", {"set-cookie": "a=b"}, b'{"a":1}', 9)
    save_fixture(tmp_path, "vtex", "busqueda_ssd", resp, recorded_at="2026-10-13")
    loaded = load_fixture("vtex", "busqueda_ssd", root=tmp_path)
    assert loaded.status == 206 and loaded.body == b'{"a":1}'
    assert loaded.headers == {}  # nunca se guardan cabeceras ni cookies
    assert (tmp_path / "vtex" / "busqueda_ssd.json.gz").exists()


def test_scrub_removes_ips_and_delivery_location() -> None:
    body = b"ip 181.49.10.3 and 2800:e2:9c00::1 <span>Deliver to Bucaramanga 680001</span>"
    out = scrub(body)
    assert b"181.49.10.3" not in out and b"2800:e2" not in out
    assert b"Bucaramanga" not in out and b"680001" not in out


def test_scrub_keeps_times_and_hex_that_are_not_ips() -> None:
    body = b"12:30:45 #ab:cd:ef 2026-10-13T15:00:00Z v1.2.3 999.1.1.1"
    assert scrub(body) == body


def test_route_alkosto_product_url() -> None:
    source, ref = route_url("https://www.alkosto.com/tv-kalley/p/7705946474559", load_all())
    assert source.descriptor.code == "alkosto"
    assert ref == ListingRef("alkosto", "7705946474559")
