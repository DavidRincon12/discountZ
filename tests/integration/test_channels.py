"""Paso del ciclo que lee los canales de Telegram (spec de canales §2, §4 y §5)."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from discountz.app.channels import ChannelsResult, due_channels, run_channels
from discountz.app.collect import SourceRun
from discountz.config import load_params, load_topics
from discountz.domain.community import ChannelPost
from discountz.domain.errors import ParseError
from discountz.domain.health import BreakerState, effective_state
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra import db as repo
from discountz.infra.http import FakeHttp, HttpResponse, PacedClient
from discountz.sources.base import ChannelPage, SourceContext, SourceDescriptor
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import load_all

pytestmark = pytest.mark.integration

T = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
CLOSED = BreakerState("closed", None, 0, 0, 0, 0)
PARAMS = load_params()
TOPICS = load_topics()
URL = "https://t.me/s/fake"


def deal(
    channel: str,
    post_id: int,
    *,
    title: str = "Set de 7 cuchillos de cocina",
    coupon: str | None = None,
    age: timedelta = timedelta(minutes=5),
    price: int = 1200,
) -> ChannelPost:
    return ChannelPost(
        channel=channel,
        post_id=post_id,
        published_at=T - age,
        title=title,
        price=Money(price, Currency.USD),
        coupon=coupon,
        store=None,
        hashtags=("cocina",),
        image_url=None,
        link=SafeUrl.parse(f"https://t.me/x/{post_id}", frozenset({"t.me"})),
    )


@dataclass
class FakeChannel:
    """Canal falso: una petición por `fetch_posts` y páginas programadas."""

    code: str
    pages: list[ChannelPage | Exception]
    afters: list[int | None] = field(default_factory=list)
    channel: str = "fake"

    def __post_init__(self) -> None:
        real = load_all()[self.code].descriptor
        self.descriptor: SourceDescriptor = real

    def fetch_posts(self, ctx: SourceContext, after_id: int | None) -> ChannelPage:
        ctx.http.request("GET", URL)
        self.afters.append(after_id)
        item = self.pages[min(len(self.afters) - 1, len(self.pages) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    def detect_block(self, resp: HttpResponse) -> None:
        return detect_block_common(resp)  # type: ignore[return-value]


def run_for(fake: FakeChannel, breaker: BreakerState = CLOSED) -> SourceRun:
    budget = PARAMS.budgets[fake.code]
    paced = PacedClient(
        FakeHttp({URL: HttpResponse(200, URL, {}, b"ok", 0)}),
        budget,
        lambda s: None,
        lambda: 0.0,
        random.Random(1),
        fake.detect_block,
        source=fake.code,
    )
    return SourceRun(fake, paced, budget, breaker, deque())  # type: ignore[arg-type]


def run(conn: psycopg.Connection, *fakes: FakeChannel, now: datetime = T) -> ChannelsResult:
    return run_channels(
        conn, [run_for(f) for f in fakes], now, PARAMS, TOPICS, random.Random(1), 1e9
    )


def page(
    *posts: ChannelPost, max_id: int | None = None, more: bool = False, unparsed: int = 0
) -> ChannelPage:
    top = max_id if max_id is not None else max((p.post_id for p in posts), default=None)
    return ChannelPage(tuple(posts), top, more, unparsed, unparsed)


def outbox(conn: psycopg.Connection) -> list[tuple[str, str | None, str]]:
    rows = conn.execute(
        "SELECT dedup_key, topic, card->>'kind' FROM discountz.outbox ORDER BY id"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def test_first_read_saves_cursor_without_sending(db: psycopg.Connection) -> None:
    fake = FakeChannel("tg_cajitatech", [page(*(deal("tg_cajitatech", i) for i in range(1, 6)))])
    result = run(db, fake)
    assert outbox(db) == []
    assert repo.get_channel_cursor(db, "tg_cajitatech") == repo.ChannelCursor(5, T)
    assert fake.afters == [None]
    assert result.requests == 1 and result.sent == 0


def test_new_posts_are_queued_in_their_topic(db: psycopg.Connection) -> None:
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    fake = FakeChannel(
        "tg_cajitatech",
        [page(deal("tg_cajitatech", 101), deal("tg_cajitatech", 102, title="Teclado mecánico"))],
    )
    result = run(db, fake)
    assert outbox(db) == [
        ("canal:tg_cajitatech:101", "hogar", "community_deal"),
        ("canal:tg_cajitatech:102", "tecnologia", "community_deal"),
    ]
    assert fake.afters == [100]
    assert repo.get_channel_cursor(db, "tg_cajitatech") == repo.ChannelCursor(102, T)
    assert result.sent == 2
    row = db.execute(
        "SELECT priority, reply_markup IS NOT NULL FROM discountz.outbox LIMIT 1"
    ).fetchone()
    assert row == (9, True)


def test_pages_follow_after_until_three(db: psycopg.Connection) -> None:
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    pages: list[ChannelPage | Exception] = [
        page(deal("tg_cajitatech", 100 + i * 20), more=True) for i in range(1, 5)
    ]
    fake = FakeChannel("tg_cajitatech", pages)
    run(db, fake)
    assert fake.afters == [100, 120, 140]
    assert repo.get_channel_cursor(db, "tg_cajitatech") == repo.ChannelCursor(160, T)


def test_old_posts_are_marked_read_not_sent(db: psycopg.Connection) -> None:
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    fake = FakeChannel("tg_cajitatech", [page(deal("tg_cajitatech", 101, age=timedelta(hours=7)))])
    result = run(db, fake)
    assert outbox(db) == []
    assert repo.get_channel_cursor(db, "tg_cajitatech") == repo.ChannelCursor(101, T)
    assert result.skipped == 1


def test_cross_channel_duplicate_sent_once(db: psycopg.Connection) -> None:
    repo.save_channel_cursor(db, "tg_clubgratis", 59500, T - timedelta(hours=1))
    repo.save_channel_cursor(db, "tg_cajitatech", 88160, T - timedelta(hours=1))
    club = FakeChannel(
        "tg_clubgratis", [page(deal("tg_clubgratis", 59510, title="TV OLED", coupon="TVZE10"))]
    )
    caj = FakeChannel(
        "tg_cajitatech", [page(deal("tg_cajitatech", 88168, title="Televisor", coupon="tvze10"))]
    )
    result = run(db, club, caj)
    assert [k for k, _, _ in outbox(db)] == ["canal:tg_clubgratis:59510"]
    dup = db.execute(
        "SELECT duplicate_of FROM discountz.channel_post WHERE channel = 'tg_cajitatech'"
    ).fetchone()
    assert dup == ("tg_clubgratis:59510",)
    assert result.duplicates == 1


def test_replayed_page_does_not_resend(db: psycopg.Connection) -> None:
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    pages: list[ChannelPage | Exception] = [page(deal("tg_cajitatech", 101))]
    run(db, FakeChannel("tg_cajitatech", pages))
    db.execute("UPDATE discountz.channel_cursor SET last_id = 100")
    run(db, FakeChannel("tg_cajitatech", pages), now=T + timedelta(minutes=10))
    assert len(outbox(db)) == 1


def test_failure_mid_page_keeps_cursor(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    real = repo.enqueue
    calls = {"n": 0}

    def flaky(*args: object, **kw: object) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("falla simulada")
        return real(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(repo, "enqueue", flaky)
    fake = FakeChannel(
        "tg_cajitatech",
        [page(deal("tg_cajitatech", 101), deal("tg_cajitatech", 102, title="Teclado mecánico"))],
    )
    run(db, fake)
    assert outbox(db) == []
    assert repo.get_channel_cursor(db, "tg_cajitatech") == repo.ChannelCursor(
        100, T - timedelta(hours=1)
    )
    assert db.execute("SELECT count(*) FROM discountz.channel_post").fetchone() == (0,)


def test_parse_error_opens_breaker_after_threshold(db: psycopg.Connection) -> None:
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    breaker = CLOSED
    now = T
    for _ in range(3):
        r = run_for(FakeChannel("tg_cajitatech", [ParseError("format")]), breaker)
        run_channels(db, [r], now, PARAMS, TOPICS, random.Random(1), 1e9)
        assert r.failed
        breaker = r.breaker
        now += timedelta(minutes=10)
    assert effective_state(breaker, now) == "open"
    due = due_channels(
        db, load_all(), PARAMS.budgets, {"tg_cajitatech": breaker}, now, PARAMS.channel_interval
    )
    assert "tg_cajitatech" not in due


def test_channel_due_every_ten_minutes(db: psycopg.Connection) -> None:
    def due(now: datetime) -> Sequence[str]:
        return due_channels(db, load_all(), PARAMS.budgets, {}, now, PARAMS.channel_interval)

    assert set(due(T)) == {
        "tg_descuentostech",
        "tg_clubgratis",
        "tg_cajitatech",
        "dealnews",
        "slickdeals_amazon",
    }
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T)
    assert "tg_cajitatech" not in due(T + timedelta(minutes=5))
    assert "tg_cajitatech" in due(T + timedelta(minutes=10))
    # Un canal apagado con /fuente off no tiene presupuesto en la entrada del plan.
    budgets = {k: v for k, v in PARAMS.budgets.items() if k != "tg_clubgratis"}
    assert "tg_clubgratis" not in due_channels(
        db, load_all(), budgets, {}, T, PARAMS.channel_interval
    )


def test_unparsed_streak_across_reads_is_parse_error(db: psycopg.Connection) -> None:
    """Formato roto a ritmo normal: una publicación con precio y sin oferta por lectura."""
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    now = T
    results = []
    for i in range(1, 6):
        r = run_for(FakeChannel("tg_cajitatech", [page(max_id=100 + i, unparsed=1)]))
        run_channels(db, [r], now, PARAMS, TOPICS, random.Random(1), 1e9)
        results.append((r.failed, r.counters.parse_errors))
        now += timedelta(minutes=10)
    assert results == [(False, 0)] * 4 + [(True, 1)]
    cursor = repo.get_channel_cursor(db, "tg_cajitatech")
    assert cursor is not None and cursor.last_id == 105
    # Una oferta que sí se lee reinicia la racha.
    r = run_for(FakeChannel("tg_cajitatech", [page(deal("tg_cajitatech", 106))]))
    run_channels(db, [r], now, PARAMS, TOPICS, random.Random(1), 1e9)
    assert not r.failed
    cursor = repo.get_channel_cursor(db, "tg_cajitatech")
    assert cursor is not None and cursor.unparsed_streak == 0


def test_failing_channel_does_not_stop_the_others(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.save_channel_cursor(db, "tg_clubgratis", 59500, T - timedelta(hours=1))
    repo.save_channel_cursor(db, "tg_cajitatech", 100, T - timedelta(hours=1))
    real = repo.insert_channel_post

    def boom(conn: psycopg.Connection, code: str, *args: object, **kw: object) -> bool:
        if code == "tg_clubgratis":
            raise RuntimeError("falla simulada")
        return real(conn, code, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(repo, "insert_channel_post", boom)
    club = run_for(FakeChannel("tg_clubgratis", [page(deal("tg_clubgratis", 59501))]))
    caj = run_for(FakeChannel("tg_cajitatech", [page(deal("tg_cajitatech", 101))]))
    result = run_channels(db, [club, caj], T, PARAMS, TOPICS, random.Random(1), 1e9)
    assert club.failed and not caj.failed
    assert [k for k, _, _ in outbox(db)] == ["canal:tg_cajitatech:101"]
    assert result.sent == 1
