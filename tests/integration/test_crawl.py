"""Caso de uso del rastreo de catálogo (spec del rastreo §2.2, §3.2 y §4) contra Postgres real."""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from discountz.app.crawl import (
    DB_RECONNECTS,
    CrawlDeps,
    CrawlResult,
    run_crawl,
    run_crawl_reconnecting,
)
from discountz.app.held import Slot, admit
from discountz.config import load_params
from discountz.domain.catalog import CatalogObservation, CatalogState
from discountz.domain.errors import ParseError, Unavailable
from discountz.domain.interests import Interest, compile_interest
from discountz.domain.model import (
    AlertKind,
    ExtractionPath,
    Lead,
    ListingRef,
    Offer,
    SearchSpec,
    SellerClass,
)
from discountz.domain.money import Currency, Money
from discountz.domain.scheduling import CrawlBudget
from discountz.domain.urls import SafeUrl
from discountz.infra import db as repo
from discountz.infra.db import CatalogRow
from discountz.infra.http import MAX_BODY_BYTES, BlockKind, Files, HttpResponse, Method, Params
from discountz.sources.base import (
    Capability,
    CardFacts,
    CatalogPage,
    CatalogPartition,
    ListingFailed,
    ListingFound,
    ListingGone,
    ListingOutcome,
    NeedsResolution,
    NoCredentials,
    SearchPage,
    SourceContext,
    SourceDescriptor,
)
from discountz.sources.blocks import detect_block_common
from tests.profiles import spec_profile

pytestmark = pytest.mark.integration
T = datetime(2026, 10, 16, 12, tzinfo=UTC)
PATH = "/Tecnología/Celulares/"


def obs(ext: str, price: int, *, ean: str | None = None) -> CatalogObservation:
    return CatalogObservation(ext, Money(price, Currency.COP), PATH, ean, SellerClass.FIRST_PARTY)


def offer(ext: str, price: int) -> Offer:
    return Offer(
        ref=ListingRef("exito", ext),
        parent_external_id=None,
        title=f"Celular {ext}",
        price=Money(price, Currency.COP),
        list_price=None,
        in_stock=True,
        seller_class=SellerClass.FIRST_PARTY,
        unit_key="un|1",
        extraction=ExtractionPath.API_JSON,
        observed_at=T,
        url_path=f"/celular-{ext}/p",
    )


@dataclass
class Clock:
    t: float = 0.0
    step: float = 1.0  # segundos que tarda cada petición

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@dataclass
class Transport:
    clock: Clock
    blocked: set[str] = field(default_factory=set)
    urls: list[str] = field(default_factory=list)
    body: bytes = b"{}"

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
        self.clock.t += self.clock.step
        self.urls.append(url)
        status = 403 if url in self.blocked else 200
        return HttpResponse(status, url, {}, self.body, 1)

    def kinds(self) -> list[str]:
        return [re.sub(r"https://fake/([a-z_]+).*", r"\1", u) for u in self.urls]


@dataclass
class FakeCatalog:
    """Fuente de catálogo programada; cada método hace una petición para contar presupuesto."""

    pages: dict[str, list[list[CatalogObservation]]]
    offers: dict[str, Offer] = field(default_factory=dict)
    hot: dict[str, CatalogObservation] = field(default_factory=dict)
    changed: list[list[CatalogObservation]] = field(default_factory=list)
    errors: set[tuple[str, int]] = field(default_factory=set)
    # Página → lecturas que fallan por red (5xx tras los reintentos) antes de responder.
    transient: dict[tuple[str, int], int] = field(default_factory=dict)
    failure: tuple[str, int] = ("retries_exhausted", 503)  # código y status de esas fallas
    items_failures: int = 0  # relecturas de la capa caliente que fallan por red
    code: str = "exito"
    supports_changed: bool = False
    max_batch: int = 50
    since_seen: list[datetime] = field(default_factory=list)
    default_offers: dict[str, Offer] = field(default_factory=dict)  # vendedor por defecto (v1)
    fail_confirms: int = 0  # confirmaciones que fallan por red antes de responder
    currency: Currency = Currency.COP
    paths: dict[str, str] = field(default_factory=dict)  # partición → ruta; sin ruta, «/»
    descriptor: SourceDescriptor = field(init=False)

    def __post_init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=self.code,
            kind="store",
            currency=self.currency,
            hosts=frozenset({"www.exito.com"}),
            client="httpx",
            capabilities=frozenset({Capability.DETAIL, Capability.CATALOG}),
            max_batch=self.max_batch,
            external_id_re=re.compile(r"^\d+$"),
            image_hosts=frozenset({"exitocol.vteximg.com.br"}),
            supports_changed=self.supports_changed,
        )

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[CatalogPartition]:
        ctx.http.request("GET", "https://fake/partitions")
        return [CatalogPartition(k, self.paths.get(k, "/")) for k in self.pages]

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        ctx.http.request("GET", f"https://fake/page/{partition}/{page}")
        if (partition, page) in self.errors:
            raise ParseError("schema")
        if self.transient.get((partition, page), 0) > 0:
            self.transient[(partition, page)] -= 1
            raise Unavailable(self.code, *self.failure)
        pages = self.pages[partition]
        items = pages[page] if page < len(pages) else []
        return CatalogPage(items, has_more=page + 1 < len(pages))

    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage:
        ctx.http.request("GET", f"https://fake/changed/{page}")
        self.since_seen.append(since)
        items = self.changed[page] if page < len(self.changed) else []
        return CatalogPage(items, has_more=page + 1 < len(self.changed))

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> Sequence[CatalogObservation]:
        ctx.http.request("GET", "https://fake/items")
        if self.items_failures > 0:
            self.items_failures -= 1
            raise Unavailable(self.code, "retries_exhausted")
        return [self.hot[i] for i in external_ids if i in self.hot]

    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]:
        ctx.http.request("GET", "https://fake/facts")
        return {
            i: CardFacts(
                f"Celular {i} 256 GB",
                f"https://www.exito.com/celular-{i}/p",
                f"https://exitocol.vteximg.com.br/arquivos/ids/{i}/a.jpg",
            )
            for i in external_ids
        }

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        ctx.http.request("GET", "https://fake/listings")
        offers = self.offers | self.default_offers
        return [
            ListingFound(offers[r.external_id]) if r.external_id in offers else ListingGone(r)
            for r in refs
        ]

    def fetch_catalog_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        ctx.http.request("GET", "https://fake/listings")
        if self.fail_confirms > 0:
            self.fail_confirms -= 1
            return [ListingFailed(r, Unavailable("fake", "timeout")) for r in refs]
        return [
            ListingFound(self.offers[r.external_id])
            if r.external_id in self.offers
            else ListingGone(r)
            for r in refs
        ]

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        return None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        return SearchPage([], None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        return None

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


def deps(
    conn: psycopg.Connection,
    source: FakeCatalog,
    clock: Clock,
    *,
    max_per_run: int = 1000,
    holder: str = "gh:1:1",
    max_items: int | None = None,
    interests: tuple[Interest, ...] = (),
) -> tuple[CrawlDeps, Transport]:
    transport = Transport(clock)
    d = CrawlDeps(
        conn=conn,
        source=source,
        http=transport,
        credentials=NoCredentials(),
        topics=spec_profile(),
        params=load_params(),
        budget=CrawlBudget(0, 0, max_per_run, 100_000, 2500),
        holder=holder,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        rnd=random.Random(1),
        max_items=max_items,
        interests=interests,
    )
    return d, transport


def count(conn: psycopg.Connection, table: str, where: str = "true") -> int:
    r = conn.execute(f"SELECT count(*) FROM discountz.{table} WHERE {where}").fetchone()  # noqa: S608
    assert r is not None
    return int(r[0])


def seed_history(conn: psycopg.Connection, ext: str, price: int, *, ean: str | None = None) -> None:
    """Producto visto 5 días a `price` estable: ya tiene `usual` y `min_seen`."""
    start = T - timedelta(days=5)
    state = CatalogState(price, price, price, start, start, 20, None, None, None)
    repo.save_catalog_page(
        conn,
        "exito",
        [CatalogRow(ext, ean, "tecnologia", "COP", state)],
        T - timedelta(hours=1),
    )


def page_of(n: int, start: int = 0) -> list[CatalogObservation]:
    return [obs(str(1000 + start + i), 50_000 + i) for i in range(n)]


def test_crawl_resumes_from_cursor_without_duplicates(db: psycopg.Connection) -> None:
    src = FakeCatalog({"C:/1/": [page_of(2, 0), page_of(2, 2), page_of(2, 4), page_of(2, 6)]})
    clock = Clock(step=20)
    d, transport = deps(db, src, clock)
    first = run_crawl(d, T, minutes=1)
    assert first.status == "ok"
    assert transport.kinds() == ["partitions", "page", "page"]
    cur = repo.get_cursor(db, "exito")
    assert (cur.sweep_no, cur.partition_idx, cur.page) == (1, 0, 2)
    assert cur.partitions == ("C:/1/",)

    clock2 = Clock(step=20)
    d2, transport2 = deps(db, src, clock2)
    second = run_crawl(d2, T + timedelta(hours=1), minutes=1)
    assert second.pages == 2
    assert count(db, "catalog_item") == 8
    assert count(db, "catalog_item", "last_changed_at IS NOT NULL") == 0  # 8 nuevos, sin cambios
    assert transport2.kinds() == ["page", "page"]  # la vuelta terminó: la corrida para
    cur = repo.get_cursor(db, "exito")
    assert (cur.sweep_no, cur.partitions, cur.partition_idx, cur.page) == (1, (), 0, 0)
    assert cur.last_sweep_s == 3600
    assert cur.day_requests == 5


def test_crawl_skips_partitions_outside_the_profile(
    db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Mercado está apagado en el perfil: su partición no se pide (ADR 0013)."""
    src = FakeCatalog(
        {"C:/1/": [page_of(1)], "C:/2/": [page_of(1, 5)]},
        paths={"C:/1/": "/Tecnología/", "C:/2/": "/Mercado/"},
    )
    d, transport = deps(db, src, Clock())
    with caplog.at_level(logging.INFO, logger="discountz"):
        result = run_crawl(d, T, minutes=60)
    assert "crawl_partitions_skipped source=exito count=1" in caplog.text
    assert result.status == "ok"
    assert transport.kinds() == ["partitions", "page"]
    assert transport.urls[1] == "https://fake/page/C:/1//0"
    assert count(db, "catalog_item") == 1


def test_crawl_with_every_partition_off_ends_ok_without_requests(db: psycopg.Connection) -> None:
    """Sin nada que rastrear la corrida termina en paz y no guarda un barrido vacío."""
    src = FakeCatalog({"C:/2/": [page_of(1)]}, paths={"C:/2/": "/Mercado/"})
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert (result.status, result.pages) == ("ok", 0)
    assert transport.kinds() == ["partitions"]
    cur = repo.get_cursor(db, "exito")
    assert (cur.sweep_no, cur.partitions) == (0, ())


def test_crawl_resumes_a_cursor_saved_with_plain_keys(db: psycopg.Connection) -> None:
    """Un barrido guardado antes del perfil (solo claves) sigue donde iba."""
    cur = repo.get_cursor(db, "exito")
    repo.save_cursor(
        db, replace(cur, sweep_no=1, partitions=("C:/1/", "C:/2/"), partition_idx=1, page=0)
    )
    src = FakeCatalog(
        {"C:/1/": [page_of(1)], "C:/2/": [page_of(1, 5)]},
        paths={"C:/1/": "/Tecnología/", "C:/2/": "/Mercado/"},
    )
    d, transport = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).status == "ok"
    assert transport.urls == ["https://fake/page/C:/2//0"]


def test_crawl_accepts_catalog_pages_over_the_cycle_body_cap(db: psycopg.Connection) -> None:
    """Olímpica pesa ~7 MB por página de 50 (medido el 2026-10-17): el rastreo no la descarta."""
    src = FakeCatalog({"C:/1/": [page_of(2)]})
    d, transport = deps(db, src, Clock(step=20))
    transport.body = b"x" * (MAX_BODY_BYTES + 1)
    result = run_crawl(d, T, minutes=1)
    assert (result.status, result.items) == ("ok", 2)


def test_crawl_splits_partition_and_keeps_split_in_cursor(db: psycopg.Connection) -> None:
    src = FakeCatalog({"C:/1/": [page_of(1)]})
    bands = {"C:/1/|P:[1 TO 9]": page_of(1, 5)}

    def split(ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        ctx.http.request("GET", f"https://fake/page/{partition}/{page}")
        if partition == "C:/1/":
            return CatalogPage((), has_more=False, split=["C:/1/|P:[1 TO 9]", "C:/1/|P:[10 TO 99]"])
        return CatalogPage(bands.get(partition, []), has_more=False)

    src.fetch_catalog_page = split  # type: ignore[method-assign]
    clock = Clock(step=20)
    d, _ = deps(db, src, clock)
    run_crawl(d, T, minutes=1)  # particiones, página partida y la primera banda
    cur = repo.get_cursor(db, "exito")
    assert cur.partitions == ("C:/1/|P:[1 TO 9]", "C:/1/|P:[10 TO 99]")
    assert (cur.partition_idx, cur.page) == (1, 0)
    assert "1005" in repo.load_catalog_states(db, "exito", ["1005"])


def test_crawl_hot_tier_first_and_capped(db: psycopg.Connection) -> None:
    rows = [
        CatalogRow(
            str(i),
            None,
            "tecnologia",
            "COP",
            CatalogState(10_000, None, None, T, T, 1, T - timedelta(hours=1), None, None),
        )
        for i in range(200)
    ]
    repo.save_catalog_page(db, "exito", rows, T - timedelta(hours=1))
    src = FakeCatalog({"C:/1/": [page_of(1)] * 100}, max_batch=2)
    d, transport = deps(db, src, Clock(), max_per_run=20)
    result = run_crawl(d, T, minutes=60)
    kinds = transport.kinds()
    assert kinds[:5] == ["items"] * 5  # 25 % de 20
    assert kinds.count("items") == 5
    assert result.requests == len(kinds) == 20


def test_crawl_hot_tier_skips_topics_out_of_the_profile(db: psycopg.Connection) -> None:
    """Lo guardado de un tema apagado no se relee: no puede alertar y así vence con la retención
    (ADR 0013)."""
    changed = CatalogState(10_000, None, None, T, T, 1, T - timedelta(hours=1), None, None)
    rows = [
        CatalogRow("1", None, "belleza", "COP", changed),
        CatalogRow("2", None, "tecnologia", "COP", changed),
    ]
    repo.save_catalog_page(db, "exito", rows, T - timedelta(hours=1))
    src = FakeCatalog({"C:/1/": [page_of(1)]}, max_batch=1)
    d, transport = deps(db, src, Clock())
    run_crawl(d, T, minutes=60)
    assert transport.kinds().count("items") == 1  # solo el de tecnología


def test_crawl_hot_tier_uses_fetch_changed_when_supported(db: psycopg.Connection) -> None:
    src = FakeCatalog({"all": [page_of(1)]}, changed=[[obs("7", 9_000)]], supports_changed=True)
    d, _ = deps(db, src, Clock(step=1))
    run_crawl(d, T, minutes=60)
    assert src.since_seen == []  # primera corrida: sin `changed_since`, no hay capa caliente
    assert repo.get_cursor(db, "exito").changed_since == T

    later = T + timedelta(hours=1)
    d2, transport2 = deps(db, src, Clock(step=1))
    run_crawl(d2, later, minutes=60)
    assert src.since_seen == [T]
    assert transport2.kinds()[0] == "changed"
    assert repo.get_cursor(db, "exito").changed_since == later
    assert "7" in repo.load_catalog_states(db, "exito", ["7"])


def test_crawl_three_parse_errors_in_a_row_stop(db: psycopg.Connection) -> None:
    pages = [page_of(1, i) for i in range(6)]
    src = FakeCatalog({"C:/1/": pages}, errors={("C:/1/", 1), ("C:/1/", 2), ("C:/1/", 3)})
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.status == "partial"
    assert transport.kinds() == ["partitions", "page", "page", "page", "page"]
    assert repo.get_cursor(db, "exito").page == 4  # la próxima corrida sigue después
    breaker = repo.load_breakers(db)["exito"]
    assert breaker.consecutive_parse >= 3 or breaker.state == "open"


def test_crawl_logs_the_code_of_each_source_error(
    db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Sin el código, un rastreo que para en `partial` no dice qué falló (2026-10-17)."""
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(3)]}, errors={("C:/1/", 1)})
    d, _ = deps(db, src, Clock())
    with caplog.at_level(logging.INFO, logger="discountz"):
        run_crawl(d, T, minutes=60)
    assert "source_error source=exito code=" in caplog.text


def test_crawl_logs_why_it_did_not_run(
    db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(4)]})
    d, transport = deps(db, src, Clock())
    transport.blocked.add("https://fake/page/C:/1//1")
    run_crawl(d, T, minutes=60)
    again, _ = deps(db, src, Clock())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="discountz"):
        assert run_crawl(again, T + timedelta(minutes=5), minutes=60).status == "blocked"
    assert "crawl_done source=exito status=blocked requests=0" in caplog.text


def test_crawl_single_parse_error_is_skipped(db: psycopg.Connection) -> None:
    pages = [page_of(1, i) for i in range(3)]
    src = FakeCatalog({"C:/1/": pages}, errors={("C:/1/", 1)})
    d, _ = deps(db, src, Clock())
    run_crawl(d, T, minutes=60)
    assert set(repo.load_catalog_states(db, "exito", ["1000", "1001", "1002"])) == {"1000", "1002"}


def test_crawl_retries_a_page_after_a_transient_error(db: psycopg.Connection) -> None:
    """Un corte de segundos en la tienda cortaba la hora entera: el 2026-10-18 y 19 pararon así
    al menos 16 corridas (10 de Olímpica, 5 de Éxito y 1 de Panamericana)."""
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(3)]}, transient={("C:/1/", 1): 1})
    clock = Clock()
    d, transport = deps(db, src, clock)
    result = run_crawl(d, T, minutes=60)
    assert (result.status, result.items) == ("ok", 3)
    assert transport.kinds() == ["partitions", "page", "page", "page", "page"]
    assert clock.t >= 60  # esperó un minuto antes de pedirla otra vez
    assert repo.load_breakers(db)["exito"].consecutive_unavailable == 0


def test_crawl_stops_after_three_transient_errors_on_the_same_page(db: psycopg.Connection) -> None:
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(3)]}, transient={("C:/1/", 1): 9})
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.status == "partial"
    assert transport.kinds() == ["partitions", "page", "page", "page", "page"]
    assert repo.get_cursor(db, "exito").page == 1  # la próxima corrida la vuelve a pedir
    assert repo.load_breakers(db)["exito"].consecutive_unavailable == 3


def test_crawl_recovered_errors_do_not_add_up_in_the_breaker(db: psycopg.Connection) -> None:
    """Cinco cortes de segundos en páginas distintas no son cinco fallas seguidas."""
    transient = {("C:/1/", p): 1 for p in range(1, 6)}
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(7)]}, transient=transient)
    d, _ = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert (result.status, result.items) == ("ok", 7)
    breaker = repo.load_breakers(db)["exito"]
    assert (breaker.state, breaker.consecutive_unavailable) == ("closed", 0)


def test_crawl_hot_tier_survives_a_transient_error(db: psycopg.Connection) -> None:
    state = CatalogState(10_000, None, None, T, T, 1, T - timedelta(hours=1), None, None)
    row = CatalogRow("7", None, "tecnologia", "COP", state)
    repo.save_catalog_page(db, "exito", [row], T - timedelta(hours=1))
    src = FakeCatalog({"C:/1/": [page_of(1)]}, hot={"7": obs("7", 10_000)}, items_failures=1)
    d, transport = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).status == "ok"
    assert transport.kinds() == ["items", "items", "partitions", "page"]


def test_crawl_does_not_wait_for_an_error_a_pause_cannot_fix(db: psycopg.Connection) -> None:
    """Una clave rechazada no se arregla en un minuto: la corrida para sin esperar."""
    src = FakeCatalog(
        {"C:/1/": [page_of(1, i) for i in range(3)]},
        transient={("C:/1/", 1): 9},
        failure=("bad_key", 401),
    )
    clock = Clock()
    d, transport = deps(db, src, clock)
    assert run_crawl(d, T, minutes=60).status == "partial"
    assert transport.kinds() == ["partitions", "page", "page"]
    assert clock.t < 60


def test_crawl_does_not_wait_past_its_deadline(db: psycopg.Connection) -> None:
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(3)]}, transient={("C:/1/", 1): 1})
    clock = Clock()
    d, _ = deps(db, src, clock)
    assert run_crawl(d, T, minutes=1).status == "partial"
    assert clock.t < 60


def test_crawl_logs_the_http_status_of_a_source_error(
    db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Sin el status no se sabe si el corte fue un 5xx, un 429 o la red (2026-10-20)."""
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(3)]}, transient={("C:/1/", 1): 9})
    d, _ = deps(db, src, Clock())
    with caplog.at_level(logging.INFO, logger="discountz"):
        run_crawl(d, T, minutes=60)
    assert "source_error source=exito code=retries_exhausted http_status=503" in caplog.text


def test_crawl_confirmation_mismatch_drops_candidate(db: psycopg.Connection) -> None:
    seed_history(db, "123", 199_900)
    src = FakeCatalog({"C:/1/": [[obs("123", 119_900)]]}, offers={"123": offer("123", 122_300)})
    d, _ = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.alerts == 0
    assert count(db, "alert") == 0
    assert count(db, "outbox") == 0


def test_crawl_confirms_with_cheapest_in_stock_seller(db: psycopg.Connection) -> None:
    """Vendedor por defecto agotado y otro con stock: la confirmación usa la regla del recorrido."""
    seed_history(db, "123", 199_900)
    gone = replace(offer("123", 119_900), in_stock=False)
    src = FakeCatalog(
        {"C:/1/": [[obs("123", 119_900)]]},
        offers={"123": offer("123", 119_900)},
        default_offers={"123": gone},
    )
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 1


def test_crawl_confirm_failure_keeps_candidate_for_next_run(db: psycopg.Connection) -> None:
    seed_history(db, "123", 199_900)
    src = FakeCatalog(
        {"C:/1/": [[obs("123", 119_900)]]}, offers={"123": offer("123", 119_900)}, fail_confirms=1
    )
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 0
    assert count(db, "catalog_item", "last_changed_at IS NOT NULL") == 0  # queda sin aplicar
    d2, _ = deps(db, src, Clock())
    assert run_crawl(d2, T + timedelta(hours=1), minutes=60).alerts == 1
    assert count(db, "catalog_item", "last_changed_at IS NOT NULL") == 1


def test_crawl_ean_duplicate_is_skipped(db: psycopg.Connection) -> None:
    ean = "7701234567890"
    start = T - timedelta(days=5)
    state = CatalogState(119_900, 199_900, 199_900, start, start, 3, None, None, None)
    repo.save_catalog_page(db, "carulla", [CatalogRow("9", ean, "tecnologia", "COP", state)], T)
    repo.mark_catalog_alert(db, "carulla", "9", 119_900, T - timedelta(hours=1))
    seed_history(db, "123", 199_900, ean=ean)
    src = FakeCatalog(
        {"C:/1/": [[obs("123", 119_900, ean=ean)]]}, offers={"123": offer("123", 119_900)}
    )
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.alerts == 0
    assert "listings" not in transport.kinds()
    assert count(db, "alert") == 0


def test_crawl_block_trips_breaker_and_stops(db: psycopg.Connection) -> None:
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(4)]})
    d, transport = deps(db, src, Clock())
    transport.blocked.add("https://fake/page/C:/1//1")
    result = run_crawl(d, T, minutes=60)
    assert result.status == "blocked"
    assert repo.load_breakers(db)["exito"].state == "open"
    assert repo.get_cursor(db, "exito").page == 1
    assert transport.kinds() == ["partitions", "page", "page"]
    again, transport2 = deps(db, src, Clock())
    assert run_crawl(again, T + timedelta(minutes=5), minutes=60).status == "blocked"
    assert transport2.urls == []


def test_crawl_disabled_source_does_nothing(db: psycopg.Connection) -> None:
    src = FakeCatalog({"all": [page_of(1)]}, code="bestbuy_us")  # apagada en la semilla
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result == CrawlResult("disabled", 0, 0, 0, 0, 0)
    assert transport.urls == []


def test_crawl_lease_held_skips(db: psycopg.Connection) -> None:
    assert repo.acquire_crawl_lease(db, "exito", "otro", T, timedelta(minutes=60))
    src = FakeCatalog({"C:/1/": [page_of(1)]})
    d, transport = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).status == "skipped_locked"
    assert transport.urls == []


def test_crawl_respects_daily_cap(db: psycopg.Connection) -> None:
    repo.save_cursor(db, replace(repo.get_cursor(db, "exito"), day=T.date(), day_requests=99_999))
    src = FakeCatalog({"C:/1/": [page_of(1)] * 5})
    d, transport = deps(db, src, Clock())
    run_crawl(d, T, minutes=60)
    assert len(transport.urls) == 1


def test_crawl_spreads_the_daily_budget_over_the_hours_left(db: psycopg.Connection) -> None:
    """A las 12:00 UTC quedan 12 horas: las 120 peticiones que quedan dan 10 por corrida."""
    repo.save_cursor(db, replace(repo.get_cursor(db, "exito"), day=T.date(), day_requests=99_880))
    src = FakeCatalog({"C:/1/": [page_of(1, i) for i in range(200)]})
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.requests == 10
    assert len(transport.urls) == 10


def test_crawl_enqueues_card_with_topic(db: psycopg.Connection) -> None:
    seed_history(db, "123", 199_900)
    src = FakeCatalog({"C:/1/": [[obs("123", 119_900)]]}, offers={"123": offer("123", 119_900)})
    d, _ = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.topic == "tecnologia"
    assert row.priority == 5  # máx(2, 9 − 4002 // 1000)
    assert row.card == {
        "store": "exito",
        "topic": "tecnologia",
        "title": "Celular 123 256 GB",
        "url": "https://www.exito.com/celular-123/p",
        "image_url": "https://exitocol.vteximg.com.br/arquivos/ids/123/a.jpg",
        "price_minor": 119_900,
        "usual_minor": 199_900,
        "currency": "COP",
        "drop_bp": 4002,
        "min_days": 5,
        "kind": "catalog_drop",
        "reference_label": "Antes",
        "note": None,
        "peers": 0,
        "interest": None,
    }
    assert "BAJÓ DE PRECIO" in row.body_html
    alert = db.execute("SELECT kind, delivery FROM discountz.alert").fetchone()
    assert alert == ("catalog_drop", "queued")
    s = repo.load_catalog_states(db, "exito", ["123"])["123"]
    assert (s.last_alert_at, s.last_alert_minor) == (T, 119_900)


def test_drop_in_a_non_card_topic_is_not_confirmed(
    db: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Belleza está fuera del perfil: la bajada no se confirma ni alerta (ADR 0013)."""
    seed_history(db, "123", 199_900)
    drop = CatalogObservation(
        "123", Money(119_900, Currency.COP), "/Belleza/Maquillaje/", None, SellerClass.FIRST_PARTY
    )
    src = FakeCatalog({"C:/1/": [[drop]]}, offers={"123": offer("123", 119_900)})
    d, transport = deps(db, src, Clock())
    with caplog.at_level(logging.INFO, logger="discountz"):
        result = run_crawl(d, T, minutes=60)
    assert result.alerts == 0
    (done,) = [m for m in caplog.messages if m.startswith("crawl_done")]
    assert "skipped=1" in done.split()
    assert "listings" not in transport.kinds()
    assert count(db, "alert") == 0
    # La fila sí se guarda con el precio nuevo, como cualquier lectura sin candidato.
    where = "external_id = '123' AND topic = 'belleza' AND price_minor = 119900"
    assert count(db, "catalog_item", where) == 1


def test_drop_in_a_card_topic_still_alerts(db: psycopg.Connection) -> None:
    """La misma bajada en Tecnología sigue saliendo como tarjeta."""
    seed_history(db, "123", 199_900)
    src = FakeCatalog({"C:/1/": [[obs("123", 119_900)]]}, offers={"123": offer("123", 119_900)})
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.alerts == 1
    assert "listings" in transport.kinds()
    assert count(db, "alert") == 1


def test_a_warranty_listed_as_a_product_never_alerts(db: psycopg.Connection) -> None:
    """«Garantía Extendida 36 Meses» baja de precio pero no es una oferta (Jumbo, 2026-10-29)."""
    seed_history(db, "123", 288_000)
    warranty = replace(obs("123", 223_978), title="Garantía Extendida 36 Meses")
    src = FakeCatalog({"C:/1/": [[warranty]]}, offers={"123": offer("123", 223_978)})
    d, _ = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.alerts == 0
    assert count(db, "alert") == 0
    assert count(db, "catalog_item", "external_id = '123' AND price_minor = 223978") == 1


def test_crawl_price_error_also_posts_line_in_category(db: psycopg.Connection) -> None:
    seed_history(db, "123", 199_900)
    src = FakeCatalog({"C:/1/": [[obs("123", 39_900)]]}, offers={"123": offer("123", 39_900)})
    d, _ = deps(db, src, Clock())
    run_crawl(d, T, minutes=60)
    rows = {r.topic: r for r in repo.claim_outbox(db, 10, T)}
    assert set(rows) == {"errores", "tecnologia"}
    assert rows["errores"].priority == 0
    assert rows["errores"].card is not None
    assert rows["tecnologia"].card is None
    assert "Errores de precio" in rows["tecnologia"].body_html


def test_crawl_alert_past_the_daily_cap_waits_for_the_digest(db: psycopg.Connection) -> None:
    cap = load_params().daily_cards
    for i in range(cap):
        admit(db, Slot("exito", "hogar", AlertKind.CATALOG_DROP, 1000 + 100 * i), T, cap, "• x")
    seed_history(db, "123", 199_900)
    src = FakeCatalog({"C:/1/": [[obs("123", 119_900)]]}, offers={"123": offer("123", 119_900)})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 0
    assert count(db, "outbox") == 0
    assert db.execute("SELECT delivery FROM discountz.alert").fetchone() == ("held",)
    row = db.execute("SELECT topic, line FROM discountz.offer_log WHERE NOT carded").fetchone()
    assert row is not None and row[0] == "tecnologia"
    assert "Celular 123 256 GB</a> · <b>$119.900</b>" in row[1]


def test_crawl_same_promotion_goes_out_once(db: psycopg.Connection) -> None:
    """Dos tallas o colores al mismo descuento en la misma tienda y tema: una sola tarjeta."""
    seed_history(db, "123", 199_900)
    seed_history(db, "124", 199_900)
    src = FakeCatalog(
        {"C:/1/": [[obs("123", 119_900), obs("124", 119_900)]]},
        offers={"123": offer("123", 119_900), "124": offer("124", 119_900)},
    )
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 1
    assert count(db, "outbox") == 1
    deliveries = db.execute("SELECT delivery FROM discountz.alert ORDER BY id").fetchall()
    assert deliveries == [("queued",), ("held",)]


def test_crawl_stops_when_breaker_opens_meanwhile_and_keeps_it(db: psycopg.Connection) -> None:
    """El ciclo de la v1 abre el breaker a mitad de la corrida: el rastreo para y no lo pisa."""
    src = FakeCatalog({"C:/1/": [page_of(1, 0), page_of(1, 1), page_of(1, 2)]})
    original = src.fetch_catalog_page

    def page(ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        n = page
        if n == 1:
            b = repo.load_breakers(db)["exito"]
            repo.save_breaker(
                db, "exito", replace(b, state="open", open_until=T + timedelta(hours=1))
            )
        return original(ctx, partition, page)

    src.fetch_catalog_page = page  # type: ignore[method-assign]
    d, transport = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.status == "blocked"
    assert transport.kinds().count("page") == 2
    assert repo.load_breakers(db)["exito"].state == "open"


# ───────────── eBay: precio bajo frente al mismo modelo (spec de eBay §4.2)

EBAY = "ebay_us"
EPATH = "/Consumer Electronics/TV/"
MODEL = "99999"


def eobs(ext: str, price: int, model: str | None = MODEL) -> CatalogObservation:
    return CatalogObservation(
        ext, Money(price, Currency.USD), EPATH, None, SellerClass.FIRST_PARTY, model
    )


def eoffer(ext: str, price: int) -> Offer:
    return replace(
        offer(ext, 0),
        ref=ListingRef(EBAY, ext),
        price=Money(price, Currency.USD),
        url_path=f"/itm/{ext}",
    )


def ebay_source(page: list[CatalogObservation], prices: Mapping[str, int]) -> FakeCatalog:
    return FakeCatalog(
        {"293": [page]},
        offers={e: eoffer(e, p) for e, p in prices.items()},
        code=EBAY,
        currency=Currency.USD,
        max_batch=1,
    )


def seed_ebay(
    conn: psycopg.Connection, ext: str, price: int, model: str | None = MODEL, days: int = 5
) -> None:
    start = T - timedelta(days=days)
    state = CatalogState(price, price, price, start, start, 20, None, None, None)
    row = CatalogRow(ext, None, "otros", "USD", state, model_key=model)
    repo.save_catalog_page(conn, EBAY, [row], T - timedelta(hours=1))


def seed_peers(conn: psycopg.Connection, price: int, n: int = 5) -> None:
    for i in range(n):
        seed_ebay(conn, f"80000000000{i}", price)


def test_model_low_alert(db: psycopg.Connection) -> None:
    seed_peers(db, 10_000)
    src = ebay_source([eobs("900000000001", 8_000)], {"900000000001": 8_000})
    d, _ = deps(db, src, Clock())
    result = run_crawl(d, T, minutes=60)
    assert result.alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.card is not None
    assert row.card["kind"] == "model_low"
    assert (row.card["usual_minor"], row.card["drop_bp"]) == (10_000, 2000)
    assert row.card["reference_label"] == "Mediana de 5 publicaciones del mismo modelo"
    assert row.card["peers"] == 5
    assert "MÁS BARATO QUE SU MODELO" in row.body_html
    alert = db.execute("SELECT kind, reference_minor FROM discountz.alert").fetchone()
    assert alert == ("model_low", 10_000)
    s = repo.load_catalog_states(db, EBAY, ["900000000001"])["900000000001"]
    assert s.last_alert_minor == 8_000


def test_model_low_needs_five_peers(db: psycopg.Connection) -> None:
    seed_peers(db, 10_000, n=4)
    src = ebay_source([eobs("900000000001", 8_000)], {"900000000001": 8_000})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 0


def test_model_low_duplicate_within_source(db: psycopg.Connection) -> None:
    seed_peers(db, 10_000)
    first = ebay_source([eobs("900000000001", 8_000)], {"900000000001": 8_000})
    d, _ = deps(db, first, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 1
    second = ebay_source([eobs("900000000002", 8_050)], {"900000000002": 8_050})
    d2, transport = deps(db, second, Clock())
    assert run_crawl(d2, T + timedelta(minutes=30), minutes=60).alerts == 0
    assert "listings" not in transport.kinds()


def test_model_low_same_page_alerts_once(db: psycopg.Connection) -> None:
    seed_peers(db, 10_000)
    page = [eobs("900000000001", 8_000), eobs("900000000002", 7_990)]
    src = ebay_source(page, {"900000000001": 8_000, "900000000002": 7_990})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.card is not None and row.card["price_minor"] == 7_990


def test_model_low_first_read_uses_same_page_peers(db: psycopg.Connection) -> None:
    """Primera lectura de una categoría: los pares del mismo modelo vienen en la misma página."""
    page = [eobs(f"80000000000{i}", 10_000) for i in range(5)] + [eobs("900000000001", 8_000)]
    src = ebay_source(page, {"900000000001": 8_000})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.card is not None
    assert (row.card["kind"], row.card["usual_minor"], row.card["peers"]) == (
        "model_low",
        10_000,
        5,
    )


def test_model_low_same_page_price_replaces_stored_one(db: psycopg.Connection) -> None:
    """Si un par guardado sale en la página con otro precio, cuenta el de la página."""
    seed_peers(db, 10_000)
    page = [eobs(f"80000000000{i}", 8_100) for i in range(5)] + [eobs("900000000001", 8_000)]
    src = ebay_source(page, {"900000000001": 8_000})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 0


def test_same_read_keeps_bigger_drop(db: psycopg.Connection) -> None:
    """§4.1 da 2.100 pb y §4.2 da 3.000 pb: una sola alerta, la del modelo."""
    seed_peers(db, 11_429)
    seed_ebay(db, "900000000001", 10_127)
    src = ebay_source([eobs("900000000001", 8_000)], {"900000000001": 8_000})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.card is not None
    assert (row.card["kind"], row.card["drop_bp"], row.card["usual_minor"]) == (
        "model_low",
        3000,
        11_429,
    )
    assert count(db, "alert") == 1


def test_same_read_keeps_listing_drop_when_bigger(db: psycopg.Connection) -> None:
    seed_peers(db, 10_000)
    seed_ebay(db, "900000000001", 10_500)
    src = ebay_source([eobs("900000000001", 7_900)], {"900000000001": 7_900})
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.card is not None
    assert (row.card["kind"], row.card["reference_label"]) == ("catalog_drop", "Antes")


def test_max_items_blocks_new_rows_only(db: psycopg.Connection) -> None:
    seed_ebay(db, "800000000001", 10_000, model=None)
    seed_ebay(db, "800000000002", 10_000, model=None)
    page = [eobs("800000000001", 9_900, None), eobs("900000000009", 5_000, None)]
    src = ebay_source(page, {})
    d, _ = deps(db, src, Clock(), max_items=2)
    run_crawl(d, T, minutes=60)
    states = repo.load_catalog_states(db, EBAY, ["800000000001", "900000000009"])
    assert set(states) == {"800000000001"}
    assert states["800000000001"].price_minor == 9_900
    assert count(db, "catalog_item", "source = 'ebay_us'") == 2


def test_ebay_paused_at_450(db: psycopg.Connection) -> None:
    repo.set_maintenance_state(db, T.date(), 460_000_000, True)
    src = ebay_source([eobs("900000000001", 8_000)], {})
    d, transport = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).status == "disabled"
    assert transport.urls == []


def test_quota_stops_without_tripping_breaker(db: psycopg.Connection) -> None:
    src = ebay_source([eobs("900000000001", 8_000)], {})

    def quota(ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        ctx.http.request("GET", f"https://fake/page/{partition}/{page}")
        raise Unavailable(EBAY, "quota", 429)

    src.fetch_catalog_page = quota  # type: ignore[method-assign]
    d, _ = deps(db, src, Clock())
    assert run_crawl(d, T, minutes=60).status == "partial"
    b = repo.load_breakers(db)[EBAY]
    assert (b.state, b.consecutive_unavailable) == ("closed", 0)


def _drop_on_fetch(
    src: FakeCatalog, admin: psycopg.Connection, pages: set[int], *, once: bool = True
) -> list[psycopg.Connection]:
    """Corta desde el servidor la conexión del rastreo al pedir esas páginas (caída del pooler)."""
    victims: list[psycopg.Connection] = []
    fetch = src.fetch_catalog_page

    def dropping(ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        result = fetch(ctx, partition, page)
        if page in pages:
            admin.execute("SELECT pg_terminate_backend(%s)", (victims[-1].info.backend_pid,))
            if once:
                pages.discard(page)
        return result

    src.fetch_catalog_page = dropping  # type: ignore[method-assign]
    return victims


def test_crawl_reconnects_when_the_db_drops_mid_run(
    db: psycopg.Connection,
    connect_extra: Callable[[], psycopg.Connection],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """18-oct 20:42: una caída del pooler cortaba la corrida; ahora reconecta y sigue."""
    src = FakeCatalog({"C:/1/": [page_of(2, 0), page_of(2, 2), page_of(2, 4), page_of(2, 6)]})
    victims = _drop_on_fetch(src, db, {2})

    def reconnect() -> psycopg.Connection:
        victims.append(connect_extra())
        return victims[-1]

    d, transport = deps(reconnect(), src, Clock(step=20))
    with caplog.at_level(logging.INFO, logger="discountz"):
        result = run_crawl_reconnecting(d, T, 10, reconnect)
    assert result.status == "ok"
    assert count(db, "catalog_item") == 8
    assert transport.kinds() == ["partitions", "page", "page", "page", "page", "page"]
    # La clase dice si fue una caída (AdminShutdown) o el statement_timeout (QueryCanceled).
    pattern = r"db_reconnect source=exito count=1 error=(AdminShutdown|OperationalError)\b"
    assert re.search(pattern, caplog.text)
    assert count(db, "crawl_lease") == 0
    assert repo.get_cursor(db, "exito").partitions == ()


def test_crawl_gives_up_after_the_reconnects(
    db: psycopg.Connection, connect_extra: Callable[[], psycopg.Connection]
) -> None:
    src = FakeCatalog({"C:/1/": [page_of(2, 0), page_of(2, 2)]})
    victims = _drop_on_fetch(src, db, {0}, once=False)

    def reconnect() -> psycopg.Connection:
        victims.append(connect_extra())
        return victims[-1]

    d, _ = deps(reconnect(), src, Clock(step=20))
    with pytest.raises(psycopg.OperationalError):
        run_crawl_reconnecting(d, T, 60, reconnect)
    assert len(victims) == DB_RECONNECTS + 1


def test_crawl_skips_when_the_schema_does_not_match(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con una migración pendiente el rastreo no escribe sobre un esquema que no conoce."""
    monkeypatch.setattr(repo, "SCHEMA_VERSION", repo.SCHEMA_VERSION + 1)
    d, transport = deps(db, FakeCatalog({"C:/1/": [page_of(2)]}), Clock())
    assert run_crawl(d, T, minutes=60).status == "skipped_schema"
    assert transport.urls == []
    assert count(db, "crawl_lease") == 0


def test_max_items_counts_the_catalog_once_per_run(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con ~150 mil filas de eBay, un `count(*)` por página costaba ~170 por corrida."""
    calls: list[str] = []
    real = repo.count_catalog_items

    def counting(conn: psycopg.Connection, source: str) -> int:
        calls.append(source)
        return real(conn, source)

    monkeypatch.setattr(repo, "count_catalog_items", counting)
    pages = [[eobs(f"90000000{p}00{i}", 5_000, None) for i in range(2)] for p in range(3)]
    src = FakeCatalog({"293": pages}, code=EBAY, currency=Currency.USD, max_batch=1)
    d, _ = deps(db, src, Clock(), max_items=3)
    run_crawl(d, T, minutes=60)
    assert calls == [EBAY]
    assert count(db, "catalog_item", "source = 'ebay_us'") == 4  # el tope se mira por página


SILLA = compile_interest("Silla de oficina", "hogar", ["silla ergonomica", "office chair"])
METER = compile_interest("Multímetro", "ferreteria", ["multimetro", "multimeter"])


def titled(ext: str, price: int, title: str, path: str = PATH) -> CatalogObservation:
    money = Money(price, Currency.COP)
    return CatalogObservation(ext, money, path, None, SellerClass.FIRST_PARTY, title=title)


def test_interest_match_alerts_in_an_interest_topic(db: psycopg.Connection) -> None:
    """Hogar va solo por interés: la silla ergonómica sale en tarjeta con su interés."""
    seed_history(db, "123", 199_900)
    seed_history(db, "124", 199_900)
    chair = titled("123", 119_900, "Silla Ergonómica Negra", "/Hogar/Muebles/")
    table = titled("124", 119_900, "Mesa de comedor", "/Hogar/Muebles/")
    offers = {"123": offer("123", 119_900), "124": offer("124", 119_900)}
    src = FakeCatalog({"C:/1/": [[chair, table]]}, offers=offers)
    d, _ = deps(db, src, Clock(), interests=(METER, SILLA))
    result = run_crawl(d, T, minutes=60)
    assert result.alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.topic == "hogar"
    assert row.card is not None and row.card["interest"] == "Silla de oficina"
    assert "🎯 Silla de oficina" in row.body_html
    logged = db.execute("SELECT interest FROM discountz.offer_log").fetchall()
    assert logged == [("Silla de oficina",)]


def test_interest_match_skips_the_price_floor(db: psycopg.Connection) -> None:
    """Bajo el precio mínimo (100.000) y con poco ahorro, pero coincide con un interés."""
    seed_history(db, "123", 79_900)
    seed_history(db, "124", 79_900)
    meter = titled("123", 59_900, "Multímetro Digital UNI-T")
    phone = titled("124", 59_900, "Celular básico")
    offers = {"123": offer("123", 59_900), "124": offer("124", 59_900)}
    src = FakeCatalog({"C:/1/": [[meter, phone]]}, offers=offers)
    d, _ = deps(db, src, Clock(), interests=(METER,))
    assert run_crawl(d, T, minutes=60).alerts == 1
    assert count(db, "alert") == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.card is not None and row.card["interest"] == "Multímetro"


def fill_interest_cap(conn: psycopg.Connection, name: str, topic: str) -> None:
    """Las 3 tarjetas del día del interés ya salieron de otra tienda."""
    for i in range(3):
        drop = 1000 + 100 * i
        admit(conn, Slot("panamericana", topic, AlertKind.CATALOG_DROP, drop, name), T, 8, "• x")


def test_full_interest_cap_leaves_a_card_topic_offer_to_the_source_cap(
    db: psycopg.Connection,
) -> None:
    """Tecnología va en tarjeta y el multímetro pasa los mínimos: sale aunque su interés ya
    gastó el tope, y cuenta para el tope de la tienda."""
    fill_interest_cap(db, "Multímetro", "tecnologia")
    seed_history(db, "123", 199_900)
    meter = titled("123", 119_900, "Multímetro Digital UNI-T")
    src = FakeCatalog({"C:/1/": [[meter]]}, offers={"123": offer("123", 119_900)})
    d, _ = deps(db, src, Clock(), interests=(METER,))
    assert run_crawl(d, T, minutes=60).alerts == 1
    (row,) = repo.claim_outbox(db, 10, T)
    assert row.card is not None and row.card["interest"] == "Multímetro"
    last = "SELECT source, interest FROM discountz.offer_log ORDER BY id DESC"
    assert db.execute(last).fetchone() == ("exito", None)


def test_full_interest_cap_holds_an_offer_only_the_interest_brings(
    db: psycopg.Connection,
) -> None:
    """Hogar va solo por interés: sin cupo en el interés, la silla espera al resumen."""
    fill_interest_cap(db, "Silla de oficina", "hogar")
    seed_history(db, "123", 199_900)
    chair = titled("123", 119_900, "Silla Ergonómica Negra", "/Hogar/Muebles/")
    src = FakeCatalog({"C:/1/": [[chair]]}, offers={"123": offer("123", 119_900)})
    d, _ = deps(db, src, Clock(), interests=(SILLA,))
    assert run_crawl(d, T, minutes=60).alerts == 0
    assert db.execute("SELECT delivery FROM discountz.alert").fetchone() == ("held",)


def test_full_interest_cap_holds_a_card_topic_offer_below_the_floors(
    db: psycopg.Connection,
) -> None:
    """Bajo el precio mínimo solo la saca el interés: sin cupo en él, espera al resumen."""
    fill_interest_cap(db, "Multímetro", "tecnologia")
    seed_history(db, "123", 79_900)
    meter = titled("123", 59_900, "Multímetro Digital UNI-T")
    src = FakeCatalog({"C:/1/": [[meter]]}, offers={"123": offer("123", 59_900)})
    d, _ = deps(db, src, Clock(), interests=(METER,))
    assert run_crawl(d, T, minutes=60).alerts == 0
    assert db.execute("SELECT delivery FROM discountz.alert").fetchone() == ("held",)
