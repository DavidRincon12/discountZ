"""Jerarquía de errores de discountZ (spec §7.1).

`str(exc)` NUNCA lleva URLs, títulos, consultas, precios ni secretos: se registra en logs públicos.
Por eso los errores llevan códigos cortos y ubicaciones, nunca el valor que falló.
"""

from __future__ import annotations

from typing import ClassVar, Literal


class DiscountzError(Exception):
    """Raíz de todos los errores propios."""


# Dominio (puros)
class DomainError(DiscountzError):
    """Error de una regla del dominio."""


class InvalidMoney(DomainError):
    """Un monto no cumple las reglas de `Money`."""


class CurrencyMismatch(DomainError):
    """Se intentó operar dinero de monedas distintas."""

    def __init__(self) -> None:
        super().__init__("currency_mismatch")


class StaleObservation(DomainError):
    """Una lectura es anterior a la última registrada."""


class ParseError(DomainError):
    """Un texto o una respuesta no se pudo interpretar.

    `code` identifica la causa (p. ej. `bad_price`) y `loc` el campo, nunca el valor.
    """

    def __init__(self, code: str, loc: str | None = None) -> None:
        super().__init__(code if loc is None else f"{code} at {loc}")
        self.code = code
        self.loc = loc


class UnsafeUrl(DomainError):
    """Una URL no pasa la lista blanca o tiene una forma peligrosa (userinfo, IP, puerto...)."""

    def __init__(self, code: str = "unsafe_url") -> None:
        super().__init__(code)
        self.code = code


# Fuentes
SourceErrorKind = Literal["blocked", "unavailable", "parse", "gone", "budget"]


class SourceError(DiscountzError):
    """Falla al leer una fuente. Solo lleva el código de fuente, un código corto y el status."""

    kind: ClassVar[SourceErrorKind] = "unavailable"

    def __init__(self, source: str, code: str, http_status: int | None = None) -> None:
        detail = f"{source}:{code}" if http_status is None else f"{source}:{code}:{http_status}"
        super().__init__(detail)
        self.source = source
        self.code = code
        self.http_status = http_status


class HardBlock(SourceError):
    """Bloqueo duro (captcha, challenge, 403): sin reintento; abre el breaker."""

    kind = "blocked"


class Unavailable(SourceError):
    """Timeout, error de red o 5xx tras los reintentos; 5 seguidos abren el breaker."""

    kind = "unavailable"


class NotFound(SourceError):
    """404: el listing parece retirado; no afecta el breaker."""

    kind = "gone"


class BudgetExhausted(SourceError):
    """Se agotó el presupuesto de peticiones de la fuente en este ciclo."""

    kind = "budget"


class CredentialUnavailable(SourceError):
    """La fuente necesita credenciales que no hay (v1: ninguna fuente las usa)."""

    kind = "unavailable"


# Telegram
class TelegramError(DiscountzError):
    """Error al hablar con la API de Telegram. `str(exc)` es un código, nunca la descripción."""


class TelegramRetryAfter(TelegramError):
    """429: Telegram pide esperar `retry_after` segundos."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("retry_after")
        self.retry_after = retry_after


class TelegramBadRequest(TelegramError):
    """400: p. ej. «can't parse entities» o «query is too old»."""


class TelegramForbidden(TelegramError):
    """403: David bloqueó el bot."""


class TelegramConflict(TelegramError):
    """409: hay un webhook activo u otro `getUpdates` en curso."""


# Infraestructura
class InfrastructureError(DiscountzError):
    """Error de configuración, base de datos o entorno."""


class ConfigError(InfrastructureError):
    """Configuración inválida (exit 2)."""


class DatabaseUnavailable(InfrastructureError):
    """La base de datos no responde (exit 3)."""


class SchemaMismatch(InfrastructureError):
    """La versión del esquema no coincide con `SCHEMA_VERSION`."""


class LeaseBusy(InfrastructureError):
    """Otro proceso tiene el lease."""
