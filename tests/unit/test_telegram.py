"""Cliente de la API de Telegram sobre `FakeHttp` (spec §5.1, §5.4, §5.5 y §7.1)."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from discountz.domain.errors import (
    TelegramBadRequest,
    TelegramConflict,
    TelegramError,
    TelegramForbidden,
    TelegramRetryAfter,
    Unavailable,
)
from discountz.infra.http import FakeHttp, HttpResponse
from discountz.infra.telegram import CallbackQuery, IncomingMessage, TelegramClient, Update

TOKEN = "123456:ABC-secreto_de_prueba"  # noqa: S105 - token falso de prueba
API = f"https://api.telegram.org/bot{TOKEN}"


def resp(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status, "https://api.telegram.org/", {}, json.dumps(payload).encode(), 5)


def client(routes: dict[str, HttpResponse | list[HttpResponse]]) -> tuple[TelegramClient, FakeHttp]:
    http = FakeHttp(routes)
    return TelegramClient(SecretStr(TOKEN), http), http


def test_get_updates_parses_messages_and_callbacks() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 10,
                "message": {
                    "message_id": 5,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": 42, "type": "private"},
                    "text": "/ayuda",
                },
            },
            {
                "update_id": 11,
                "callback_query": {
                    "id": "cbq1",
                    "from": {"id": 42},
                    "message": {"message_id": 7, "chat": {"id": 42}},
                    "data": "mute:L1:30",
                },
            },
            {"update_id": 12, "edited_message": {"weird": True}},
            {"update_id": 13, "message": {"message_id": "x"}},
        ],
    }
    tg, http = client({f"{API}/getUpdates": resp(payload)})
    updates = tg.get_updates(offset=10)
    assert updates == [
        Update(10, IncomingMessage(5, 42, 42, "/ayuda"), None),
        Update(11, None, CallbackQuery("cbq1", 42, 42, 7, "mute:L1:30")),
        Update(12, None, None),
        Update(13, None, None),
    ]
    method, _, body = http.calls[0]
    assert method == "POST"
    assert body == {
        "offset": 10,
        "timeout": 0,
        "limit": 100,
        "allowed_updates": ["message", "callback_query"],
    }


def test_send_message_posts_html_without_preview() -> None:
    tg, http = client({f"{API}/sendMessage": resp({"ok": True, "result": {"message_id": 99}})})
    markup = {"inline_keyboard": [[{"text": "Seguir", "callback_data": "fol:L1"}]]}
    mid = tg.send_message(42, "<b>hola</b>", markup, silent=True, reply_to_message_id=5)
    assert mid == 99
    assert http.calls[0][2] == {
        "chat_id": 42,
        "text": "<b>hola</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "disable_notification": True,
        "reply_markup": markup,
        "reply_parameters": {"message_id": 5, "allow_sending_without_reply": True},
    }


def test_send_plain_text_omits_parse_mode() -> None:
    tg, http = client({f"{API}/sendMessage": resp({"ok": True, "result": {"message_id": 1}})})
    tg.send_message(42, "a < b", None, silent=False, parse_mode=None)
    body = http.calls[0][2]
    assert body is not None
    assert "parse_mode" not in body
    assert "reply_markup" not in body


def test_edit_and_answer_callback() -> None:
    tg, http = client(
        {
            f"{API}/editMessageText": resp({"ok": True, "result": True}),
            f"{API}/answerCallbackQuery": resp({"ok": True, "result": True}),
        }
    )
    tg.edit_message(42, 7, "<i>x</i>", None)
    tg.answer_callback("cbq1", "Listo")
    assert http.calls[0][2] == {
        "chat_id": 42,
        "message_id": 7,
        "text": "<i>x</i>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    assert http.calls[1][2] == {"callback_query_id": "cbq1", "text": "Listo"}


@pytest.mark.parametrize(
    ("status", "payload", "exc"),
    [
        (
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}},
            TelegramRetryAfter,
        ),
        (400, {"ok": False, "error_code": 400, "description": "can't parse"}, TelegramBadRequest),
        (403, {"ok": False, "error_code": 403, "description": "blocked"}, TelegramForbidden),
        (409, {"ok": False, "error_code": 409, "description": "webhook"}, TelegramConflict),
        (502, {"ok": False, "error_code": 502}, TelegramError),
    ],
)
def test_errors_map_to_types_without_secrets(
    status: int, payload: dict[str, object], exc: type[TelegramError]
) -> None:
    tg, _ = client({f"{API}/sendMessage": resp(payload, status)})
    with pytest.raises(exc) as info:
        tg.send_message(42, "x", None, silent=False)
    assert TOKEN not in str(info.value)
    assert "parse" not in str(info.value)  # nunca la descripción de Telegram
    if isinstance(info.value, TelegramRetryAfter):
        assert info.value.retry_after == 7


def test_network_error_becomes_telegram_error() -> None:
    class Down:
        def request(self, *args: object, **kwargs: object) -> HttpResponse:
            raise Unavailable("telegram", "network")

    tg = TelegramClient(SecretStr(TOKEN), Down())
    with pytest.raises(TelegramError) as info:
        tg.get_updates(offset=0)
    assert str(info.value) == "network"


def test_non_json_body_is_telegram_error() -> None:
    bad = HttpResponse(200, "https://api.telegram.org/", {}, b"<html>", 1)
    tg, _ = client({f"{API}/getUpdates": bad})
    with pytest.raises(TelegramError, match="bad_response"):
        tg.get_updates(offset=0)


def test_repr_hides_token() -> None:
    tg, _ = client({})
    assert TOKEN not in repr(tg)
