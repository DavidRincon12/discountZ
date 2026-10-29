"""Mercado Libre de punta a punta (spec de Mercado Libre §1.3): una fila de Éxito siembra la
búsqueda por EAN y el adaptador real lee las fixtures grabadas de la API, contra Postgres."""

from __future__ import annotations

import random

import psycopg
import pytest

from discountz.app.crawl import CrawlDeps, run_crawl
from discountz.config import load_params
from discountz.domain.scheduling import CrawlBudget
from discountz.sources.mercadolibre import MercadoLibreSource
from tests.integration.test_crawl import Clock, T
from tests.integration.test_crawl_seeds import HOUR, lookups, store_row
from tests.profiles import spec_profile
from tests.sources.test_mercadolibre import Api, Creds, fx, resp

pytestmark = pytest.mark.integration
EAN = "0740617344806"  # el de la fixture search-ean
PRODUCT = "MCO39410080"  # el producto que da esa búsqueda


def crawl_deps(conn: psycopg.Connection, src: MercadoLibreSource, api: Api) -> CrawlDeps:
    clock = Clock()
    return CrawlDeps(
        conn=conn,
        source=src,
        http=api,
        credentials=Creds(),
        topics=spec_profile(),
        params=load_params(),
        budget=CrawlBudget(0, 0, 1000, 100_000, 2500),
        holder="gh:1:1",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rnd=random.Random(1),
    )


def test_mercadolibre_crawl_tracks_a_seeded_product(db: psycopg.Connection) -> None:
    store_row(db, "exito", "1", EAN, price=600_000)
    api = Api(
        {
            "POST /oauth/token": resp({"access_token": "TKN", "expires_in": 21600}),
            f"GET /products/search?{EAN}": fx("search-ean"),
            f"GET /products/{PRODUCT}/items": fx("items"),
            f"GET /products/{PRODUCT}": fx("product"),
        }
    )
    src = MercadoLibreSource()
    assert run_crawl(crawl_deps(db, src, api), T, minutes=30).status == "ok"
    # El más barato elegible de la fixture es una tienda oficial: no se pide ningún /users.
    assert [(m, p) for m, p, _, _ in api.calls] == [
        ("POST", "/oauth/token"),
        ("GET", "/products/search"),
        ("GET", f"/products/{PRODUCT}/items"),
    ]
    assert {h.get("Authorization") for m, _, _, h in api.calls if m == "GET"} == {"Bearer TKN"}
    rows = db.execute(
        """SELECT external_id, ean, topic, price_minor, checked_at FROM discountz.catalog_item
            WHERE source = 'mercadolibre'"""
    ).fetchall()
    assert rows == [(PRODUCT, EAN, "tecnologia", 439_900, None)]
    assert lookups(db) == {EAN: PRODUCT}

    api.calls.clear()  # 7 h después: el token venció y el producto se relee
    assert run_crawl(crawl_deps(db, src, api), T + 7 * HOUR, minutes=30).status == "ok"
    assert [p for _, p, _, _ in api.calls] == ["/oauth/token", f"/products/{PRODUCT}/items"]
    checked = db.execute(
        "SELECT checked_at FROM discountz.catalog_item WHERE source = 'mercadolibre'"
    ).fetchone()
    assert checked == (T + 7 * HOUR,)
