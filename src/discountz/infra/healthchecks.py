"""Pings opcionales a healthchecks.io (spec §1.3): no-op si no hay URL configurada.

Un ping que falla nunca afecta el ciclo: solo es un aviso de vida.
"""

from __future__ import annotations

import contextlib

from pydantic import SecretStr

from discountz.domain.errors import SourceError
from discountz.infra.http import HttpClient


class Healthchecks:
    def __init__(self, url: SecretStr | None, http: HttpClient) -> None:
        self._url = url
        self._http = http

    def __repr__(self) -> str:
        return "Healthchecks(url=***)"

    def start(self) -> None:
        self._ping("/start")

    def success(self) -> None:
        self._ping("")

    def fail(self) -> None:
        self._ping("/fail")

    def _ping(self, suffix: str) -> None:
        if self._url is None:
            return
        with contextlib.suppress(SourceError):
            self._http.request("POST", self._url.get_secret_value().rstrip("/") + suffix)
