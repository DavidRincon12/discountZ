"""GTIN válido para buscar un producto por su código (spec de Mercado Libre §1.4). Puro.

Las tiendas también publican códigos internos y de peso variable con forma de EAN: esos son de
circulación restringida (GS1) y ningún catálogo externo los conoce.
"""

from __future__ import annotations

_LENGTHS = frozenset({8, 12, 13, 14})
_RESTRICTED_8 = ("0", "2")
_RESTRICTED_13 = ("02", "04", "2")


def gtin_ok(code: str) -> bool:
    """8, 12, 13 o 14 dígitos (14 solo con 0 adelante), dígito de control GS1 y no restringido."""
    if not code.isascii() or not code.isdigit() or len(code) not in _LENGTHS:
        return False
    if len(code) == 14:
        if code[0] != "0":
            return False
        code = code[1:]
    if not _check_digit_ok(code):
        return False
    if len(code) == 8:
        return not code.startswith(_RESTRICTED_8)
    return not code.zfill(13).startswith(_RESTRICTED_13)


def _check_digit_ok(code: str) -> bool:
    """Pesos 3 y 1 alternados desde la derecha, sin contar el dígito de control."""
    body, check = code[:-1], int(code[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check
