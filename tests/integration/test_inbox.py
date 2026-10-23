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
    created: list[tuple[int, str]] = field(default_factory=list)

    def get_updates(self, offset: int, timeout_s: int = 0) -> list[Update]:
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.answered.append(callback_id)

    def create_forum_topic(self, chat_id: int, name: str) -> int:
        self.created.append((chat_id, name))
        return 100 + len(self.created)


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


def test_search_rejected_on_channel(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/buscar tg_clubgratis iphone")]])
    assert count(db, "search") == 0
    (reply,) = replies(db)
    assert "no admite /buscar" in reply


def test_status_command_replies_with_pre(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/estado")]])
    (reply,) = replies(db)
    assert reply.startswith("<pre>discountZ")


def test_temas_creates_missing_topics_once(db: psycopg.Connection) -> None:
    repo.save_topic(db, "errores", "🚨 Errores de precio", 7)  # ya existía
    _, tg = run(db, [[msg(1, "/temas")]])
    names = [name for _, name in tg.created]
    assert len(names) == 13
    assert all(chat == CHAT for chat, _ in tg.created)
    assert not any("Errores" in n for n in names)
    threads = repo.topic_threads(db)
    assert len(threads) == 14
    assert threads["errores"] == 7
    assert "Creé 13 temas" in replies(db)[0]
    _, again = run(db, [[msg(2, "/temas")]])
    assert again.created == []
    assert "ya existen" in replies(db)[1]


def _auto_and_manual(db: psycopg.Connection) -> tuple[int, int]:
    ids = []
    for ext, origin in (("1", "auto"), ("2", "manual")):
        o = offer(source="exito", external_id=ext)
        lid = repo.upsert_listing(db, o, "tecnologia", f"https://www.exito.com/{ext}/p", T).id
        row = db.execute(
            """INSERT INTO discountz.watch (source, listing_id, status, origin, next_check_at)
               VALUES ('exito', %s, 'active', %s, %s) RETURNING id""",
            (lid, origin, T),
        ).fetchone()
        assert row is not None
        ids.append(int(row[0]))
    return ids[0], ids[1]


def test_list_marks_auto_watches(db: psycopg.Connection) -> None:
    auto, manual = _auto_and_manual(db)
    run(db, [[msg(1, "/lista")]])
    text = replies(db)[0]
    assert f"a{auto} · " in text
    assert f"p{manual} · " in text


def test_drop_auto_removes_only_auto(db: psycopg.Connection) -> None:
    auto, manual = _auto_and_manual(db)
    run(db, [[msg(1, f"/dejar p{auto}")]])
    assert count(db, "watch") == 2  # pN no toca seguimientos automáticos
    run(db, [[msg(2, "/dejar auto")]])
    assert count(db, "watch") == 1
    assert "1 seguimiento automático" in replies(db)[1]
    run(db, [[msg(3, f"/dejar a{manual}")]])
    assert count(db, "watch") == 1


def _trip(db: psycopg.Connection, code: str) -> None:
    db.execute(
        """UPDATE discountz.source
              SET breaker = 'open', breaker_open_until = %s, breaker_trips = 3,
                  consecutive_unavailable = 5, consecutive_parse_failures = 2
            WHERE code = %s""",
        (T + timedelta(hours=8), code),
    )


def _breaker(db: psycopg.Connection, code: str) -> tuple[object, ...] | None:
    return db.execute(
        """SELECT breaker, breaker_open_until, breaker_trips, consecutive_unavailable,
                  consecutive_parse_failures, ramp_until
             FROM discountz.source WHERE code = %s""",
        (code,),
    ).fetchone()


def test_source_on_closes_an_open_breaker_without_ramp(db: psycopg.Connection) -> None:
    _trip(db, "exito")
    run(db, [[msg(1, "/fuente on exito")]])
    # Ya estaba activa: no se vuelve a fijar la rampa; las caídas previas (trips) se
    # conservan para que, si sigue caída, el siguiente disparo espere más.
    assert _breaker(db, "exito") == ("closed", None, 3, 0, 0, None)
    (reply,) = replies(db)
    assert "reinicié su breaker" in reply


def test_source_on_after_off_closes_breaker_and_sets_ramp(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/fuente off exito")]])
    _trip(db, "exito")
    run(db, [[msg(2, "/fuente on exito")]])
    state = _breaker(db, "exito")
    assert state is not None
    assert state[:5] == ("closed", None, 3, 0, 0)
    assert state[5] is not None
    assert "activada" in replies(db)[-1]


SEEDS = 55  # intereses de config/interests.toml


def test_interest_add_list_and_drop(db: psycopg.Connection) -> None:
    """La lista agrupa por tema, numera la semilla y luego los personales (spec del perfil §3.4)."""
    add = [msg(1, "/interes rtx 5070, RTX5070 #tecnologia"), msg(2, "/interes taladro percutor")]
    run(db, [add])
    rows = repo.list_interests(db)
    assert [(r.name, r.topic, r.terms) for r in rows] == [
        ("rtx 5070", "tecnologia", ("rtx 5070", "rtx5070")),
        ("taladro percutor", "ferreteria", ("taladro percutor",)),
    ]
    rtx, drill = (SEEDS + r.id for r in rows)
    added = replies(db)
    assert added[0].startswith(f"Listo: {rtx}. rtx 5070 en 📱 Tecnología.")
    assert f"Listo: {drill}. taladro percutor en 🔧 Ferretería y autos." in added[1]
    assert f"/interes quitar {drill}" in added[1]  # el tema salió del clasificador: cómo cambiarlo

    run(db, [[msg(3, "/interes")]])
    listing = replies(db)[2]
    assert "55 de la lista base (🌱) y 2 tuyos" in listing
    assert "\n1. 🌱 Portátil\n" in listing
    tech = listing.index("📱 Tecnología")
    assert tech < listing.index("24. 🌱 Tableta gráfica") < listing.index(f"{rtx}. rtx 5070")
    assert listing.index(f"{rtx}. rtx 5070") < listing.index("🔧 Ferretería y autos")
    assert listing.index("🔧 Ferretería y autos") < listing.index(f"{drill}. taladro percutor")

    run(db, [[msg(4, f"/interes quitar {rtx}"), msg(5, f"/interes quitar {rtx}")]])
    assert [r.name for r in repo.list_interests(db)] == ["taladro percutor"]
    assert replies(db)[3:] == [f"Listo: quité {rtx}. rtx 5070.", f"No encontré el interés {rtx}."]


def test_interest_numbers_do_not_shift_after_a_drop(db: psycopg.Connection) -> None:
    """Quitar dos seguidos con los números de la lista quita esos dos, no el que se corrió."""
    run(db, [[msg(1, "/interes kindle"), msg(2, "/interes kobo"), msg(3, "/interes boox")]])
    kindle, kobo, boox = (SEEDS + r.id for r in repo.list_interests(db))
    run(db, [[msg(4, f"/interes quitar {kindle}"), msg(5, f"/interes quitar {kobo}")]])
    assert [SEEDS + r.id for r in repo.list_interests(db)] == [boox]


def test_interest_add_rejects_a_seed_name_with_other_case_or_accents(
    db: psycopg.Connection,
) -> None:
    run(db, [[msg(1, "/interes MULTIMETRO, multimetro digital"), msg(2, "/interes Ssd")]])
    run(db, [[msg(3, "/interes rtx 5070"), msg(4, "/interes RTX-5070")]])
    assert [r.name for r in repo.list_interests(db)] == ["rtx 5070"]
    texts = replies(db)
    assert "Multímetro" in texts[0] and "Multímetro" not in texts[1]
    assert "SSD" in texts[1]
    assert "rtx 5070" in texts[3] and texts[3].startswith("Ya existe")


def test_interest_add_rejects_the_101st(db: psycopg.Connection) -> None:
    for i in range(100):
        repo.add_interest(db, f"interes {i:03}", "otros", [f"termino {i:03}"], T)
    run(db, [[msg(1, "/interes uno mas")]])
    assert len(repo.list_interests(db)) == 100
    assert "100" in replies(db)[0] and "/interes quitar" in replies(db)[0]


def test_interest_drop_of_a_seed_is_rejected(db: psycopg.Connection) -> None:
    run(db, [[msg(1, "/interes quitar 1")]])
    text = replies(db)[0]
    assert "Portátil" in text and "config/interests.toml" in text
