"""GTIN válido para buscar en Mercado Libre (spec de Mercado Libre §1.4)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from discountz.domain.gtin import gtin_ok


def _check_digit(body: str) -> str:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


@pytest.mark.parametrize(
    "code",
    [
        "7702004003508",  # EAN-13 de Colombia
        "0740617344806",  # EAN-13 con 0 adelante
        "850049670821",  # UPC-A
        "96385074",  # EAN-8
        "00740617344806",  # GTIN-14 que solo rellena un EAN-13
        "9780306406157",  # ISBN-13
    ],
)
def test_gtin_ok_accepts_valid_codes(code: str) -> None:
    assert gtin_ok(code)


def test_gtin_ok_rejects_bad_check_digit() -> None:
    assert not gtin_ok("6921732899395")


@pytest.mark.parametrize(
    "code",
    [
        "2001234567893",  # 20: uso interno de la tienda
        "2900000000018",  # 29
        "0200000000011",  # 02: peso variable
        "0400000000015",  # 04: uso interno
        "200000000004",  # UPC-A que empieza por 2
        "400000000008",  # UPC-A que empieza por 4
        "00000017",  # EAN-8 que empieza por 0
        "20000011",  # EAN-8 que empieza por 2
    ],
)
def test_gtin_ok_rejects_restricted_circulation(code: str) -> None:
    assert not gtin_ok(code)


@pytest.mark.parametrize(
    "code", ["", "123456", "12345678901", "77020040035O8", "17702004003505", " 7702004003508"]
)
def test_gtin_ok_rejects_other_shapes(code: str) -> None:
    assert not gtin_ok(code)


@given(
    st.text(alphabet="13456789", min_size=1, max_size=1),
    st.text(alphabet="0123456789", min_size=11, max_size=11),
)
def test_gtin_ok_holds_for_any_computed_check_digit(first: str, rest: str) -> None:
    body = first + rest
    assert gtin_ok(body + _check_digit(body))
