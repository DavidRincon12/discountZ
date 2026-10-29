"""Mercado Libre por la API de su catálogo (spec de Mercado Libre, ADR 0018).

Fixtures reales grabadas desde Actions el 2026-10-29 (recortadas, ver `"recorte"`).
"""

from __future__ import annotations

import gzip
import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr

from discountz.domain.catalog import CatalogObservation
from discountz.domain.errors import CredentialUnavailable, HardBlock, ParseError, Unavailable
from discountz.domain.model import ListingRef, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.scheduling import SourceBudget
from discountz.domain.urls import SafeUrl
from discountz.infra.http import Files, HttpResponse, Method, PacedClient, Params
from discountz.sources.base import (
    Capability,
    CardFacts,
    EanCatalogSource,
    ListingFailed,
    ListingFound,
    ListingGone,
    NeedsResolution,
    SourceContext,
)
from discountz.sources.fixtures import FIXTURES_DIR, load_fixture
from discountz.sources.mercadolibre import (
    API,
    CODE,
    MeliSeller,
    MercadoLibreSource,
    parse_leader,
    parse_product,
    parse_search,
    parse_sellers,
)

_FIXTURES = (
    "search-ean",
    "search-ean-no-zeros",
    "search-empty",
    "items",
    "items-none",
    "product",
    "user",
)
_NOT_OFFICIAL = {272545453, 324693682, 3339126792, 222480760, 1200436618, 629159107}


def _body(name: str) -> bytes:
    return load_fixture("mercadolibre", name).body


def _edit(name: str, change: Callable[[dict[str, object]], object]) -> bytes:
    """La fixture con su JSON modificado por `change(datos)`."""
    data = json.loads(_body(name))
    change(data)
    return json.dumps(data).encode()


def _rows(data: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = data["results"]  # type: ignore[assignment]
    return rows


def _first_picture(url: str) -> Callable[[dict[str, object]], None]:
    def change(data: dict[str, object]) -> None:
        pictures: list[dict[str, object]] = data["pictures"]  # type: ignore[assignment]
        pictures[0]["url"] = url

    return change


def _reputation(status: str | None, level: str | None) -> bytes:
    def change(data: dict[str, object]) -> None:
        reputation: dict[str, object] = data["seller_reputation"]  # type: ignore[assignment]
        reputation.update(power_seller_status=status, level_id=level)

    return _edit("user", change)


def _code(call: Callable[[], object]) -> str:
    with pytest.raises(ParseError) as err:
        call()
    return err.value.code


def test_fixtures_are_recorded_without_token() -> None:
    for name in _FIXTURES:
        raw = gzip.decompress((FIXTURES_DIR / "mercadolibre" / f"{name}.json.gz").read_bytes())
        assert isinstance(json.loads(raw), dict)
        assert b"access_token" not in raw


def test_parse_search_returns_first_product_id() -> None:
    assert parse_search(_body("search-ean")) == "MCO39410080"
    assert parse_search(_body("search-empty")) is None
    other_site = _edit("search-ean", lambda d: _rows(d).insert(0, {"id": "MLA123"}))
    assert parse_search(other_site) == "MCO39410080"
    assert _code(lambda: parse_search(b"<html>")) == "bad_search"


def test_parse_sellers_orders_eligible_by_price() -> None:
    sellers = parse_sellers(_body("items"))
    prices = [s.price.minor for s in sellers]
    assert len(sellers) == 14
    assert prices == sorted(prices)
    assert all(s.price.currency is Currency.COP for s in sellers)
    assert sellers[0] == MeliSeller(
        297702699, Money(439900, Currency.COP), official=True, free_shipping=True
    )
    assert {s.seller_id for s in sellers if not s.official} == _NOT_OFFICIAL


def test_parse_sellers_skips_used_international_and_other_currency() -> None:
    def change(data: dict[str, object]) -> None:
        rows = _rows(data)
        rows[0]["condition"] = "used"
        rows[1]["international_delivery_mode"] = "CBT"
        rows[2]["currency_id"] = "USD"

    sellers = parse_sellers(_edit("items", change))
    assert len(sellers) == 11
    assert {s.seller_id for s in sellers}.isdisjoint({297702699, 205607103, 272545453})


def test_parse_sellers_returns_nothing_when_no_seller_is_eligible() -> None:
    def change(data: dict[str, object]) -> None:
        for row in _rows(data):
            row["condition"] = "used"

    assert parse_sellers(_edit("items", change)) == []


def test_parse_sellers_skips_an_invalid_entry() -> None:
    def change(data: dict[str, object]) -> None:
        rows = _rows(data)
        del rows[0]["seller_id"]
        rows[1]["price"] = "457424"

    sellers = parse_sellers(_edit("items", change))
    assert len(sellers) == 12
    assert 205607103 not in {s.seller_id for s in sellers}
    assert sellers[0].price == Money(485000, Currency.COP)


def test_parse_sellers_skips_zero_and_absurd_prices() -> None:
    def change(data: dict[str, object]) -> None:
        rows = _rows(data)
        rows[0]["price"] = 0
        rows[1]["price"] = 1e30

    sellers = parse_sellers(_edit("items", change))
    assert len(sellers) == 12
    assert {s.seller_id for s in sellers}.isdisjoint({297702699, 205607103})


def test_parse_sellers_rounds_decimal_prices() -> None:
    def change(data: dict[str, object]) -> None:
        _rows(data)[1]["price"] = 457424.5

    sellers = parse_sellers(_edit("items", change))
    assert next(s for s in sellers if s.seller_id == 205607103).price == Money(457425, Currency.COP)


def test_parse_sellers_rejects_a_body_without_results() -> None:
    assert _code(lambda: parse_sellers(b"{}")) == "bad_items"
    assert _code(lambda: parse_sellers(b"<html>")) == "bad_items"
    assert _code(lambda: parse_sellers(b"[" * 100_000)) == "bad_items"


def test_parse_sellers_rejects_when_no_entry_validates() -> None:
    def change(data: dict[str, object]) -> None:
        for row in _rows(data):
            row["price"] = "439900"

    assert _code(lambda: parse_sellers(_edit("items", change))) == "bad_items"


def test_parse_product_reads_name_and_first_cdn_picture() -> None:
    product = parse_product(_body("product"))
    assert product.name.startswith("Taladro/Atornillador")
    assert product.image_url == (
        "https://http2.mlstatic.com/D_NQ_NP_939054-MLA100928655228_122025-F.jpg"
    )
    for url in ("https://evil.example/x.jpg", "http://http2.mlstatic.com/x.jpg"):
        assert parse_product(_edit("product", _first_picture(url))).image_url is None
    assert _code(lambda: parse_product(_edit("product", lambda d: d.pop("name")))) == "bad_product"
    assert _code(lambda: parse_product(_edit("product", lambda d: d.update(name=" ")))) == (
        "bad_product"
    )


def test_parse_leader_only_platinum_or_gold_in_green() -> None:
    assert parse_leader(_reputation("platinum", "5_green")) == "platinum"
    assert parse_leader(_reputation("gold", "5_green")) == "gold"
    for status, level in (("silver", "5_green"), ("platinum", "4_light_green"), (None, None)):
        assert parse_leader(_reputation(status, level)) is None
    assert parse_leader(_body("user")) is None
    assert _code(lambda: parse_leader(b"<html>")) == "bad_user"


# --- la fuente (Task 5) ---------------------------------------------------------------------

T = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
TOKEN = "TKN-uno"  # noqa: S105 - falso, de prueba
TOKEN2 = "TKN-dos"
SECRET = "SECRETO"  # noqa: S105 - falso, de prueba
PRODUCT = "MCO18924972"
REF = ListingRef(CODE, PRODUCT)
TOKEN_ROUTE = "POST /oauth/token"  # noqa: S105 - una ruta, no un secreto
ITEMS = f"GET /products/{PRODUCT}/items"
PRODUCT_ROUTE = f"GET /products/{PRODUCT}"
WEB_HOSTS = frozenset({"www.mercadolibre.com.co"})
PHOTO = "https://http2.mlstatic.com/D_NQ_NP_939054-MLA100928655228_122025-F.jpg"
CHEAP = 272545453  # el primer vendedor sin tienda oficial de la fixture `items`

Route = HttpResponse | list[HttpResponse]
Change = Callable[[dict[str, object]], None]


class Creds:
    def bearer(self, provider: str) -> SecretStr:
        assert provider == CODE
        return SecretStr(SECRET)

    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]:
        raise CredentialUnavailable(provider, "missing")

    def public_id(self, provider: str) -> str:
        assert provider == CODE
        return "ID-APP"


@dataclass
class Api:
    """HttpClient falso: responde por método y ruta (la búsqueda, también por EAN) y anota todo.

    El token responde siempre si la prueba no da otro; una ruta sin respuesta da 404, como la API.
    """

    routes: dict[str, Route]
    calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = field(default_factory=list)

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
        query = dict(pairs) | dict(data or {})
        path = urlsplit(url).path
        self.calls.append((method, path, query, dict(headers or {})))
        key = f"{method} {path}"
        if path == "/products/search":
            key += f"?{query.get('product_identifier')}"
        route = self.routes.get(key)
        if route is None:
            route = token() if key == TOKEN_ROUTE else resp({"status": 404}, 404)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        return replace(route, url=url)

    def paths(self, prefix: str) -> list[str]:
        return [p for m, p, q, h in self.calls if p.startswith(prefix)]


def resp(obj: object, status: int = 200) -> HttpResponse:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return HttpResponse(status, API, {}, raw, 0)


def token(value: str = TOKEN, expires_in: int = 21600) -> HttpResponse:
    return resp({"access_token": value, "token_type": "Bearer", "expires_in": expires_in})


def fx(name: str) -> HttpResponse:
    return load_fixture(CODE, name)


def items(*changes: Change) -> HttpResponse:
    """Los vendedores de la fixture `items` con cada cambio aplicado."""

    def change(data: dict[str, object]) -> None:
        for apply in changes:
            apply(data)

    return resp(_edit("items", change))


def seller(seller_id: int, **fields: object) -> Change:
    def change(data: dict[str, object]) -> None:
        next(r for r in _rows(data) if r["seller_id"] == seller_id).update(fields)

    return change


def every_seller(**fields: object) -> Change:
    def change(data: dict[str, object]) -> None:
        for row in _rows(data):
            row.update(fields)

    return change


def user(status: str | None, level: str | None) -> HttpResponse:
    return resp(_reputation(status, level))


def paced(api: Api, src: MercadoLibreSource) -> PacedClient:
    budget = SourceBudget(CODE, 0, 0, 100, 1000, timedelta(hours=2), timedelta(days=1), 1)
    return PacedClient(
        api, budget, lambda s: None, lambda: 0.0, random.Random(0), src.detect_block, source=CODE
    )


def ctx(http: object, now: datetime = T) -> SourceContext:
    return SourceContext(http, Creds(), 1e9, now)  # type: ignore[arg-type]


def setup(routes: dict[str, Route]) -> tuple[MercadoLibreSource, Api, SourceContext]:
    src = MercadoLibreSource()
    api = Api(routes)
    return src, api, ctx(paced(api, src))


def cop(minor: int) -> Money:
    return Money(minor, Currency.COP)


def test_descriptor_follows_the_spec() -> None:
    src = MercadoLibreSource()
    d = src.descriptor
    assert (d.code, d.kind, d.currency, d.client, d.max_batch, d.auth) == (
        "mercadolibre",
        "store",
        Currency.COP,
        "httpx",
        1,
        "oauth_client",
    )
    assert d.hosts == WEB_HOSTS
    assert d.image_hosts == frozenset({"http2.mlstatic.com"})
    assert d.capabilities == frozenset({Capability.DETAIL, Capability.CONFIRM, Capability.CATALOG})
    assert d.external_id_re.fullmatch(PRODUCT)
    assert not d.external_id_re.fullmatch("MLA18924972")
    assert isinstance(src, EanCatalogSource)


def test_contract_parts_without_use_are_empty() -> None:
    src, api, c = setup({})
    assert not src.search(c, SearchSpec(CODE, "taladro", (), None, None, "ferreteria"), 0).offers
    assert not src.catalog_partitions(c)
    assert not src.fetch_catalog_page(c, "x", 0).items
    assert not src.fetch_changed(c, T, 0).items
    assert src.fetch_leads(c) == []
    assert _code(lambda: src.resolve(c, NeedsResolution(CODE, "slug:x"))) == "no_resolution"
    assert api.calls == []


def test_token_is_fetched_once_and_sent_as_bearer() -> None:
    src, api, c = setup({ITEMS: fx("items")})
    src.fetch_catalog_items(c, [PRODUCT])
    src.fetch_catalog_items(c, [PRODUCT])
    posts = [(p, q) for m, p, q, h in api.calls if m == "POST"]
    form = {"grant_type": "client_credentials", "client_id": "ID-APP", "client_secret": SECRET}
    assert posts == [("/oauth/token", form)]
    bearers = [h["Authorization"] for m, p, q, h in api.calls if m == "GET"]
    assert bearers == [f"Bearer {TOKEN}", f"Bearer {TOKEN}"]
    assert TOKEN not in repr(src)
    assert SECRET not in repr(src)


def test_token_renews_ten_minutes_before_expiry() -> None:
    src = MercadoLibreSource()
    api = Api({TOKEN_ROUTE: [token(TOKEN), token(TOKEN2)], ITEMS: fx("items")})
    http = paced(api, src)
    expiry = T + timedelta(hours=6)
    for now in (T, expiry - timedelta(minutes=11), expiry - timedelta(minutes=9)):
        src.fetch_catalog_items(ctx(http, now), [PRODUCT])
    bearers = [h["Authorization"] for m, p, q, h in api.calls if m == "GET"]
    assert bearers == [f"Bearer {TOKEN}", f"Bearer {TOKEN}", f"Bearer {TOKEN2}"]


def test_401_renews_once_then_bad_key() -> None:
    denied = resp({"message": "invalid access token", "status": 401}, 401)
    src, api, c = setup(
        {
            TOKEN_ROUTE: [token(TOKEN), token(TOKEN2), token("TKN-tres")],
            ITEMS: [denied, fx("items"), denied, denied],
        }
    )
    assert src.fetch_catalog_items(c, [PRODUCT])  # 401 ⇒ token nuevo y la lectura sigue
    with pytest.raises(CredentialUnavailable) as err:
        src.fetch_catalog_items(c, [PRODUCT])  # 401 también con el token nuevo
    assert err.value.code == "bad_key"
    assert len([m for m, p, q, h in api.calls if m == "POST"]) == 3


@pytest.mark.parametrize("status", [400, 401])
def test_rejected_app_credentials_are_bad_key(status: int) -> None:
    src, _api, c = setup({TOKEN_ROUTE: resp({"error": "invalid_client"}, status)})
    with pytest.raises(CredentialUnavailable) as err:
        src.fetch_catalog_items(c, [PRODUCT])
    assert err.value.code == "bad_key"


def test_find_by_ean_retries_without_leading_zeros() -> None:
    src, api, c = setup(
        {
            "GET /products/search?0740617344806": fx("search-empty"),
            "GET /products/search?740617344806": fx("search-ean"),
            "GET /products/search?6921732899394": fx("search-empty"),
        }
    )
    assert src.find_by_ean(c, "0740617344806") == "MCO39410080"
    assert src.find_by_ean(c, "6921732899394") is None
    searched = [q for m, p, q, h in api.calls if p == "/products/search"]
    assert [q["product_identifier"] for q in searched] == [
        "0740617344806",
        "740617344806",
        "6921732899394",
    ]
    assert all((q["status"], q["site_id"]) == ("active", "MCO") for q in searched)


def test_find_by_ean_retries_with_the_twelve_digit_upc() -> None:
    src, api, c = setup(
        {
            "GET /products/search?0045496590420": fx("search-empty"),
            "GET /products/search?045496590420": fx("search-ean"),
            "GET /products/search?00740617344806": fx("search-empty"),
            "GET /products/search?740617344806": fx("search-ean"),
        }
    )
    assert src.find_by_ean(c, "0045496590420") == "MCO39410080"  # UPC-A del sistema 0
    assert src.find_by_ean(c, "00740617344806") == "MCO39410080"  # GTIN-14
    searched = [q["product_identifier"] for m, p, q, h in api.calls if p == "/products/search"]
    assert searched == ["0045496590420", "045496590420", "00740617344806", "740617344806"]


@pytest.mark.parametrize("status", [400, 404])
def test_find_by_ean_400_is_parse_error(status: int) -> None:
    bad = resp({"message": "invalid product_identifier", "status": status}, status)
    src, _api, c = setup({"GET /products/search?7702004003508": bad})
    assert _code(lambda: src.find_by_ean(c, "7702004003508")) == "bad_request"


def test_official_store_wins_without_user_lookup() -> None:
    src, api, c = setup({ITEMS: fx("items")})
    assert src.fetch_catalog_items(c, [PRODUCT]) == [
        CatalogObservation(PRODUCT, cop(439900), "", None, SellerClass.FIRST_PARTY)
    ]
    assert api.paths("/users/") == []


def test_green_leader_is_marketplace() -> None:
    src, _api, c = setup(
        {
            ITEMS: items(seller(CHEAP, price=400000)),
            f"GET /users/{CHEAP}": user("platinum", "5_green"),
        }
    )
    [obs] = src.fetch_catalog_items(c, [PRODUCT])
    assert (obs.price, obs.seller_class) == (cop(400000), SellerClass.MARKETPLACE)


@pytest.mark.parametrize(("status", "level"), [("platinum", "1_red"), ("silver", "5_green")])
def test_red_or_silver_seller_is_skipped(status: str, level: str) -> None:
    src, _api, c = setup(
        {ITEMS: items(seller(CHEAP, price=400000)), f"GET /users/{CHEAP}": user(status, level)}
    )
    [obs] = src.fetch_catalog_items(c, [PRODUCT])
    assert (obs.price, obs.seller_class) == (cop(439900), SellerClass.FIRST_PARTY)


def test_seller_lookups_stop_at_three_per_product() -> None:
    first, second, third, fourth = 272545453, 324693682, 3339126792, 222480760
    cheapest = (seller(s, price=100000 + i) for i, s in enumerate((first, second, third, fourth)))
    routes: dict[str, Route] = {ITEMS: items(*cheapest)}
    routes |= {f"GET /users/{s}": user("silver", "5_green") for s in (first, second, third)}
    routes[f"GET /users/{fourth}"] = user("platinum", "5_green")  # pasado el tope: no se consulta
    src, api, c = setup(routes)
    [obs] = src.fetch_catalog_items(c, [PRODUCT])
    assert api.paths("/users/") == [f"/users/{s}" for s in (first, second, third)]
    assert (obs.price, obs.seller_class) == (cop(439900), SellerClass.FIRST_PARTY)


def test_seller_reputation_is_cached() -> None:
    other = "MCO99"
    body = items(seller(CHEAP, price=400000))
    src, api, c = setup(
        {
            ITEMS: body,
            f"GET /products/{other}/items": body,
            f"GET /users/{CHEAP}": user("gold", "5_green"),
        }
    )
    assert len(src.fetch_catalog_items(c, [PRODUCT, other])) == 2
    assert api.paths("/users/") == [f"/users/{CHEAP}"]


@pytest.mark.parametrize("status", [403, 404])
def test_user_403_or_404_skips_the_seller_without_blocking(status: int) -> None:
    src = MercadoLibreSource()
    denied = resp({"blocked_by": "PolicyAgent", "status": status}, status)
    api = Api({ITEMS: items(seller(CHEAP, price=400000)), f"GET /users/{CHEAP}": denied})
    http = paced(api, src)
    [obs] = src.fetch_catalog_items(ctx(http), [PRODUCT])
    assert obs.seller_class is SellerClass.FIRST_PARTY
    assert http.blocked is None


@pytest.mark.parametrize("status", [400, 410])
def test_user_4xx_skips_the_seller_once(status: int) -> None:
    other = "MCO99"
    body = items(seller(CHEAP, price=400000))
    bad = resp({"message": "bad request", "status": status}, status)
    src, api, c = setup(
        {ITEMS: body, f"GET /products/{other}/items": body, f"GET /users/{CHEAP}": bad}
    )
    found = src.fetch_catalog_items(c, [PRODUCT, other])
    assert [o.seller_class for o in found] == [SellerClass.FIRST_PARTY] * 2
    assert api.paths("/users/") == [f"/users/{CHEAP}"]  # el perfil roto no se pide otra vez


def test_user_5xx_fails_the_read() -> None:
    down = resp({"message": "internal error", "status": 500}, 500)
    src, _api, c = setup({ITEMS: items(seller(CHEAP, price=400000)), f"GET /users/{CHEAP}": down})
    with pytest.raises(Unavailable):  # pasajero: no se anota como «no es líder»
        src.fetch_catalog_items(c, [PRODUCT])


def test_unreadable_user_skips_the_seller() -> None:
    src, _api, c = setup(
        {ITEMS: items(seller(CHEAP, price=400000)), f"GET /users/{CHEAP}": resp(b"<html>")}
    )
    [obs] = src.fetch_catalog_items(c, [PRODUCT])
    assert (obs.price, obs.seller_class) == (cop(439900), SellerClass.FIRST_PARTY)


def test_items_404_gives_no_observation() -> None:
    src, _api, c = setup({ITEMS: fx("items-none")})
    assert src.fetch_catalog_items(c, [PRODUCT]) == []


def test_foreign_ids_are_skipped_without_requests() -> None:
    src, api, c = setup({})
    assert src.fetch_catalog_items(c, ["MLA123", "../users/1", ""]) == []
    assert api.calls == []


def test_policy_403_on_items_is_hard_block() -> None:
    src, _api, c = setup({ITEMS: resp({"blocked_by": "PolicyAgent", "status": 403}, 403)})
    with pytest.raises(HardBlock):
        src.fetch_catalog_items(c, [PRODUCT])


def test_catalog_listings_found_gone_failed() -> None:
    src, api, c = setup({ITEMS: fx("items"), PRODUCT_ROUTE: fx("product")})
    [found] = src.fetch_catalog_listings(c, [REF])
    assert isinstance(found, ListingFound)
    offer = found.offer
    assert (offer.ref, offer.title) == (REF, parse_product(_body("product")).name)
    assert (offer.price, offer.seller_class) == (cop(439900), SellerClass.FIRST_PARTY)
    assert (offer.url_path, offer.list_price, offer.in_stock, offer.unit_key) == (
        f"/p/{PRODUCT}",
        None,
        True,
        "un|1",
    )
    assert src.fetch_listings(c, [REF]) == [found]
    for body in (fx("items-none"), items(every_seller(condition="used"))):
        src, api, c = setup({ITEMS: body, PRODUCT_ROUTE: fx("product")})
        assert src.fetch_catalog_listings(c, [REF]) == [ListingGone(REF)]
        assert api.paths(f"/products/{PRODUCT}") == [f"/products/{PRODUCT}/items"]
    src, api, c = setup({ITEMS: resp(b"<html>"), PRODUCT_ROUTE: fx("product")})
    [failed] = src.fetch_catalog_listings(c, [REF])
    assert isinstance(failed, ListingFailed)
    assert isinstance(failed.error, ParseError)


def test_confirm_reads_the_listing_again() -> None:
    src, _api, c = setup({ITEMS: fx("items"), PRODUCT_ROUTE: fx("product")})
    [found] = src.fetch_listings(c, [REF])
    assert isinstance(found, ListingFound)
    assert src.confirm(c, found.offer) == found.offer
    src, _api, c = setup({ITEMS: fx("items-none")})
    assert src.confirm(c, found.offer) is None
    src, _api, c = setup({ITEMS: resp(b"<html>")})
    assert _code(lambda: src.confirm(c, found.offer)) == "bad_items"


def test_card_facts_reuse_the_confirmation() -> None:
    src, api, c = setup({ITEMS: fx("items"), PRODUCT_ROUTE: fx("product")})
    src.fetch_catalog_listings(c, [REF])
    before = len(api.calls)
    name = parse_product(_body("product")).name
    assert src.fetch_card_facts(c, [PRODUCT]) == {
        PRODUCT: CardFacts(
            name,
            "https://www.mercadolibre.com.co/p/MCO18924972",
            PHOTO,
            note="Tienda oficial · Envío gratis",
        )
    }
    assert len(api.calls) == before
    leader = items(seller(CHEAP, price=400000, shipping={"free_shipping": False}))
    src, api, c = setup(
        {
            ITEMS: leader,
            PRODUCT_ROUTE: fx("product"),
            f"GET /users/{CHEAP}": user("platinum", "5_green"),
        }
    )
    src.fetch_catalog_listings(c, [REF])
    assert src.fetch_card_facts(c, [PRODUCT])[PRODUCT].note == "MercadoLíder Platinum"


def test_card_facts_without_confirmation_reads_again() -> None:
    src, api, c = setup({ITEMS: fx("items"), PRODUCT_ROUTE: fx("product")})
    facts = src.fetch_card_facts(c, [PRODUCT])
    assert facts[PRODUCT].image_url == PHOTO
    assert sorted(api.paths("/products/")) == [f"/products/{PRODUCT}", f"/products/{PRODUCT}/items"]


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.mercadolibre.com.co/taladro-dewalt/p/MCO18924972",
        "https://www.mercadolibre.com.co/p/MCO18924972?pdp_filters=x",
    ],
)
def test_parse_url_reads_product_links(raw: str) -> None:
    assert MercadoLibreSource().parse_url(SafeUrl.parse(raw, WEB_HOSTS)) == REF


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.mercadolibre.com.co/ofertas",
        "https://www.mercadolibre.com.co/p/MLA18924972",
        "https://www.mercadolibre.com.co/a/b/p/MCO18924972",
    ],
)
def test_parse_url_rejects_other_pages(raw: str) -> None:
    assert MercadoLibreSource().parse_url(SafeUrl.parse(raw, WEB_HOSTS)) is None
