"""Rastreo de Falabella de punta a punta con fixtures, contra Postgres real (ADR 0017)."""

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
from discountz.sources.falabella import FalabellaSource
from discountz.sources.fixtures import load_fixture

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
class Falabella:
    """La página 1 de cada partición es la fixture grande; las demás, la última."""

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
        first = dict(params or {}).get("page") == "1"
        return load_fixture("falabella", "category-page" if first else "category-last")


def test_falabella_crawl_saves_items_by_topic(db: psycopg.Connection) -> None:
    clock = Clock()
    http = Falabella(clock)
    d = CrawlDeps(
        conn=db,
        source=FalabellaSource(),
        http=http,
        credentials=NoCredentials(),
        topics=load_topics(),
        params=load_params(),
        budget=CrawlBudget(3, 2, 100, 700, 0),
        holder="gh:1:1",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rnd=random.Random(1),
    )
    result = run_crawl(d, T, minutes=45)
    assert result.status == "ok"
    assert result.requests <= 100
    rows = db.execute(
        "SELECT count(*), min(currency) FROM discountz.catalog_item WHERE source = 'falabella'"
    ).fetchone()
    assert rows is not None
    assert rows[0] >= 4
    assert rows[1] == "COP"
    assert all("/falabella-co/category/" in u and "?" not in u for u in http.urls)
