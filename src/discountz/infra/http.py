"""Clientes HTTP y el Decorator `PacedClient` (ritmo, presupuesto, reintentos, bloqueos).

Único módulo, junto con `__main__`, que lee el reloj real; aquí se inyecta (`sleep`, `monotonic`).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode

import httpx
from curl_cffi import requests as curl_requests

from discountz.domain.errors import BudgetExhausted, HardBlock, NotFound, Unavailable
from discountz.domain.scheduling import SourceBudget

MAX_BODY_BYTES = 5 * 1024 * 1024
# Páginas de catálogo VTEX de 50 productos: Olímpica ~7 MB, Éxito ~6 MB (medido el 2026-10-17).
CATALOG_MAX_BODY_BYTES = 24 * 1024 * 1024
TIMEOUT_S = 20.0
MAX_RETRIES = 2
MAX_RETRY_AFTER_S = 60.0
BACKOFF_CAP_S = 20.0
RETRY_STATUS = frozenset({500, 502, 503, 504, 429})
# Juego completo de cabeceras de Chrome para httpx (spec §4.4), fijo durante la ejecución.
BROWSER_HEADERS: Mapping[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CO,es;q=0.9",
}

Method = Literal["GET", "POST"]
# Una secuencia de pares permite repetir claves (VTEX: `fq=skuId:A&fq=skuId:B`).
Params = Mapping[str, str] | Sequence[tuple[str, str]]
Files = Mapping[str, tuple[str, bytes, str]]  # campo → (nombre, contenido, tipo MIME)


class BlockKind(StrEnum):
    """Tipo de bloqueo detectado (los marcadores viven en `sources/blocks.py`)."""

    HTTP_403 = "http_403"
    CAPTCHA = "captcha"
    CHALLENGE = "challenge"
    PERIMETERX = "perimeterx"
    DATADOME = "datadome"
    AKAMAI = "akamai"
    ACCOUNT_VERIFICATION = "account_verification"
    VTEX_406 = "vtex_406"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int


class HttpClient(Protocol):
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
    ) -> HttpResponse: ...


def _pairs(params: Params) -> list[tuple[str, str | int | float | bool | None]]:
    return list(params.items()) if isinstance(params, Mapping) else list(params)


def _full_url(url: str, params: Params | None) -> str:
    """Espacios como `%20`: el WAF de VTEX rechaza `+` en filtros como `P:[1 TO 9]`."""
    return f"{url}?{urlencode(_pairs(params), quote_via=quote)}" if params else url


class HttpxClient:
    """httpx sin redirecciones automáticas, con tope de tamaño y de tiempo."""

    def __init__(
        self,
        source: str,
        headers: Mapping[str, str] | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_body: int = MAX_BODY_BYTES,
    ) -> None:
        self._source = source
        self._monotonic = monotonic
        self._max_body = max_body
        self._client = httpx.Client(
            timeout=httpx.Timeout(TIMEOUT_S, connect=5.0),
            follow_redirects=False,
            headers={**BROWSER_HEADERS, **(headers or {})},
            transport=transport,
        )

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
        start = self._monotonic()
        deadline = start + TIMEOUT_S
        try:
            with self._client.stream(
                method,
                _full_url(url, params),
                json=dict(json) if json is not None else None,
                data=dict(data) if data is not None else None,
                files=dict(files) if files is not None else None,
                headers=dict(headers) if headers else None,
                cookies=dict(cookies) if cookies else None,
            ) as resp:
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > self._max_body:
                        raise Unavailable(self._source, "too_large", resp.status_code)
                    if self._monotonic() > deadline:
                        raise Unavailable(self._source, "deadline", resp.status_code)
                    chunks.append(chunk)
                return HttpResponse(
                    resp.status_code,
                    str(resp.url),
                    dict(resp.headers),
                    b"".join(chunks),
                    int((self._monotonic() - start) * 1000),
                )
        except httpx.TimeoutException as exc:
            raise Unavailable(self._source, "timeout") from exc
        except httpx.HTTPError as exc:
            raise Unavailable(self._source, "network") from exc

    def close(self) -> None:
        self._client.close()


class CurlClient:
    """curl_cffi suplantando la huella TLS de Chrome; nunca se sobrescribe el User-Agent."""

    def __init__(self, source: str, impersonate: str = "chrome") -> None:
        self._source = source
        self._session: Any = curl_requests.Session(impersonate=impersonate, timeout=TIMEOUT_S)

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
        if files is not None:
            raise ValueError("CurlClient no sube archivos")
        try:
            resp: Any = self._session.request(
                method,
                _full_url(url, params),
                json=dict(json) if json is not None else None,
                data=dict(data) if data is not None else None,
                headers=dict(headers) if headers else None,
                cookies=dict(cookies) if cookies else None,
                allow_redirects=False,
                stream=True,
            )
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_content():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    resp.close()
                    raise Unavailable(self._source, "too_large", int(resp.status_code))
                chunks.append(chunk)
            resp.close()
        except curl_requests.RequestsError as exc:
            raise Unavailable(self._source, "network") from exc
        return HttpResponse(
            int(resp.status_code),
            str(resp.url),
            {str(k): str(v) for k, v in resp.headers.items()},
            b"".join(chunks),
            int(resp.elapsed.total_seconds() * 1000)
            if hasattr(resp.elapsed, "total_seconds")
            else int(float(resp.elapsed) * 1000),
        )

    def close(self) -> None:
        self._session.close()


class PacedClient:
    """Decorator: pausa mínima + jitter, presupuesto por ejecución, reintentos y bloqueos."""

    def __init__(
        self,
        inner: HttpClient,
        budget: SourceBudget,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
        rnd: random.Random,
        detect_block: Callable[[HttpResponse], BlockKind | None],
        *,
        source: str,
        pace_multiplier: int = 1,
        deadline: float | None = None,
        max_body: int = MAX_BODY_BYTES,
    ) -> None:
        self._inner = inner
        self._max_body = max_body
        self._budget = budget
        self._sleep = sleep
        self._monotonic = monotonic
        self._rnd = rnd
        self._detect_block = detect_block
        self._source = source
        self._pace = pace_multiplier
        self._deadline = deadline  # plazo blando del ciclo (monotonic): no se reintenta tras él
        self._last_end: float | None = None
        self.used = 0
        self.blocked: BlockKind | None = None

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
        if self.blocked is not None:
            raise HardBlock(self._source, f"stopped_{self.blocked.value}")
        attempts = 1 if method == "POST" else 1 + MAX_RETRIES
        last_status: int | None = None
        for attempt in range(1, attempts + 1):
            resp = self._attempt(method, url, params, json, headers, cookies, data, files)
            if resp is None:
                last_status = None
            else:
                kind = self._detect_block(resp)
                if kind is not None:
                    self.blocked = kind
                    raise HardBlock(self._source, kind.value, resp.status)
                if resp.status == 404:
                    raise NotFound(self._source, "not_found", 404)
                if len(resp.body) > self._max_body:
                    raise Unavailable(self._source, "too_large", resp.status)
                if resp.status not in RETRY_STATUS:
                    if resp.status >= 400:
                        raise Unavailable(self._source, "http_error", resp.status)
                    return resp
                last_status = resp.status
            if attempt == attempts:
                break
            wait = self._retry_wait(resp, attempt)
            if wait is None or self._past_deadline(wait):
                break
            self._sleep(wait)
        raise Unavailable(self._source, "retries_exhausted", last_status)

    def _attempt(
        self,
        method: Method,
        url: str,
        params: Params | None,
        json: Mapping[str, object] | None,
        headers: Mapping[str, str] | None,
        cookies: Mapping[str, str] | None,
        data: Mapping[str, str] | None = None,
        files: Files | None = None,
    ) -> HttpResponse | None:
        if self.used >= self._budget.max_per_run:
            raise BudgetExhausted(self._source, "budget")
        self._pause()
        self.used += 1
        try:
            return self._inner.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                cookies=cookies,
                data=data,
                files=files,
            )
        except Unavailable as exc:
            if exc.code == "too_large":  # la misma página no cabrá al reintentarla
                raise
            return None
        finally:
            self._last_end = self._monotonic()

    def _pause(self) -> None:
        if self._last_end is None:
            return
        gap = self._budget.min_pause_s * self._pace + self._rnd.uniform(0, self._budget.jitter_s)
        remaining = self._last_end + gap - self._monotonic()
        if remaining > 0:
            self._sleep(remaining)

    def _past_deadline(self, wait: float) -> bool:
        if self._deadline is None:
            return False
        return self._monotonic() + wait + TIMEOUT_S > self._deadline

    def _retry_wait(self, resp: HttpResponse | None, attempt: int) -> float | None:
        if resp is not None and resp.status == 429:
            raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if raw is None or not raw.strip().isdigit():
                return None
            value = float(raw.strip())
            return value if value <= MAX_RETRY_AFTER_S else None
        cap = min(BACKOFF_CAP_S, 2.0 * 2 ** (attempt - 1))
        wait: float = self._rnd.uniform(0, cap)
        return wait


@dataclass
class FakeHttp:
    """Transporte falso para pruebas: URL completa (con params) → respuesta o secuencia."""

    routes: Mapping[str, HttpResponse | Sequence[HttpResponse]]
    calls: list[tuple[str, str, Mapping[str, object] | None]] = field(default_factory=list)
    uploads: list[tuple[Mapping[str, str] | None, Files | None]] = field(default_factory=list)
    _cursor: dict[str, int] = field(default_factory=dict)

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
        full = _full_url(url, params)
        self.calls.append((method, full, json))
        if data is not None or files is not None:
            self.uploads.append((data, files))
        route = self.routes.get(full)
        if route is None:
            raise AssertionError(f"URL sin fixture: {method} {full}")
        if isinstance(route, HttpResponse):
            return route
        i = self._cursor.get(full, 0)
        self._cursor[full] = i + 1
        return route[min(i, len(route) - 1)]
