"""Rastreo de eBay de punta a punta: BD real, adaptador real y la Browse API con fixtures (Task 8).

Una corrida de `crawl` da una alerta por bajada de la misma publicación (§4.1), otra por precio bajo
frente al mismo modelo (§4.2) y ninguna por la publicación de un vendedor bajo el umbral (B4).
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import psycopg
import pytest
from pydantic import SecretStr

from discountz.app.crawl import CrawlDeps, crawl_max_items, run_crawl
from discountz.app.render import CASILLERO
from discountz.config import load_params, load_topics
from discountz.domain.catalog import CatalogState
from discountz.domain.errors import CredentialUnavailable
from discountz.infra import db as repo
from discountz.infra.db import CatalogRow
from discountz.infra.http import Files, HttpResponse, Method, Params
from discountz.sources.ebay import CODE, EbaySource
from discountz.sources.fixtures import load_fixture
from tests.integration.conftest import db, empty_db_url  # noqa: F401

pytestmark = pytest.mark.integration

T = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
FIRST_PARTITION = "296"  # la menor del recorte del árbol sin la raíz 1
DROP = "910000000001"  # §4.1: venía a USD 100 tres días; ahora USD 79
MODEL_ITEM = "910000000002"  # §4.2: USD 80 frente a 5 publicaciones de USD 100 del modelo
WEAK = "910000000003"  # vendedor con 50 calificaciones: fuera por B4
MODEL = "55555555"
PRICES = {DROP: "79.00", MODEL_ITEM: "80.00", WEAK: "50.00"}


class Creds:
    def bearer(self, provider: str) -> SecretStr:
        return SecretStr("QmFzaWM=")

    def signed_headers(self, provider: str, now: datetime) -> Mapping[str, str]:
        raise CredentialUnavailable(provider, "missing")

    def public_id(self, provider: str) -> str:
        raise CredentialUnavailable(provider, "missing")


def jresp(obj: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, "https://api.ebay.com/x", {}, json.dumps(obj).encode(), 1)


def summary(ext: str, *, score: int = 5_000, epid: str | None = None) -> dict[str, object]:
    base: dict[str, object] = json.loads(load_fixture(CODE, "search-query").body)
    item = dict(base["itemSummaries"][2])  # type: ignore[index]
    item |= {
        "itemId": f"v1|{ext}|0",
        "legacyItemId": ext,
        "price": {"value": PRICES[ext], "currency": "USD"},
        "seller": {"feedbackPercentage": "99.8", "feedbackScore": score},
        "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}],
    }
    if epid:
        item["epid"] = epid
    return item


def detail(ext: str) -> dict[str, object]:
    item: dict[str, object] = json.loads(load_fixture(CODE, "item-single").body)
    item |= {
        "itemId": f"v1|{ext}|0",
        "price": {"value": PRICES[ext], "currency": "USD"},
        "seller": {"feedbackPercentage": "99.8", "feedbackScore": 5_000},
        "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}],
    }
    return item


@dataclass
class EbayApi:
    """La Browse API falsa: token, árbol, búsqueda por categoría y fichas por id."""

    t: float = 0.0
    paths: list[str] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

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
        self.t += 1
        path = unquote(urlsplit(url).path)
        query = dict(params.items()) if isinstance(params, Mapping) else dict(params or [])
        self.paths.append(path)
        if path.endswith("/oauth2/token"):
            return jresp({"access_token": "TKN", "expires_in": 7200})
        if path.endswith("/category_tree/0"):
            return load_fixture(CODE, "taxonomy")
        if path.endswith("/item_summary/search"):
            if query.get("category_ids") != FIRST_PARTITION:
                return jresp({"total": 0, "limit": 75, "offset": 0})
            items = [summary(DROP), summary(MODEL_ITEM, epid=MODEL), summary(WEAK, score=50)]
            return jresp({"total": 3, "limit": 75, "offset": 0, "itemSummaries": items})
        ext = path.rsplit("|", 2)[-2] if "|" in path else ""
        if ext in PRICES:
            return jresp(detail(ext))
        return load_fixture(CODE, "error-404")


def seed(conn: psycopg.Connection, ext: str, price: int, model: str | None) -> None:
    start = T - timedelta(days=3)
    state = CatalogState(price, price, price, start, start, 30, None, None, None)
    row = CatalogRow(ext, None, "tecnologia", "USD", state, model_key=model)
    repo.save_catalog_page(conn, CODE, [row], T - timedelta(hours=2))


def test_ebay_crawl_end_to_end(db: psycopg.Connection) -> None:  # noqa: F811
    seed(db, DROP, 10_000, None)
    for i in range(5):
        seed(db, f"92000000000{i}", 10_000, MODEL)
    params = load_params()
    api = EbayApi()
    deps = CrawlDeps(
        conn=db,
        source=EbaySource(params.ebay),
        http=api,
        credentials=Creds(),
        topics=load_topics(),
        params=params,
        budget=params.crawl[CODE],
        holder="gh:1:1",
        monotonic=api.monotonic,
        sleep=api.sleep,
        rnd=random.Random(1),
        max_items=crawl_max_items(CODE, params),
    )
    result = run_crawl(deps, T, minutes=45)
    assert result.status == "ok"
    assert result.alerts == 2
    rows = {r.card["kind"]: r for r in repo.claim_outbox(db, 10, T) if r.card is not None}
    assert set(rows) == {"catalog_drop", "model_low"}
    drop, model = rows["catalog_drop"], rows["model_low"]
    assert drop.card is not None and model.card is not None
    assert (drop.card["price_minor"], drop.card["usual_minor"]) == (7_900, 10_000)
    assert drop.card["note"] == "vendedor 99,8 % (5.000)"
    assert model.card["reference_label"] == "Mediana de 5 publicaciones del mismo modelo"
    assert drop.topic == model.topic == "tecnologia"
    for r in (drop, model):
        assert CASILLERO in r.body_html
        assert "Abrir en eBay" in json.dumps(r.reply_markup)
    states = repo.load_catalog_states(db, CODE, [WEAK, MODEL_ITEM])
    assert set(states) == {MODEL_ITEM}  # la del vendedor flojo ni se guarda
    assert repo.load_breakers(db)[CODE].state == "closed"
