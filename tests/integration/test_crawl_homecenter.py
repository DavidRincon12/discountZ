"""Rastreo de Homecenter de punta a punta con fixtures, contra Postgres real (ADR 0016)."""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
import pytest

from discountz.app.crawl import CrawlDeps, run_crawl
from discountz.config import load_params, load_topics
from discountz.domain.scheduling import CrawlBudget
from discountz.infra.http import Files, HttpResponse, Method, Params
from discountz.sources.base import NoCredentials
from discountz.sources.fixtures import load_fixture
from discountz.sources.homecenter import HomecenterSource

pytestmark = pytest.mark.integration

T = datetime(2026, 10, 24, 12, tzinfo=UTC)


@dataclass
class Clock:
    t: float = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@dataclass
class Homecenter:
    """Cada categoría devuelve la página 1 de la fixture grande y la última como página 5."""

    clock: Clock
    urls: list[str] = field(default_factory=list)

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
        self.urls.append(url)
        return load_fixture("homecenter", "category-last" if params else "category-page")


def test_homecenter_crawl_saves_items_by_topic(db: psycopg.Connection) -> None:
    clock = Clock()
    http = Homecenter(clock)
    d = CrawlDeps(
        conn=db,
        source=HomecenterSource(),
        http=http,
        credentials=NoCredentials(),
        topics=load_topics(),
        params=load_params(),
        budget=CrawlBudget(3, 2, 100, 800, 0),
        holder="gh:1:1",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rnd=random.Random(1),
    )
    result = run_crawl(d, T, minutes=45)
    assert result.status == "ok"
    assert result.requests <= 100
    rows = db.execute(
        "SELECT count(*), min(currency) FROM discountz.catalog_item WHERE source = 'homecenter'"
    ).fetchone()
    assert rows is not None
    assert rows[0] >= 5
    assert rows[1] == "COP"
    assert all("/category/" in u and "?" not in u for u in http.urls)
