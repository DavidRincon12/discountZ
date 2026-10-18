"""Entrega con tarjeta y tema (spec del rastreo §5.1, §5.3 y §5.4)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from PIL import Image
from pydantic import SecretStr

from discountz.app.compose import compose_alerts
from discountz.app.deliver import deliver, make_photo_fetcher
from discountz.config import load_params
from discountz.infra import db as repo
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.infra.telegram import TelegramClient
from discountz.sources.registry import load_all

pytestmark = pytest.mark.integration
T = datetime(2026, 10, 16, 15, tzinfo=UTC)
TOKEN = "123456:ABC-secreto_de_prueba"  # noqa: S105 - token falso de prueba
API = f"https://api.telegram.org/bot{TOKEN}"
IMG = "https://exitocol.vteximg.com.br/arquivos/ids/1/a.jpg"
CARD = {
    "store": "exito",
    "topic": "tecnologia",
    "title": "Celular",
    "url": "https://www.exito.com/celular/p",
    "image_url": IMG,
    "price_minor": 59_900,
    "usual_minor": 99_900,
    "currency": "COP",
    "drop_bp": 4004,
    "min_days": 5,
    "kind": "catalog_drop",
}


def ok(message_id: int = 1) -> HttpResponse:
    body = json.dumps({"ok": True, "result": {"message_id": message_id}}).encode()
    return HttpResponse(200, API, {}, body, 1)


def thread_not_found() -> HttpResponse:
    body = {"ok": False, "error_code": 400, "description": "Bad Request: message thread not found"}
    return HttpResponse(400, API, {}, json.dumps(body).encode(), 1)


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def enqueue(
    conn: psycopg.Connection,
    key: str,
    *,
    priority: int = 3,
    topic: str | None = None,
    card: dict[str, object] | None = None,
) -> int:
    return repo.enqueue(
        conn,
        "alert",
        f"<b>{key}</b>",
        priority=priority,
        dedup_key=key,
        now=T,
        ttl=timedelta(hours=24),
        topic=topic,
        card=card,
    )


def forbidden() -> HttpResponse:
    body = {"ok": False, "error_code": 403, "description": "Forbidden: bot is not a member"}
    return HttpResponse(403, API, {}, json.dumps(body).encode(), 1)


def run(
    conn: psycopg.Connection,
    routes: dict[str, HttpResponse | list[HttpResponse]],
    photos: object = None,
    channel_id: int | None = None,
) -> FakeHttp:
    http = FakeHttp(routes)
    tg = TelegramClient(SecretStr(TOKEN), http)
    deliver(
        conn,
        tg,
        -100,
        T,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
        deadline=1e9,
        photos=photos,  # type: ignore[arg-type]
        channel_id=channel_id,
    )
    return http


def test_deliver_card_row_sends_photo_to_topic(db: psycopg.Connection) -> None:
    repo.save_topic(db, "tecnologia", "📱 Tecnología", 11)
    enqueue(db, "cat:1", topic="tecnologia", card=CARD)
    seen: list[tuple[str, str]] = []

    def photos(source: str, url: str) -> bytes | None:
        seen.append((source, url))
        return jpeg()

    http = run(db, {f"{API}/sendPhoto": ok(5)}, photos)
    assert seen == [("exito", IMG)]
    data, files = http.uploads[0]
    assert data is not None and files is not None
    assert data["message_thread_id"] == "11"
    assert data["caption"] == "<b>cat:1</b>"
    png = Image.open(io.BytesIO(files["photo"][1]))
    assert png.size == (1080, 1080)
    row = db.execute("SELECT status, telegram_message_id FROM discountz.outbox").fetchone()
    assert row == ("sent", 5)


def test_deliver_text_row_goes_to_topic_thread(db: psycopg.Connection) -> None:
    repo.save_topic(db, "hogar", "🛋️ Hogar y muebles", 22)
    enqueue(db, "alert:1", topic="hogar")
    http = run(db, {f"{API}/sendMessage": ok()})
    assert http.calls[0][2]["message_thread_id"] == 22  # type: ignore[index]


def test_deliver_falls_back_to_general_when_topic_missing(db: psycopg.Connection) -> None:
    repo.save_topic(db, "hogar", "🛋️ Hogar y muebles", 22)
    enqueue(db, "alert:1", topic="hogar")
    enqueue(db, "alert:2", topic="moda")  # sin hilo guardado
    http = run(db, {f"{API}/sendMessage": [thread_not_found(), ok(2), ok(3)]})
    threads = [c[2].get("message_thread_id") for c in http.calls]  # type: ignore[union-attr]
    assert threads == [22, None, None]
    notices = db.execute(
        "SELECT body_html FROM discountz.outbox WHERE purpose = 'system'"
    ).fetchall()
    assert len(notices) == 1
    assert "/temas" in notices[0][0]


def test_deliver_skips_photo_from_foreign_host(db: psycopg.Connection) -> None:
    repo.save_topic(db, "tecnologia", "📱 Tecnología", 11)
    foreign = CARD | {"image_url": "https://evil.example.com/a.jpg"}
    enqueue(db, "cat:1", topic="tecnologia", card=foreign)
    net = FakeHttp({})  # cualquier petición haría fallar la prueba
    photos = make_photo_fetcher(net, load_all())
    assert photos("exito", "https://evil.example.com/a.jpg") is None
    assert photos("exito", "http://exitocol.vteximg.com.br/a.jpg") is None  # sin https
    assert photos("desconocida", IMG) is None
    http = run(db, {f"{API}/sendPhoto": ok()}, photos)
    assert net.calls == []
    assert len(http.uploads) == 1


def test_photo_fetcher_validates_image(db: psycopg.Connection) -> None:
    net = FakeHttp(
        {
            IMG: HttpResponse(200, IMG, {}, jpeg(), 1),
            IMG + "?x": HttpResponse(200, IMG, {}, b"<html>no</html>", 1),
        }
    )
    photos = make_photo_fetcher(net, load_all())
    assert photos("exito", IMG) is not None
    assert photos("exito", IMG + "?x") is None


def test_deliver_order_price_error_first(db: psycopg.Connection) -> None:
    enqueue(db, "cat:drop", priority=5)
    enqueue(db, "cat:big", priority=2)
    enqueue(db, "cat:error", priority=0)
    http = run(db, {f"{API}/sendMessage": ok()})
    texts = [c[2]["text"] for c in http.calls]  # type: ignore[index]
    assert texts == ["<b>cat:error</b>", "<b>cat:big</b>", "<b>cat:drop</b>"]


def test_compose_assigns_topic_from_category(db: psycopg.Connection) -> None:
    for ext, source, category, kind in (
        ("1", "exito", "componentes", "good_deal"),
        ("2", "slickdeals", "tecnologia", "community_deal"),
    ):
        db.execute(
            """INSERT INTO discountz.listing (source, external_id, canonical_url, title, category,
                   first_seen_at, last_seen_at, last_in_stock, last_seller_class)
               VALUES (%s, %s, 'https://www.exito.com/x/p', 't', %s, now(), now(), true, 'f')""",
            (source, ext, category),
        )
        db.execute(
            """INSERT INTO discountz.alert (listing_id, kind, maturity, price_minor, observed_at)
               SELECT id, %s, 'warm', 59900, now() FROM discountz.listing WHERE external_id = %s""",
            (kind, ext),
        )
    compose_alerts(db, {}, T, load_params())
    topics: dict[str, str] = dict(
        db.execute("SELECT dedup_key, topic FROM discountz.outbox ORDER BY id").fetchall()
    )
    assert sorted(topics.values()) == ["comunidad_us", "tecnologia"]


def test_deliver_without_any_topic_sends_to_general_without_notice(
    db: psycopg.Connection,
) -> None:
    enqueue(db, "alert:1", topic="hogar")  # el grupo aún no tiene temas (/temas sin correr)
    http = run(db, {f"{API}/sendMessage": ok()})
    assert [c[2].get("message_thread_id") for c in http.calls] == [None]  # type: ignore[union-attr]
    assert db.execute(
        "SELECT count(*) FROM discountz.outbox WHERE purpose = 'system'"
    ).fetchone() == (0,)


def test_thread_not_found_forgets_topic_so_temas_recreates_it(db: psycopg.Connection) -> None:
    repo.save_topic(db, "hogar", "🛋️ Hogar y muebles", 22)
    repo.save_topic(db, "moda", "👟 Moda y calzado", 33)
    enqueue(db, "alert:1", topic="hogar")
    enqueue(db, "alert:2", topic="hogar")
    http = run(db, {f"{API}/sendMessage": [thread_not_found(), ok(2), ok(3)]})
    threads = [c[2].get("message_thread_id") for c in http.calls]  # type: ignore[union-attr]
    assert threads == [22, None, None]  # la segunda ya no insiste en el hilo borrado
    assert repo.topic_threads(db) == {"moda": 33}


CHANNEL = -1009


def test_deliver_mirrors_card_to_channel_without_topic_or_callbacks(
    db: psycopg.Connection,
) -> None:
    repo.save_topic(db, "tecnologia", "📱 Tecnología", 11)
    markup = {
        "inline_keyboard": [
            [{"text": "Abrir en Éxito", "url": "https://www.exito.com/celular/p"}],
            [{"text": "Seguir", "callback_data": "fol:L1"}],
        ]
    }
    repo.enqueue(
        db,
        "alert",
        "<b>cat:1</b>",
        priority=3,
        dedup_key="cat:1",
        now=T,
        ttl=timedelta(hours=24),
        reply_markup=markup,
        topic="tecnologia",
        card=CARD,
    )
    http = run(db, {f"{API}/sendPhoto": [ok(5), ok(6)]}, channel_id=CHANNEL)
    (group, _), (channel, files) = http.uploads
    assert group is not None and group["chat_id"] == "-100"
    assert channel is not None and files is not None
    assert channel["chat_id"] == str(CHANNEL)
    assert "message_thread_id" not in channel
    assert channel["caption"] == "<b>cat:1</b>"
    link_row = markup["inline_keyboard"][0]
    assert json.loads(channel["reply_markup"]) == {"inline_keyboard": [link_row]}
    row = db.execute("SELECT status, telegram_message_id FROM discountz.outbox").fetchone()
    assert row == ("sent", 5)  # el id guardado es el del grupo


def test_deliver_mirrors_only_offers_to_channel(db: psycopg.Connection) -> None:
    rows: tuple[tuple[repo.Purpose, str], ...] = (
        ("alert", "a"),
        ("digest", "d"),
        ("system", "s"),
        ("reply", "r"),
    )
    for purpose, text in rows:
        repo.enqueue(
            db, purpose, text, priority=3, dedup_key=f"k:{text}", now=T, ttl=timedelta(hours=24)
        )
    http = run(db, {f"{API}/sendMessage": ok()}, channel_id=CHANNEL)
    sent = [(c[2]["chat_id"], str(c[2]["text"])) for c in http.calls]  # type: ignore[index]
    assert sorted(t for chat, t in sent if chat == CHANNEL) == ["a", "d"]
    assert sorted(t for chat, t in sent if chat == -100) == ["a", "d", "r", "s"]
    to_channel = [c[2] for c in http.calls if c[2] is not None and c[2]["chat_id"] == CHANNEL]
    assert [p.get("reply_markup") for p in to_channel] == [None, None]


def test_channel_failure_does_not_touch_group_delivery(db: psycopg.Connection) -> None:
    enqueue(db, "alert:1")
    enqueue(db, "alert:2")
    http = run(db, {f"{API}/sendMessage": [ok(1), forbidden(), ok(2), ok(3)]}, channel_id=CHANNEL)
    chats = [c[2]["chat_id"] for c in http.calls]  # type: ignore[index]
    assert chats == [-100, CHANNEL, -100]  # sin permiso en el canal: no se insiste en el ciclo
    rows = db.execute("SELECT status FROM discountz.outbox ORDER BY id").fetchall()
    assert rows == [("sent",), ("sent",)]


def test_channel_mirror_uses_plain_text_when_html_was_rejected(db: psycopg.Connection) -> None:
    enqueue(db, "alert:1")
    bad = HttpResponse(
        400,
        API,
        {},
        json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: x"}).encode(),
        1,
    )
    http = run(db, {f"{API}/sendMessage": [bad, ok(1), ok(2)]}, channel_id=CHANNEL)
    mirror = http.calls[-1][2]
    assert mirror is not None and mirror["chat_id"] == CHANNEL
    assert mirror["text"] == "alert:1" and "parse_mode" not in mirror
