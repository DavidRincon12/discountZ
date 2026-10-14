"""Cliente mínimo de la Bot API de Telegram (spec §5.1, §5.4 y §5.5).

El token va en la URL de la API: nunca se registra ni aparece en `repr` o en los errores, que
solo llevan códigos cortos (`str(exc)` nunca incluye la descripción que devuelve Telegram).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from discountz.domain.errors import (
    SourceError,
    TelegramBadRequest,
    TelegramConflict,
    TelegramError,
    TelegramForbidden,
    TelegramRetryAfter,
)
from discountz.infra.http import HttpClient

API_BASE = "https://api.telegram.org"
ALLOWED_UPDATES = ("message", "callback_query")
UPDATES_LIMIT = 100
_NO_PREVIEW = {"is_disabled": True}


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: int
    from_id: int
    chat_id: int
    text: str | None


@dataclass(frozen=True, slots=True)
class CallbackQuery:
    id: str
    from_id: int
    chat_id: int
    message_id: int
    data: str | None


@dataclass(frozen=True, slots=True)
class Update:
    """Un update ya validado. Sin mensaje ni callback = forma desconocida (se descarta)."""

    update_id: int
    message: IncomingMessage | None
    callback: CallbackQuery | None


class TelegramApi(Protocol):
    """Lo que necesita la bandeja de entrada (facilita los dobles de prueba)."""

    def get_updates(self, offset: int, timeout_s: int = 0) -> list[Update]: ...
    def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...


# ───────────── Validación en el borde (pydantic ignora los campos que no usamos)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _User(_Model):
    id: int


class _Chat(_Model):
    id: int


class _Message(_Model):
    message_id: int
    from_: _User = Field(alias="from")
    chat: _Chat
    text: str | None = None


class _CallbackMessage(_Model):
    message_id: int
    chat: _Chat


class _Callback(_Model):
    id: str
    from_: _User = Field(alias="from")
    message: _CallbackMessage
    data: str | None = None


class _Update(_Model):
    update_id: int
    message: _Message | None = None
    callback_query: _Callback | None = None


def _parse_update(raw: object) -> Update | None:
    """Valida un update; si su forma no se entiende, conserva solo el `update_id`."""
    try:
        upd = _Update.model_validate(raw)
    except ValidationError:
        update_id = raw.get("update_id") if isinstance(raw, dict) else None
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            return Update(update_id, None, None)
        return None
    message = None
    if upd.message is not None:
        m = upd.message
        message = IncomingMessage(m.message_id, m.from_.id, m.chat.id, m.text)
    callback = None
    if upd.callback_query is not None:
        c = upd.callback_query
        callback = CallbackQuery(c.id, c.from_.id, c.message.chat.id, c.message.message_id, c.data)
    return Update(upd.update_id, message, callback)


class TelegramClient:
    def __init__(self, token: SecretStr, http: HttpClient) -> None:
        self._token = token
        self._http = http

    def __repr__(self) -> str:
        return "TelegramClient(token=***)"

    def get_updates(self, offset: int, timeout_s: int = 0) -> list[Update]:
        result = self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_s,
                "limit": UPDATES_LIMIT,
                "allowed_updates": list(ALLOWED_UPDATES),
            },
        )
        if not isinstance(result, list):
            raise TelegramError("bad_response")
        parsed = (_parse_update(raw) for raw in result)
        return sorted((u for u in parsed if u is not None), key=lambda u: u.update_id)

    def send_message(
        self,
        chat_id: int,
        html: str,
        reply_markup: Mapping[str, Any] | None,
        *,
        silent: bool,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> int:
        """Envía y devuelve el `message_id`. `parse_mode=None` manda texto plano."""
        payload: dict[str, object] = {"chat_id": chat_id, "text": html}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        payload["link_preview_options"] = _NO_PREVIEW
        payload["disable_notification"] = silent
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        result = self._call("sendMessage", payload)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            raise TelegramError("bad_response")
        return message_id

    def edit_message(
        self, chat_id: int, message_id: int, html: str, reply_markup: Mapping[str, Any] | None
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": html,
            "parse_mode": "HTML",
            "link_preview_options": _NO_PREVIEW,
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        self._call("editMessageText", payload)

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, object] = {"callback_query_id": callback_id}
        if text is not None:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    def _call(self, method: str, payload: Mapping[str, object]) -> object:
        url = f"{API_BASE}/bot{self._token.get_secret_value()}/{method}"
        try:
            resp = self._http.request("POST", url, json=payload)
        except SourceError as exc:
            raise TelegramError(exc.code) from None  # sin la URL: lleva el token
        try:
            body = json.loads(resp.body)
        except ValueError:
            raise TelegramError("bad_response") from None
        if not isinstance(body, dict):
            raise TelegramError("bad_response")
        if body.get("ok") is True and resp.status < 400:
            return body.get("result")
        raise _error(resp.status, body)


def _error(status: int, body: Mapping[str, object]) -> TelegramError:
    code = body.get("error_code")
    code = code if isinstance(code, int) else status
    if code == 429:
        params = body.get("parameters")
        retry = params.get("retry_after") if isinstance(params, dict) else None
        return TelegramRetryAfter(retry if isinstance(retry, int) else 1)
    if code == 400:
        return TelegramBadRequest("bad_request")
    if code == 403:
        return TelegramForbidden("forbidden")
    if code == 409:
        return TelegramConflict("conflict")
    return TelegramError(f"http_{code}")
