"""Logs públicos con lista blanca de campos y redacción de defensa (spec §7.3).

Formato `evento clave=valor` solo con: código de fuente, ids opacos (`L1234`, `p12`, `U9`),
status HTTP, conteos, duraciones y códigos de error. Nunca URLs, títulos, consultas ni precios.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from types import TracebackType

ALLOWED_FIELDS = frozenset(
    {
        "source",
        "ref",
        "status",
        "http_status",
        "count",
        "requests",
        "alerts",
        "sent",
        "updates",
        "duration_ms",
        "code",
        "error",
        "kind",
        "run_id",
        "exit",
        "health",
        "posts",
        "duplicates",
        "skipped",
        "held",
        "db_mb",
    }
)
_REDACT = (
    re.compile(r"https?://\S+"),
    re.compile(r"bot\d+:[\w-]+"),
    re.compile(r"postgres(?:ql)?://\S+"),
)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:\-]{1,60}$")


def redact(text: str) -> str:
    for pattern in _REDACT:
        text = pattern.sub("[redactado]", text)
    return text


class RedactFilter(logging.Filter):
    """Defensa adicional: redacta URLs, tokens de bot y URLs de Postgres en todo registro."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        record.exc_info = None  # las trazas pueden llevar valores: solo el tipo, vía `error=`
        record.exc_text = None
        return True


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: object) -> None:
    """Registra `name k=v…` descartando los campos fuera de la lista blanca."""
    parts = [name]
    for key, value in fields.items():
        if key not in ALLOWED_FIELDS or value is None:
            continue
        text = str(value)
        parts.append(f"{key}={text if _SAFE_VALUE.fullmatch(text) else 'invalid'}")
    logger.log(level, " ".join(parts))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "psycopg"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    code = getattr(exc, "code", None)
    suffix = f" code={code}" if isinstance(code, str) and _SAFE_VALUE.fullmatch(code) else ""
    sys.stderr.write(f"discountz: error no controlado {exc_type.__name__}{suffix}\n")


def install_excepthook() -> Callable[..., object]:
    """Reemplaza `sys.excepthook` por uno que imprime solo el tipo; devuelve el anterior."""
    previous = sys.excepthook
    sys.excepthook = _excepthook
    return previous
