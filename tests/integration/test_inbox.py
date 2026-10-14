"""Bandeja de entrada de Telegram contra Postgres real (spec §5.1, §5.3 y §5.4)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

import psycopg
import pytest

from discountz.app.inbox import process_inbox
from discountz.domain.model import ListingRef
from discountz.domain.money import Currency
from discountz.domain.urls import SafeUrl
from discountz.infra import db as repo
from discountz.infra.telegram import CallbackQuery, IncomingMessage, Update
from discountz.sources.base import NeedsResolution, Source, SourceDescriptor
from discountz.sources.registry import load_all
from tests.factories import offer
from tests.unit.series import T

pytestmark = pytest.mark.integration
OWNER = 42
CHAT = 42
STRANGER = 666


@dataclass
class FakeTelegram:
    """Devuelve lotes en orden, sin mirar el offset (simula reentregas)."""

    batches: list[list[Update]]
    offsets: list[int] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)

    def get_updates(self, offset: int, timeout_s: int = 0) -> list[Update]:
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.answered.append(callback_id)


def msg(update_id: int, text: str, *, from_id: int = OWNER, chat_id: int = CHAT) -> Update:
    return Update(update_id, IncomingMessage(update_id * 10, from_id, chat_id, text), None)


def cb(update_id: int, data: str, *, from_id: int = OWNER) -> Update:
    return Update(update_id, None, CallbackQuery(f"q{update_id}", from_id, CHAT, 500, data))


class Boom:
    """Adaptador que explota al leer la URL: un update venenoso."""

    descriptor = SourceDescriptor(
        code="boom",
        kind="store",
        currency=Currency.COP,
        hosts=frozenset({"www.boom.test"}),
        client="httpx",
        capabilities=frozenset(),
        max_batch=1,
        external_id_re=re.compile(r"^\d+$"),
    )

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        raise RuntimeError("boom")


def run(
    db: psycopg.Connection,
    batches: list[list[Update]],
    sources: Mapping[str, Source] | None = None,
) -> tuple[int, FakeTelegram]:
    tg = FakeTelegram(batches)
    n = process_inbox(db, tg, OWNER, CHAT, T, sources=sources or load_all())
    return n, tg


def offset(db: psycopg.Connection) -> int:
    return repo.telegram_offset(db)


def count(db: psycopg.Connection, table: str) -> int:
    row = db.execute(f"SELECT count(*) FROM discountz.{table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


def replies(db: psycopg.Connection) -> list[str]:
    rows = db.execute(
        "SELECT body_html FROM discountz.outbox WHERE purpose = 'reply' ORDER BY id"
    ).fetchall()
    return [r[0] for r in rows]


def test_foreign_follow_writes_nothing_and_advances_offset(db: psycopg.Connection) -> None:
    n, _ = run(
        db,
        [
            [
                msg(7, "/seguir https://www.exito.com/tv-55/p", from_id=STRANGER),
                msg(8, "/ayuda", chat_id=STRANGER),
            ]
        ],
    )
    assert n == 0
    assert count(db, "watch") == 0
    assert count(db, "outbox") == 0
    assert offset(db) == 8


def test_follow_creates_watch_and_acknowledges(db: psycopg.Connection) -> None:
    n, tg = run(db, [[msg(1, "/seguir https://www.exito.com/tv-55/p 1.200.000 #tecnologia")]])
    assert n == 1
    row = db.execute(
        """SELECT source, pending_ref, requested_category, target_price_minor, reply_to_message_id
             FROM discountz.watch"""
    ).fetchone()
    assert row == ("exito", "slug:tv-55", "tecnologia", 1_200_000, 10)
    (reply,) = replies(db)
    assert reply.startswith("p")
    assert "Éxito" in reply
    assert offset(db) == 1
    assert tg.offsets == [1, 2]


def test_follow_with_userinfo_is_rejected(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/seguir https://www.exito.com@evil.com/x")]])
    assert count(db, "watch") == 0
    (reply,) = replies(db)
    assert "rechazada" in reply
    assert "evil.com" not in reply


def test_poison_update_does_not_block_the_next(db: psycopg.Connection) -> None:
    sources = {**load_all(), "boom": Boom()}
    n, _ = run(
        db,
        [
            [
                msg(1, "/seguir https://www.boom.test/x/p"),
                msg(2, "/seguir https://www.exito.com/a/p"),
            ]
        ],
        sources,  # type: ignore[arg-type]
    )
    assert n == 2
    assert count(db, "watch") == 1
    texts = replies(db)
    assert texts[0] == "No pude procesar ese mensaje (ref U1)."
    assert len(texts) == 2
    assert offset(db) == 2


def test_redelivered_update_creates_one_watch(db: psycopg.Connection) -> None:
    same = msg(5, "/seguir https://www.exito.com/tv-55/p")
    run(db, [[same], [same]])
    run(db, [[same]])
    assert count(db, "watch") == 1
    assert len(replies(db)) == 1
    assert offset(db) == 5


def test_malformed_and_unknown_commands_get_a_reply(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/objetivo p1 abc"), msg(2, "hola"), msg(3, "/ayuda")]])
    texts = replies(db)
    assert "Formato de precio no válido" in texts[0]
    assert "/ayuda" in texts[1]
    assert "siguiente ciclo" in texts[2]


def _listing(db: psycopg.Connection, external_id: str = "123456") -> int:
    state = repo.upsert_listing(
        db, offer(external_id=external_id), "otros", "https://www.exito.com/x/p", T
    )
    return state.id


def test_callbacks_follow_mute_and_unfollow(db: psycopg.Connection) -> None:
    lid = _listing(db)
    run(db, [[cb(1, f"fol:L{lid}"), cb(2, f"mute:L{lid}:30")]])
    wid = db.execute("SELECT id FROM discountz.watch WHERE listing_id = %s", (lid,)).fetchone()
    assert wid is not None
    muted = db.execute("SELECT muted_until FROM discountz.listing WHERE id = %s", (lid,)).fetchone()
    assert muted == (T + timedelta(days=30),)
    _, tg = run(db, [[cb(3, f"unf:p{wid[0]}")]])
    assert count(db, "watch") == 0
    assert tg.answered == ["q3"]
    assert len(replies(db)) == 3


def test_foreign_callback_is_ignored(db: psycopg.Connection) -> None:
    lid = _listing(db)
    _, tg = run(db, [[cb(1, f"fol:L{lid}", from_id=STRANGER)]])
    assert count(db, "watch") == 0
    assert tg.answered == []
    assert offset(db) == 1


def test_target_category_silence_source_and_list(db: psycopg.Connection) -> None:
    lid = _listing(db)
    wid = repo.create_watch_on_listing(db, "exito", lid, None, T).id
    run(
        db,
        [
            [
                msg(1, f"/objetivo p{wid} 250.000"),
                msg(2, f"/categoria p{wid} hogar"),
                msg(3, "/silenciar todo 2"),
                msg(4, "/fuente off amazon_us"),
                msg(5, "/buscar exito ssd 1tb -carcasa max:400.000 #componentes"),
                msg(6, "/lista"),
                msg(7, "/dejar p999"),
            ]
        ],
    )
    w = db.execute(
        "SELECT target_price_minor, target_set_at FROM discountz.watch WHERE id = %s", (wid,)
    ).fetchone()
    assert w == (250_000, T)
    cat = db.execute("SELECT category FROM discountz.listing WHERE id = %s", (lid,)).fetchone()
    assert cat == ("hogar",)
    bot = db.execute("SELECT muted_until FROM discountz.bot_state").fetchone()
    assert bot == (T + timedelta(days=2),)
    src = db.execute("SELECT enabled FROM discountz.source WHERE code = 'amazon_us'").fetchone()
    assert src == (False,)
    s = db.execute(
        "SELECT query, exclude_terms, max_price_minor, category FROM discountz.search"
    ).fetchone()
    assert s == ("ssd 1tb", ["carcasa"], 400_000, "componentes")
    texts = replies(db)
    assert len(texts) == 7
    assert any("#hogar" in t and f"p{wid}" in t for t in texts)
    assert "No encontré p999" in texts[-1]


def test_search_rejected_on_source_without_search(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/buscar newegg ssd 1tb")]])
    assert count(db, "search") == 0
    (reply,) = replies(db)
    assert "no admite /buscar" in reply


def test_status_command_replies_with_pre(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/estado")]])
    (reply,) = replies(db)
    assert reply.startswith("<pre>discountZ")
