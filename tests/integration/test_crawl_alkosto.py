"""Rastreo de Alkosto de punta a punta con fixtures, contra Postgres real (spec de Alkosto §7)."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
import pytest

from discountz.app.crawl import CrawlDeps, run_crawl
from discountz.config import load_params, load_topics
from discountz.domain.scheduling import CrawlBudget
from discountz.infra import db as repo
from discountz.infra.http import Files, HttpResponse, Method, Params
from discountz.sources.alkosto import BASE, OBJECTS_URL, QUERY_URL, AlkostoSource
from discountz.sources.base import NoCredentials
from discountz.sources.fixtures import load_fixture

pytestmark = pytest.mark.integration

T = datetime(2026, 10, 20, 12, tzinfo=UTC)
EMPTY = b'{"hits": [], "nbHits": 0}'


@dataclass
class Clock:
    t: float = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@dataclass
class Alkosto:
    """Portada y Algolia falsas: la franja inicial pasa de 1.000 y se parte en dos."""

    clock: Clock
    sent: list[Mapping[str, object] | None] = field(default_factory=list)

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
        self.clock.t += 1
        self.sent.append(json)
        if url == BASE + "/":
            return load_fixture("alkosto", "home")
        if url == OBJECTS_URL:
            return load_fixture("alkosto", "objects")
        assert url == QUERY_URL
        assert json is not None
        band = list(json["numericFilters"])  # type: ignore[call-overload]
        if band == ["lowestprice_double>=1", "lowestprice_double<=999999999"]:
            return load_fixture("alkosto", "query-big")
        if band == ["lowestprice_double>=31623", "lowestprice_double<=999999999"]:
            return load_fixture("alkosto", "query-band")
        return HttpResponse(200, QUERY_URL, {}, EMPTY, 1)


def test_alkosto_crawl_saves_items_and_splits(db: psycopg.Connection) -> None:
    clock = Clock()
    http = Alkosto(clock)
    d = CrawlDeps(
        conn=db,
        source=AlkostoSource(),
        http=http,
        credentials=NoCredentials(),
        topics=load_topics(),
        params=load_params(),
        budget=CrawlBudget(2, 1, 60, 1000, 2500),
        holder="gh:1:1",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rnd=random.Random(1),
    )
    result = run_crawl(d, T, minutes=45)
    assert result.status == "ok"
    assert result.requests <= 60
    assert result.items == 30
    rows = db.execute(
        "SELECT count(*), min(currency), min(topic) FROM discountz.catalog_item"
        " WHERE source = 'alkosto'"
    ).fetchone()
    assert rows is not None
    assert rows[0] == 30
    assert rows[1] == "COP"
    states = repo.load_catalog_states(db, "alkosto", ["7705946474559"])
    assert states["7705946474559"].price_minor == 1_649_900
    topic = db.execute(
        "SELECT topic FROM discountz.catalog_item WHERE external_id = '7705946474559'"
    ).fetchone()
    assert topic == ("tecnologia",)
    queries = [json.dumps(b) for b in http.sent if b is not None and "numericFilters" in b]
    assert len(queries) == 3  # la franja inicial y sus dos mitades
