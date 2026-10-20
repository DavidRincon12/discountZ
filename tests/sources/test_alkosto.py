"""Alkosto por su índice de Algolia (spec de Alkosto §2 y §7, ADR 0009)."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from discountz.domain.errors import HardBlock, ParseError, Unavailable
from discountz.domain.model import ListingRef, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.scheduling import SourceBudget
from discountz.domain.urls import SafeUrl
from discountz.infra.http import Files, HttpResponse, Method, PacedClient, Params
from discountz.sources.alkosto import (
    ALGOLIA,
    BASE,
    HOSTS,
    INDEX,
    AlkostoItem,
    AlkostoSource,
    parse_api_key,
    parse_objects,
    parse_query,
)
from discountz.sources.base import (
    CatalogPage,
    ListingFailed,
    ListingFound,
    ListingGone,
    NoCredentials,
    SourceContext,
)
from discountz.sources.fixtures import load_fixture

TV = "7705946474559"
OTHER = "6937574879524"
MISSING = "0000000000000"
T = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
QUERY = f"{ALGOLIA}/1/indexes/{INDEX}/query"
OBJECTS = f"{ALGOLIA}/1/indexes/*/objects"


def _body(name: str) -> bytes:
    return load_fixture("alkosto", name).body


def _band() -> dict[str, object]:
    data: dict[str, object] = json.loads(_body("query-band"))
    return data


def _with_hit(**changes: object) -> bytes:
    """La franja grabada con el primer hit (el TV) modificado; `None` borra el campo."""
    data = _band()
    hits = data["hits"]
    assert isinstance(hits, list)
    hit = dict(hits[0])
    for key, value in changes.items():
        if value is None:
            hit.pop(key, None)
        else:
            hit[key] = value
    hits[0] = hit
    return json.dumps(data).encode()


def test_parse_api_key_reads_home() -> None:
    assert parse_api_key(_body("home")) == "CLAVE_DE_PRUEBA"


def test_parse_api_key_missing_is_parse_error() -> None:
    with pytest.raises(ParseError) as exc:
        parse_api_key(b"<html><script>var x = 1;</script></html>")
    assert exc.value.code == "api_key"


def test_parse_query_uses_lowest_price_not_list_price() -> None:
    items, _ = parse_query(_with_hit(pricevalue_cop_double=3899900.0))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.price == Money(1649900, Currency.COP)


def test_parse_query_reads_stock_ean_url_image_path() -> None:
    items, hits = parse_query(_body("query-band"))
    assert hits == 327
    assert len(items) == 30
    tv = next(i for i in items if i.external_id == TV)
    assert tv.in_stock is True
    assert tv.ean == TV
    assert tv.url.startswith("https://www.alkosto.com/")
    assert tv.url.endswith(f"/p/{TV}")
    assert tv.image_url is not None
    assert tv.image_url.startswith("https://cdn.dam.alkosto.com/")
    assert tv.category_path == "/tv/smart tv/"
    assert tv.title.startswith("TV KALLEY 60")


def test_parse_query_falls_back_to_750_image() -> None:
    items, _ = parse_query(_with_hit(**{"img-820wx820h_string": None}))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.image_url is not None
    assert "w=750" in tv.image_url


@given(st.sampled_from([None, 0, -5, 12.5, "1649900"]))
def test_parse_query_skips_bad_price(price: object) -> None:
    items, _ = parse_query(_with_hit(lowestprice_double=price))
    ids = {i.external_id for i in items}
    assert TV not in ids
    assert len(ids) == 29


@pytest.mark.parametrize("url", ["https://evil.com/p/1", "", "//evil.com/p/1"])
def test_parse_query_skips_foreign_url(url: str) -> None:
    items, _ = parse_query(_with_hit(url_es_string=url))
    assert TV not in {i.external_id for i in items}
    assert len(items) == 29


def test_parse_query_logs_skipped_hits_once_per_page(caplog: pytest.LogCaptureFixture) -> None:
    """~400 hits sin precio por vuelta llenaban el log con una línea cada uno (2026-10-20)."""
    data = _band()
    hits = data["hits"]
    assert isinstance(hits, list)
    for i in range(3):
        hits[i] = {**hits[i], "lowestprice_double": None}
    hits[3] = {**hits[3], "url_es_string": "https://evil.com/p/1"}
    caplog.set_level(logging.INFO, logger="discountz")
    items, _ = parse_query(json.dumps(data).encode())
    assert len(items) == 26
    lines = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
    assert lines == [
        "alkosto_items_skipped source=alkosto code=bad_price count=3",
        "alkosto_items_skipped source=alkosto code=bad_url count=1",
    ]


def test_parse_query_out_of_stock() -> None:
    items, _ = parse_query(_with_hit(stocklevel_int=0))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.in_stock is False
    assert tv.price is None


def test_parse_query_ean_only_with_gtin_length() -> None:
    items, _ = parse_query(_with_hit(code_string="ABC123"))
    tv = next(i for i in items if i.external_id == TV)
    assert tv.ean is None


def test_parse_objects_omits_missing() -> None:
    found = parse_objects(_body("objects"), ["7705946474559", "6937574879524", "0000000000000"])
    assert len(found) == 2
    assert "0000000000000" not in found
    assert all(
        isinstance(item, AlkostoItem) and item.external_id == key for key, item in found.items()
    )


def _broken_objects() -> bytes:
    data = json.loads(_body("objects"))
    data["results"][1]["lowestprice_double"] = 12.5
    return json.dumps(data).encode()


def test_parse_objects_marks_broken_items() -> None:
    found = parse_objects(_broken_objects(), [TV, OTHER, MISSING])
    assert isinstance(found[TV], AlkostoItem)
    assert isinstance(found[OTHER], ParseError)
    assert MISSING not in found


def test_parse_objects_rejects_a_result_count_mismatch() -> None:
    with pytest.raises(ParseError):
        parse_objects(_body("objects"), [TV])


def test_parse_query_with_only_broken_hits_is_parse_error() -> None:
    data = _band()
    hits = data["hits"]
    assert isinstance(hits, list)
    for hit in hits:
        hit["lowestprice_double"] = 12.5
    with pytest.raises(ParseError):
        parse_query(json.dumps(data).encode())


def test_parse_query_without_hits_is_empty() -> None:
    assert parse_query(b'{"hits": [], "nbHits": 0}') == ([], 0)


def test_parse_query_rejects_garbage() -> None:
    with pytest.raises(ParseError):
        parse_query(b'{"message": "nada"}')
    with pytest.raises(ParseError):
        parse_query(b"<html>")


# --- fuente --------------------------------------------------------------------------------

Handler = Callable[[str, str, Mapping[str, object] | None], HttpResponse]


@dataclass
class Api:
    """HttpClient falso: responde según método y URL; anota el cuerpo y las cabeceras."""

    handler: Handler
    calls: list[tuple[str, str, Mapping[str, object] | None, Mapping[str, str]]] = field(
        default_factory=list
    )

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
        self.calls.append((method, url, json, dict(headers or {})))
        return self.handler(method, url, json)

    def count(self, url: str) -> int:
        return sum(1 for _, u, _, _ in self.calls if u == url)


def fx(name: str) -> HttpResponse:
    return load_fixture("alkosto", name)


def resp(body: bytes, status: int = 200, url: str = QUERY) -> HttpResponse:
    return HttpResponse(status, url, {}, body, 0)


def routes(**by_url: HttpResponse | list[HttpResponse]) -> Handler:
    """`home`, `query` y `objects` → respuesta fija o secuencia (la última se repite)."""
    table = {
        BASE + "/": by_url.get("home", fx("home")),
        QUERY: by_url.get("query"),
        OBJECTS: by_url.get("objects"),
    }
    seen: dict[str, int] = {}

    def handle(method: str, url: str, body: Mapping[str, object] | None) -> HttpResponse:
        route = table.get(url)
        assert route is not None, f"sin ruta: {method} {url}"
        if not isinstance(route, HttpResponse):
            i = seen.get(url, 0)
            seen[url] = i + 1
            route = route[min(i, len(route) - 1)]
        return _per_request(route, body) if url == OBJECTS else route

    return handle


def _per_request(route: HttpResponse, body: Mapping[str, object] | None) -> HttpResponse:
    """Como Algolia, `getObjects` responde un resultado por id pedido, en el mismo orden."""
    if route.status != 200 or body is None:
        return route
    by_id = {r["objectID"]: r for r in json.loads(route.body)["results"] if r is not None}
    requests = body["requests"]
    assert isinstance(requests, list)
    results = [by_id.get(r["objectID"]) for r in requests]
    return resp(json.dumps({"results": results}).encode(), url=OBJECTS)


def paced(api: Api, src: AlkostoSource) -> PacedClient:
    budget = SourceBudget("alkosto", 0, 0, 50, 100, timedelta(hours=2), timedelta(hours=6), 1)
    return PacedClient(
        api,
        budget,
        lambda s: None,
        lambda: 0.0,
        random.Random(0),
        src.detect_block,
        source="alkosto",
    )


def run(**by_url: HttpResponse | list[HttpResponse]) -> tuple[AlkostoSource, Api, SourceContext]:
    src = AlkostoSource()
    api = Api(routes(**by_url))
    return src, api, SourceContext(paced(api, src), NoCredentials(), 1e9, T)


def test_parse_url_accepts_product_pages_only() -> None:
    src = AlkostoSource()
    url = SafeUrl.parse(f"{BASE}/tv-kalley-60/p/{TV}", HOSTS)
    assert src.parse_url(url) == ListingRef("alkosto", TV)
    assert src.parse_url(SafeUrl.parse(f"{BASE}/tv/c/BI_TVVI_ALKOS", HOSTS)) is None
    assert src.parse_url(SafeUrl.parse(f"{BASE}/x/p/{TV}/otra", HOSTS)) is None
    assert src.parse_url(SafeUrl.parse(f"{BASE}/x/p/ab-c", HOSTS)) is None


def test_fetch_listings_found_and_gone() -> None:
    src, api, ctx = run(objects=fx("objects"))
    refs = [ListingRef("alkosto", i) for i in (TV, OTHER, MISSING)]
    out = src.fetch_listings(ctx, refs)
    assert isinstance(out[0], ListingFound)
    offer = out[0].offer
    assert offer.price == Money(1649900, Currency.COP)
    assert offer.list_price is None
    assert offer.seller_class is SellerClass.FIRST_PARTY
    assert offer.url_path is not None
    assert offer.url_path.endswith(f"/p/{TV}")
    assert isinstance(out[1], ListingFound)
    assert out[2] == ListingGone(refs[2])
    sent = api.calls[-1][2]
    assert sent is not None
    requests = sent["requests"]
    assert isinstance(requests, list)
    assert [r["objectID"] for r in requests] == [TV, OTHER, MISSING]
    assert all(r["indexName"] == INDEX for r in requests)


def _objects_with(**changes: object) -> HttpResponse:
    data = json.loads(fx("objects").body)
    data["results"][0].update(changes)
    return resp(json.dumps(data).encode(), url=OBJECTS)


def test_confirm_returns_none_when_out_of_stock() -> None:
    src, _, ctx = run(objects=_objects_with(stocklevel_int=0))
    found = src.fetch_listings(ctx, [ListingRef("alkosto", TV)])[0]
    assert isinstance(found, ListingFound)
    assert src.confirm(ctx, found.offer) is None


def test_confirm_returns_the_fresh_offer() -> None:
    src, _, ctx = run(objects=fx("objects"))
    found = src.fetch_listings(ctx, [ListingRef("alkosto", TV)])[0]
    assert isinstance(found, ListingFound)
    again = src.confirm(ctx, found.offer)
    assert again is not None
    assert again.price == Money(1649900, Currency.COP)


def test_search_posts_query() -> None:
    src, api, ctx = run(query=fx("search"))
    page = src.search(ctx, SearchSpec("alkosto", "televisor", (), None, None, "tecnologia"), 0)
    assert len(page.offers) == 20
    method, url, sent, headers = api.calls[-1]
    assert (method, url) == ("POST", QUERY)
    assert sent is not None
    assert sent["query"] == "televisor"
    assert sent["hitsPerPage"] == 20
    assert "objectID" in list(sent["attributesToRetrieve"])  # type: ignore[call-overload]
    assert headers["X-Algolia-API-Key"] == "CLAVE_DE_PRUEBA"
    assert headers["X-Algolia-Application-Id"] == "QX5IPS1B1Q"


def test_key_is_read_once_per_process() -> None:
    src, api, ctx = run(query=fx("search"))
    spec = SearchSpec("alkosto", "televisor", (), None, None, "tecnologia")
    src.search(ctx, spec, 0)
    src.search(ctx, spec, 0)
    assert api.count(BASE + "/") == 1


def test_invalid_key_rereads_home_once_then_hard_block() -> None:
    src, api, ctx = run(query=fx("invalid-key"))
    spec = SearchSpec("alkosto", "televisor", (), None, None, "tecnologia")
    with pytest.raises(HardBlock) as exc:
        src.search(ctx, spec, 0)
    assert exc.value.code == "api_key"
    assert api.count(BASE + "/") == 2
    assert api.count(QUERY) == 2


def test_invalid_key_recovers_after_rereading_home() -> None:
    src, api, ctx = run(query=[fx("invalid-key"), fx("search")])
    page = src.search(ctx, SearchSpec("alkosto", "tv", (), None, None, "tecnologia"), 0)
    assert len(page.offers) == 20
    assert api.count(BASE + "/") == 2


def test_rate_limited_is_unavailable() -> None:
    src, _, ctx = run(query=resp(b"<html>too many</html>", 429))
    with pytest.raises(Unavailable) as exc:
        src.search(ctx, SearchSpec("alkosto", "tv", (), None, None, "tecnologia"), 0)
    assert exc.value.code == "rate_limited"


def test_home_without_key_is_parse_error() -> None:
    src, _, ctx = run(home=resp(b"<html></html>", url=BASE + "/"), query=fx("search"))
    with pytest.raises(ParseError) as exc:
        src.search(ctx, SearchSpec("alkosto", "tv", (), None, None, "tecnologia"), 0)
    assert exc.value.code == "api_key"


def test_key_never_logged_nor_in_errors(caplog: pytest.LogCaptureFixture) -> None:
    secret = "SECRETO123ABCDEF"  # noqa: S105 - falsa, de prueba
    home = resp(b'<script>"apiKey": "' + secret.encode() + b'",</script>', url=BASE + "/")
    src, _, ctx = run(home=home, query=fx("invalid-key"))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HardBlock) as exc:
        src.search(ctx, SearchSpec("alkosto", "tv", (), None, None, "tecnologia"), 0)
    assert secret not in caplog.text
    assert secret not in str(exc.value)
    assert secret not in repr(exc.value)
    assert secret not in repr(src)


# --- catálogo ------------------------------------------------------------------------------


def _band_with(nb_hits: int) -> HttpResponse:
    data = json.loads(fx("query-band").body)
    data["nbHits"] = nb_hits
    return resp(json.dumps(data).encode())


def test_catalog_starts_with_one_band() -> None:
    src, api, ctx = run()
    assert list(src.catalog_partitions(ctx)) == ["1-999999999"]
    assert api.calls == []


def test_big_band_splits_without_items() -> None:
    src, _, ctx = run(query=fx("query-big"))
    page = src.fetch_catalog_page(ctx, "1-999999999", 0)
    assert page == CatalogPage((), False, split=["1-31622", "31623-999999999"])


def test_band_reads_items_with_numeric_filters() -> None:
    src, api, ctx = run(query=fx("query-band"))
    page = src.fetch_catalog_page(ctx, "1500000-1700000", 0)
    assert page.split == ()
    assert page.has_more is False
    assert len(page.items) == 30
    tv = next(o for o in page.items if o.external_id == TV)
    assert tv.price == Money(1649900, Currency.COP)
    assert tv.category_path == "/tv/smart tv/"
    assert tv.ean == TV
    assert tv.seller_class is SellerClass.FIRST_PARTY
    sent = api.calls[-1][2]
    assert sent is not None
    assert sent["numericFilters"] == ["lowestprice_double>=1500000", "lowestprice_double<=1700000"]
    assert sent["hitsPerPage"] == 1000
    assert sent["page"] == 0


def test_band_skips_out_of_stock_items() -> None:
    data = json.loads(fx("query-band").body)
    data["hits"][0]["stocklevel_int"] = 0
    src, _, ctx = run(query=resp(json.dumps(data).encode()))
    page = src.fetch_catalog_page(ctx, "1500000-1700000", 0)
    assert TV not in {o.external_id for o in page.items}
    assert len(page.items) == 29


def test_single_price_band_over_limit_is_read_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    src, _, ctx = run(query=_band_with(1200))
    caplog.set_level(logging.INFO)
    page = src.fetch_catalog_page(ctx, "1649900-1649900", 0)
    assert page.split == ()
    assert len(page.items) == 30
    assert "alkosto_band_truncated" in caplog.text


def test_later_pages_are_empty() -> None:
    src, api, ctx = run(query=fx("query-band"))
    assert src.fetch_catalog_page(ctx, "1-10", 1) == CatalogPage((), False)
    assert api.calls == []


@pytest.mark.parametrize("partition", ["abc", "10-5", "1-2-3", "-1-5", "0-5", ""])
def test_bad_partition_is_parse_error(partition: str) -> None:
    src, _, ctx = run(query=fx("query-band"))
    with pytest.raises(ParseError):
        src.fetch_catalog_page(ctx, partition, 0)


def test_changed_is_not_supported() -> None:
    src, api, ctx = run()
    assert src.fetch_changed(ctx, T, 0) == CatalogPage((), False)
    assert src.descriptor.supports_changed is False
    assert api.calls == []


def test_catalog_items_batches_of_100() -> None:
    src, api, ctx = run(objects=fx("objects"))
    ids = [TV, OTHER] + [f"{n:013d}" for n in range(148)]
    found = src.fetch_catalog_items(ctx, ids)
    assert {o.external_id for o in found} == {TV, OTHER}
    assert api.count(OBJECTS) == 2


def test_card_facts_from_objects() -> None:
    src, _, ctx = run(objects=fx("objects"))
    facts = src.fetch_card_facts(ctx, [TV, MISSING])
    assert set(facts) == {TV}
    assert facts[TV].title.startswith("TV KALLEY 60")
    assert facts[TV].url.endswith(f"/p/{TV}")
    assert facts[TV].image_url is not None


def test_invalid_key_is_recognized_with_a_lowercase_host() -> None:
    """httpx pasa el host a minúsculas: la URL real es `https://qx5ips1b1q-dsn.algolia.net/…`."""
    lower = fx("invalid-key")
    lower = HttpResponse(lower.status, lower.url.lower(), {}, lower.body, 0)
    limited = resp(b"<html>too many</html>", 429, url=QUERY.lower())
    src, api, ctx = run(query=lower)
    with pytest.raises(HardBlock) as exc:
        src.search(ctx, SearchSpec("alkosto", "tv", (), None, None, "tecnologia"), 0)
    assert exc.value.code == "api_key"
    assert api.count(BASE + "/") == 2
    src, _, ctx = run(query=limited)
    with pytest.raises(Unavailable) as unavailable:
        src.search(ctx, SearchSpec("alkosto", "tv", (), None, None, "tecnologia"), 0)
    assert unavailable.value.code == "rate_limited"


def test_broken_item_is_failed_not_gone() -> None:
    data = json.loads(fx("objects").body)
    data["results"][1]["lowestprice_double"] = 12.5
    src, _, ctx = run(objects=resp(json.dumps(data).encode(), url=OBJECTS))
    out = src.fetch_listings(ctx, [ListingRef("alkosto", i) for i in (TV, OTHER, MISSING)])
    assert isinstance(out[0], ListingFound)
    assert isinstance(out[1], ListingFailed)
    assert isinstance(out[1].error, ParseError)
    assert out[2] == ListingGone(ListingRef("alkosto", MISSING))


def test_confirm_of_a_broken_item_is_parse_error() -> None:
    src, _, ctx = run(objects=fx("objects"))
    found = src.fetch_listings(ctx, [ListingRef("alkosto", OTHER)])[0]
    assert isinstance(found, ListingFound)
    data = json.loads(fx("objects").body)
    data["results"] = [dict(data["results"][1], lowestprice_double=12.5)]
    src, _, ctx = run(objects=resp(json.dumps(data).encode(), url=OBJECTS))
    with pytest.raises(ParseError):
        src.confirm(ctx, found.offer)
