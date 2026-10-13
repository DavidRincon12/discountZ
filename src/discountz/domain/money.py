"""Dinero como Value Object en enteros de unidad menor (COP exponente 0, USD exponente 2)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from types import MappingProxyType

from discountz.domain.errors import CurrencyMismatch, ParseError


class Currency(StrEnum):
    COP = "COP"
    USD = "USD"


EXPONENT: Mapping[Currency, int] = MappingProxyType({Currency.COP: 0, Currency.USD: 2})


@dataclass(frozen=True, slots=True, order=True)
class Money:
    minor: int
    currency: Currency

    def __add__(self, other: Money) -> Money:
        return Money(self.minor + self._same(other).minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        return Money(self.minor - self._same(other).minor, self.currency)

    def _same(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise CurrencyMismatch
        return other


def to_minor(value: Decimal, currency: Currency) -> int:
    """Convierte un monto decimal a unidad menor con redondeo `ROUND_HALF_UP`."""
    scaled = value.scaleb(EXPONENT[currency]).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(scaled)


_PREFIX_RE = re.compile(r"^(?:US)?\$?\s*")
_NUMBER_RE = re.compile(r"^\d[\d.,]*$")


def parse_price(text: str, currency: Currency) -> int:
    """Convierte un precio con formato regional a unidad menor.

    El último separador seguido de 1-2 dígitos es decimal; los demás separadores son de miles
    y deben formar grupos de 3 dígitos con un solo carácter. Así `1.299.900,00` (COP),
    `US$1,299.99` y `99,99` (USD) se interpretan sin ambigüedad, y `1.2.3` se rechaza.
    """
    raw = _PREFIX_RE.sub("", text.strip(), count=1)
    if not _NUMBER_RE.fullmatch(raw):
        raise ParseError("bad_price")
    integer, decimals = _split_decimals(raw)
    digits = _ungroup(integer)
    value = Decimal(f"{digits}.{decimals}") if decimals else Decimal(digits)
    minor = to_minor(value, currency)
    if minor <= 0:
        raise ParseError("bad_price")
    return minor


def _split_decimals(raw: str) -> tuple[str, str]:
    cut = max(raw.rfind("."), raw.rfind(","))
    if cut == -1:
        return raw, ""
    tail = raw[cut + 1 :]
    if tail.isdigit() and 1 <= len(tail) <= 2:
        head = raw[:cut]
        if raw[cut] in head:
            raise ParseError("bad_price")
        return head, tail
    return raw, ""


def _ungroup(integer: str) -> str:
    separators = {c for c in integer if c in ".,"}
    if not separators:
        return integer
    if len(separators) > 1:
        raise ParseError("bad_price")
    groups = integer.split(separators.pop())
    if not (1 <= len(groups[0]) <= 3) or any(len(g) != 3 for g in groups[1:]):
        raise ParseError("bad_price")
    return "".join(groups)


def format_money(m: Money) -> str:
    """Formato es-CO: `$1.299.900` para COP y `US$99,99` para USD."""
    sign = "-" if m.minor < 0 else ""
    exp = EXPONENT[m.currency]
    whole, frac = divmod(abs(m.minor), 10**exp)
    grouped = f"{whole:,}".replace(",", ".")
    prefix = "US$" if m.currency is Currency.USD else "$"
    decimals = f",{frac:0{exp}d}" if exp else ""
    return f"{sign}{prefix}{grouped}{decimals}"
