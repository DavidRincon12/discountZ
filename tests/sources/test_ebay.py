"""Adaptador de eBay por la Browse API (spec de eBay §2).

Fixtures reales grabadas desde Actions el 2026-10-18 (recortadas, ver `"recorte"`); la de cuota y
la de 404 son sintéticas (ver su `"nota"`).
"""

from __future__ import annotations

import json
import logging
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from discountz.config import EbayParams
from discountz.domain.errors import CredentialUnavailable, ParseError, Unavailable
from discountz.domain.model import ListingRef, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.scheduling import SourceBudget
from discountz.domain.urls import SafeUrl
from discountz.infra.http import Files, HttpResponse, Method, PacedClient, Params
from discountz.sources.base import (
    CatalogPartition,
    ListingFailed,
    ListingFound,
    ListingGone,
    SourceContext,
)
from discountz.sources.ebay import (
    CODE,
    EbayRules,
    EbaySource,
    from_item_id,
    parse_item,
    parse_search,
    to_item_id,
)
from discountz.sources.fixtures import load_fixture

T = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
BASIC = "QmFzaWNTZWNyZXRv"  # base64 falso de `app:cert`
TOKEN = "TKN-uno"  # noqa: S105 - falso, de prueba
TOKEN2 = "TKN-dos"
RULES = EbayRules(min_feedback_pct=98, min_feedback_score=100, allow_variants=False)
HOSTS = frozenset({"www.ebay.com", "ebay.com"})
ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")


class Creds:
    def bearer(self, provider: str) -> SecretStr:
        assert provider == "ebay"
        return SecretStr(BASIC)

    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]:
        raise CredentialUnavailable(provider, "missing")

    def public_id(self, provider: str) -> str:
        raise CredentialUnavailable(provider, "missing")


Handler = Callable[[str, str, dict[str, str], Mapping[str, str]], HttpResponse]


@dataclass
class Api:
    """HttpClient falso: responde según método y ruta; anota cabeceras y parámetros."""

    handler: Handler
    calls: list[tuple[str, str, dict[str, str], Mapping[str, str]]] = field(default_factory=list)

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
        path = unquote(urlsplit(url).path)
        query = dict(pairs) | dict(data or {})
        hdrs = dict(headers or {})
        self.calls.append((method, path, query, hdrs))
        return self.handler(method, path, query, hdrs)


def body(obj: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, "https://api.ebay.com/x", {}, json.dumps(obj).encode(), 0)


def token_resp(token: str = TOKEN, expires_in: int = 7200) -> HttpResponse:
    return body({"access_token": token, "expires_in": expires_in, "token_type": "Application"})


def fx(name: str) -> HttpResponse:
    return load_fixture(CODE, name)


def fx_json(name: str) -> dict[str, object]:
    data: dict[str, object] = json.loads(fx(name).body)
    return data


def paced(api: Api, src: EbaySource) -> PacedClient:
    budget = SourceBudget(CODE, 0, 0, 50, 100, timedelta(hours=2), timedelta(hours=6), 1)
    return PacedClient(
        api, budget, lambda s: None, lambda: 0.0, random.Random(0), src.detect_block, source=CODE
    )


def ctx(http: object, now: datetime = T) -> SourceContext:
    return SourceContext(http, Creds(), 1e9, now)  # type: ignore[arg-type]


def usd(minor: int) -> Money:
    return Money(minor, Currency.USD)


def spec(query: str) -> SearchSpec:
    return SearchSpec(CODE, query, (), None, None, "tecnologia")


def source() -> EbaySource:
    return EbaySource(EbayParams())


def routes(search: HttpResponse | list[HttpResponse], tokens: list[HttpResponse]) -> Api:
    seq = {"search": search if isinstance(search, list) else [search], "token": tokens}

    def handler(method: str, path: str, q: dict[str, str], h: Mapping[str, str]) -> HttpResponse:
        key = "token" if path.endswith("/oauth2/token") else "search"
        items = seq[key]
        return items.pop(0) if len(items) > 1 else items[0]

    return Api(handler)


# --- ids y enlaces -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.ebay.com/itm/123456789012", "123456789012"),
        ("https://www.ebay.com/itm/apple-iphone/123456789012?var=456", "123456789012V456"),
        ("https://ebay.com/itm/123456789012?var=0&hash=item1", "123456789012"),
    ],
)
def test_parse_url_listing(url: str, expected: str) -> None:
    assert source().parse_url(SafeUrl.parse(url, HOSTS)) == ListingRef(CODE, expected)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ebay.com/sch/i.html?_nkw=tv",
        "https://www.ebay.com/itm/12345",
        "https://www.ebay.com/",
    ],
)
def test_parse_url_rejects_other_paths(url: str) -> None:
    assert source().parse_url(SafeUrl.parse(url, HOSTS)) is None


@given(st.integers(10**8, 10**15 - 1), st.integers(0, 10**15))
def test_ids_round_trip(legacy: int, variation: int) -> None:
    item_id = f"v1|{legacy}|{variation}"
    external = from_item_id(item_id)
    assert external is not None
    assert ID_RE.fullmatch(external)
    assert source().descriptor.external_id_re.fullmatch(external)
    assert to_item_id(external) == item_id


@pytest.mark.parametrize("bad", ["v1|abc|0", "v2|123456789012|0", "123456789012", "v1|12|0", ""])
def test_from_item_id_rejects_garbage(bad: str) -> None:
    assert from_item_id(bad) is None


# --- parseo ----------------------------------------------------------------------------------


def test_parse_search_filters_real_page() -> None:
    """Abierto en caja, envío calculado sin costo y vendedores flojos quedan fuera."""
    assert parse_search(fx("search-category").body, RULES) == []


def test_parse_search_keeps_new_items_with_known_shipping() -> None:
    data = fx_json("search-category")
    for s in data["itemSummaries"]:  # type: ignore[attr-defined]
        s["conditionId"] = "1000"
    items = parse_search(json.dumps(data).encode(), RULES)
    by_id = {i.external_id: i for i in items}
    # Sin la RTX (envío calculado y 32 calificaciones) ni la tarjeta de regalo (84,6 %).
    assert set(by_id) == {"327373434819", "284061582029", "389155587813", "297819576914"}
    v100 = by_id["297819576914"]
    assert v100.price == usd(29_885)  # 295,85 + 3,00 de envío
    assert v100.shipping == usd(300)
    assert v100.seller_class is SellerClass.FIRST_PARTY
    assert v100.category_path == (
        "/Computers/Tablets & Networking/Computer Components & Parts/Graphics/Video Cards/"
    )
    assert by_id["389155587813"].price == usd(10_562)  # el precio tachado no se usa
    assert all(ID_RE.fullmatch(i.external_id) for i in items)


def test_parse_search_query_page() -> None:
    items = parse_search(fx("search-query").body, RULES)
    assert [i.external_id for i in items] == ["278384636909"]
    gpu = items[0]
    assert gpu.price == usd(71_499)
    assert gpu.model_key is None
    assert gpu.image_url == "https://i.ebayimg.com/images/g/HU0AAeSwC25qrEsQ/s-l225.jpg"
    assert gpu.web_url == "https://www.ebay.com/itm/278384636909"
    assert gpu.seller_score == 15_095


def test_parse_search_reads_epid_as_model() -> None:
    data = fx_json("search-query")
    first = data["itemSummaries"][0]  # type: ignore[index]
    first |= {"conditionId": "1000", "seller": {"feedbackPercentage": "99.5", "feedbackScore": 500}}
    items = parse_search(json.dumps(data).encode(), RULES)
    assert items[0].external_id == "840031700816"
    assert items[0].model_key == "13060624345"


def test_parse_search_drops_variants_and_calculated_shipping() -> None:
    data = fx_json("search-query")
    gpu = data["itemSummaries"][2]  # type: ignore[index]
    calc = dict(
        gpu, itemId="v1|111111111111|0", shippingOptions=[{"shippingCostType": "CALCULATED"}]
    )
    group = dict(gpu, itemId="v1|222222222222|0", itemGroupHref="https://api.ebay.com/x")
    data["itemSummaries"] = [calc, group]
    assert parse_search(json.dumps(data).encode(), RULES) == []


def test_parse_search_empty_page() -> None:
    assert parse_search(b'{"total":0,"limit":50,"offset":0}', RULES) == []


def test_parse_item_single() -> None:
    item = parse_item(fx("item-single").body, RULES)
    assert item is not None
    assert item.external_id == "206636580754"
    assert item.price == usd(65_658)  # 649,00 + 7,58 a Miami
    assert item.seller_class is SellerClass.MARKETPLACE  # 32 calificaciones: no pasa B4
    assert item.in_stock
    assert item.category_path == (
        "/Computers/Tablets & Networking/Computer Components & Parts/Graphics/Video Cards/"
    )
    assert item.image_url == "https://i.ebayimg.com/images/g/AYkAAeSwguhq1SQh/s-l1600.jpg"


def test_parse_item_lot_is_dropped() -> None:
    data = fx_json("item-single") | {"lotSize": 3}
    assert parse_item(json.dumps(data).encode(), RULES) is None


def test_parse_item_out_of_stock() -> None:
    data = fx_json("item-single")
    data["estimatedAvailabilities"] = [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]
    item = parse_item(json.dumps(data).encode(), RULES)
    assert item is not None
    assert not item.in_stock


# --- token y errores -------------------------------------------------------------------------


def test_search_sends_marketplace_headers_and_filter() -> None:
    src = source()
    api = routes(fx("search-query"), [token_resp()])
    page = src.search(ctx(paced(api, src)), spec("rtx 4070"), 0)
    assert [o.ref.external_id for o in page.offers] == ["278384636909"]
    method, path, query, headers = api.calls[0]
    assert (method, path) == ("POST", "/identity/v1/oauth2/token")
    assert headers["Authorization"] == f"Basic {BASIC}"
    assert query["grant_type"] == "client_credentials"
    assert query["scope"] == "https://api.ebay.com/oauth/api_scope"
    _, path, query, headers = api.calls[1]
    assert path == "/buy/browse/v1/item_summary/search"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert headers["X-EBAY-C-ENDUSERCTX"] == "contextualLocation=country%3DUS%2Czip%3D33166"
    assert query["q"] == "rtx 4070"
    assert query["limit"] == "50"
    assert query["filter"] == (
        "conditionIds:{1000},buyingOptions:{FIXED_PRICE},deliveryCountry:US,"
        "price:[15..],priceCurrency:USD"
    )


def test_token_fetched_once_and_renewed_on_401() -> None:
    src = source()
    api = routes(
        [fx("search-query"), fx("error-401"), fx("search-query"), fx("error-401"), fx("error-401")],
        [token_resp(TOKEN), token_resp(TOKEN2)],
    )
    c = ctx(paced(api, src))
    sp = spec("rtx 4070")
    src.search(c, sp, 0)
    src.search(c, sp, 0)  # 401 ⇒ token nuevo y reintento
    tokens = [h["Authorization"] for m, p, q, h in api.calls if p.endswith("/search")]
    assert tokens == [f"Bearer {TOKEN}", f"Bearer {TOKEN}", f"Bearer {TOKEN2}"]
    with pytest.raises(CredentialUnavailable) as info:
        src.search(c, sp, 0)  # 401 y 401 tras renovar ⇒ clave mala
    assert info.value.code == "bad_key"
    assert sum(1 for m, p, q, h in api.calls if p.endswith("/oauth2/token")) == 3


def test_token_renewed_near_expiry() -> None:
    src = source()
    api = routes(fx("search-query"), [token_resp(TOKEN, 400), token_resp(TOKEN2)])
    sp = spec("rtx 4070")
    src.search(ctx(paced(api, src), T), sp, 0)
    src.search(ctx(paced(api, src), T + timedelta(minutes=1)), sp, 0)  # vence en 5 min 40 s
    src.search(ctx(paced(api, src), T + timedelta(minutes=2)), sp, 0)  # faltan < 5 min
    tokens = [h["Authorization"] for m, p, q, h in api.calls if p.endswith("/search")]
    assert tokens == [f"Bearer {TOKEN}", f"Bearer {TOKEN}", f"Bearer {TOKEN2}"]


def test_bad_credentials_on_token_endpoint() -> None:
    src = source()
    bad = body({"error": "invalid_client"}, 401)
    api = routes(fx("search-query"), [bad])
    with pytest.raises(CredentialUnavailable) as info:
        src.search(ctx(paced(api, src)), spec("tv"), 0)
    assert info.value.code == "bad_key"


@pytest.mark.parametrize("name", ["error-quota"])
def test_quota_is_unavailable(name: str) -> None:
    src = source()
    api = routes(fx(name), [token_resp()])
    with pytest.raises(Unavailable) as info:
        src.search(ctx(paced(api, src)), spec("tv"), 0)
    assert info.value.code == "quota"


def test_quota_errorid_without_429_is_unavailable() -> None:
    resp = HttpResponse(500, "x", {}, fx("error-quota").body, 0)
    with pytest.raises(Unavailable) as info:
        source().detect_block(resp)
    assert info.value.code == "quota"


def test_access_denied_is_not_an_antibot_block() -> None:
    """El 403 de permisos (`getItems` sin acceso) no abre el breaker como un bloqueo."""
    with pytest.raises(CredentialUnavailable) as info:
        source().detect_block(fx("items-bulk-403"))
    assert info.value.code == "forbidden"


def test_token_never_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    src = source()
    api = routes([fx("error-401"), fx("search-query")], [token_resp(TOKEN), token_resp(TOKEN2)])
    src.search(ctx(paced(api, src)), spec("rtx 4070"), 0)
    text = caplog.text + repr(src)
    assert TOKEN not in text and TOKEN2 not in text and BASIC not in text


# --- seguir, buscar y catálogo (Task 6) -----------------------------------------------------

EXCLUDED = EbayParams(excluded_roots=frozenset({1}), items_per_partition=75)


def by_path(responses: Mapping[str, HttpResponse]) -> Api:
    """Token siempre válido y una respuesta por ruta (o 404 si no está)."""

    def handler(method: str, path: str, q: dict[str, str], h: Mapping[str, str]) -> HttpResponse:
        if path.endswith("/oauth2/token"):
            return token_resp()
        return responses.get(path, fx("error-404"))

    return Api(handler)


ITEM_PATH = "/buy/browse/v1/item/v1|206636580754|0"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"


def test_partitions_from_taxonomy() -> None:
    src = EbaySource(EXCLUDED)
    api = by_path({"/commerce/taxonomy/v1/category_tree/0": fx("taxonomy")})
    found = list(src.catalog_partitions(ctx(paced(api, src))))
    parts = [p.key for p in found]
    assert parts == sorted(parts, key=int)
    assert len(parts) == len(set(parts))
    assert all(p.isdigit() for p in parts)
    # Coleccionables (raíz 1) fuera; nivel 3 donde hay nietos y nivel 2 donde no los hay.
    assert not {"36", "3984", "114", "1437"} & set(parts)
    assert {"296", "4670", "15053", "309", "617", "9355", "20349", "43304"} <= set(parts)
    assert not {"1500", "9394", "293", "15032"} & set(parts)
    # Cada partición lleva la ruta de nombres desde su raíz, como la de las publicaciones.
    phones = "/Cell Phones & Accessories/Cell Phones & Smartphones/"
    assert CatalogPartition("9355", phones) in found
    radio = "/Consumer Electronics/Radio Communication/Other Radio Communication/"
    assert CatalogPartition("296", radio) in found


def test_catalog_page_observations() -> None:
    src = EbaySource(EXCLUDED)
    data = fx_json("search-query")
    first = data["itemSummaries"][0]  # type: ignore[index]
    first |= {"conditionId": "1000", "seller": {"feedbackPercentage": "99.5", "feedbackScore": 500}}
    resp = HttpResponse(200, "x", {}, json.dumps(data).encode(), 0)
    api = by_path({SEARCH_PATH: resp})
    page = src.fetch_catalog_page(ctx(paced(api, src)), "27386", 0)
    assert not page.has_more
    by_id = {o.external_id: o for o in page.items}
    assert set(by_id) == {"840031700816", "278384636909"}
    assert by_id["840031700816"].model_key == "13060624345"
    assert by_id["840031700816"].title == first["title"]
    assert by_id["278384636909"].model_key is None
    assert by_id["278384636909"].price == usd(71_499)
    assert all(o.seller_class is SellerClass.FIRST_PARTY for o in page.items)
    _, _, query, _ = api.calls[-1]
    assert (query["category_ids"], query["limit"]) == ("27386", "75")
    assert "offset" not in query or query["offset"] == "0"
    assert src.fetch_catalog_page(ctx(paced(api, src)), "27386", 1).items == ()


def test_catalog_page_rejects_bad_partition() -> None:
    src = EbaySource(EXCLUDED)
    with pytest.raises(ParseError):
        src.fetch_catalog_page(ctx(paced(by_path({}), src)), "27386&q=x", 0)


def test_fetch_listings_gone_and_found() -> None:
    src = source()
    api = by_path({ITEM_PATH: fx("item-single")})
    refs = [ListingRef(CODE, "206636580754"), ListingRef(CODE, "111111111111")]
    out = src.fetch_listings(ctx(paced(api, src)), refs)
    assert isinstance(out[0], ListingFound)
    offer = out[0].offer
    assert offer.price == usd(65_658)
    assert offer.list_price is None
    assert offer.url_path == "/itm/206636580754"
    assert isinstance(out[1], ListingGone)


def test_seguir_low_feedback_is_marketplace() -> None:
    src = source()
    out = src.fetch_listings(
        ctx(paced(by_path({ITEM_PATH: fx("item-single")}), src)), [ListingRef(CODE, "206636580754")]
    )
    assert isinstance(out[0], ListingFound)
    assert out[0].offer.seller_class is SellerClass.MARKETPLACE


def test_seguir_variant_uses_item_id_with_variation() -> None:
    src = source()
    path = "/buy/browse/v1/item/v1|157858848208|459292793708"
    data = fx_json("item-single") | {"itemId": "v1|157858848208|459292793708"}
    resp = HttpResponse(200, "x", {}, json.dumps(data).encode(), 0)
    out = src.fetch_listings(
        ctx(paced(by_path({path: resp}), src)), [ListingRef(CODE, "157858848208V459292793708")]
    )
    assert isinstance(out[0], ListingFound)
    assert out[0].offer.parent_external_id == "157858848208"
    assert out[0].offer.url_path == "/itm/157858848208?var=459292793708"


def test_fetch_listings_failure_is_reported() -> None:
    src = source()
    api = by_path({ITEM_PATH: HttpResponse(200, "x", {}, b"no-json", 0)})
    out = src.fetch_listings(ctx(paced(api, src)), [ListingRef(CODE, "206636580754")])
    assert isinstance(out[0], ListingFailed)


def test_search_one_page_of_offers() -> None:
    src = source()
    api = by_path({SEARCH_PATH: fx("search-query")})
    page = src.search(ctx(paced(api, src)), spec("rtx 4070"), 3)
    assert [o.ref.external_id for o in page.offers] == ["278384636909"]
    assert page.offers[0].seller_class is SellerClass.FIRST_PARTY
    assert sum(1 for c in api.calls if c[1] == SEARCH_PATH) == 1


def test_confirm_uses_single_item() -> None:
    src = source()
    api = by_path({ITEM_PATH: fx("item-single")})
    offers = src.fetch_listings(ctx(paced(api, src)), [ListingRef(CODE, "206636580754")])
    assert isinstance(offers[0], ListingFound)
    again = src.confirm(ctx(paced(api, src)), offers[0].offer)
    assert again is not None and again.price == usd(65_658)
    assert api.calls[-1][1] == ITEM_PATH
    gone = src.confirm(ctx(paced(by_path({}), src)), offers[0].offer)
    assert gone is None


def test_catalog_items_and_card_facts() -> None:
    src = source()
    api = by_path({ITEM_PATH: fx("item-single")})
    obs = src.fetch_catalog_items(ctx(paced(api, src)), ["206636580754", "111111111111"])
    assert [o.external_id for o in obs] == ["206636580754"]
    assert obs[0].price == usd(65_658)
    facts = src.fetch_card_facts(ctx(paced(api, src)), ["206636580754", "111111111111"])
    assert set(facts) == {"206636580754"}
    card = facts["206636580754"]
    assert card.url == "https://www.ebay.com/itm/206636580754"
    assert card.image_url == "https://i.ebayimg.com/images/g/AYkAAeSwguhq1SQh/s-l1600.jpg"
    assert card.note == "vendedor 100 % (32) · incluye envío a Miami US$7,58"


def test_card_note_free_shipping_and_thousands() -> None:
    data = fx_json("item-single")
    data["seller"] = {"feedbackPercentage": "99.6", "feedbackScore": 12345}
    data["shippingOptions"] = [{"shippingCost": {"value": "0.00", "currency": "USD"}}]
    resp = HttpResponse(200, "x", {}, json.dumps(data).encode(), 0)
    src = source()
    facts = src.fetch_card_facts(ctx(paced(by_path({ITEM_PATH: resp}), src)), ["206636580754"])
    assert facts["206636580754"].note == "vendedor 99,6 % (12.345)"


def test_changed_and_resolve_are_not_supported() -> None:
    src = source()
    assert src.fetch_changed(ctx(by_path({})), T, 0).items == ()
    assert not src.descriptor.supports_changed
    assert src.fetch_leads(ctx(by_path({}))) == []


def test_registered_with_repo_params() -> None:
    from discountz.sources.registry import load_all

    registered = load_all()[CODE]
    assert isinstance(registered, EbaySource)
    assert 1 in registered.params.excluded_roots


def test_fetch_listings_stops_at_quota() -> None:
    src = source()
    api = by_path({ITEM_PATH: fx("error-quota")})
    refs = [ListingRef(CODE, "206636580754"), ListingRef(CODE, "206636580755")]
    out = src.fetch_listings(ctx(paced(api, src)), refs)
    assert all(isinstance(o, ListingFailed) for o in out)
    assert sum(1 for c in api.calls if c[1].startswith("/buy/browse/v1/item/")) == 1


def test_nan_feedback_is_not_first_party() -> None:
    data = fx_json("item-single")
    data["seller"] = {"feedbackPercentage": "NaN", "feedbackScore": 500}
    item = parse_item(json.dumps(data).encode(), RULES)
    assert item is not None
    assert item.seller_class is SellerClass.MARKETPLACE
