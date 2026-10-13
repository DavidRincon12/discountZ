from decimal import Decimal

import pytest

from discountz.domain.errors import CurrencyMismatch, ParseError
from discountz.domain.money import Currency, Money, format_money, parse_price, to_minor


@pytest.mark.parametrize(
    ("text", "cur", "minor"),
    [
        ("1.299.900", Currency.COP, 1_299_900),
        ("$ 1.299.900", Currency.COP, 1_299_900),
        ("$1.299.900,00", Currency.COP, 1_299_900),
        ("US$1,299.99", Currency.USD, 129_999),
        ("$74.99", Currency.USD, 7_499),
        ("129", Currency.USD, 12_900),
    ],
)
def test_parse_price_regional_formats(text: str, cur: Currency, minor: int) -> None:
    assert parse_price(text, cur) == minor


@pytest.mark.parametrize("text", ["", "gratis", "1.2.3,4,5", "-5"])
def test_parse_price_rejects_garbage(text: str) -> None:
    with pytest.raises(ParseError):
        parse_price(text, Currency.COP)


def test_money_never_mixes_currencies() -> None:
    with pytest.raises(CurrencyMismatch):
        Money(1, Currency.COP) + Money(1, Currency.USD)


def test_money_adds_and_subtracts_same_currency() -> None:
    assert Money(5, Currency.COP) + Money(3, Currency.COP) == Money(8, Currency.COP)
    assert Money(5, Currency.USD) - Money(3, Currency.USD) == Money(2, Currency.USD)


def test_to_minor_rounds_half_up() -> None:
    assert to_minor(Decimal("10.005"), Currency.USD) == 1001
    assert to_minor(Decimal("1299900.5"), Currency.COP) == 1_299_901


def test_format_money() -> None:
    assert format_money(Money(1_299_900, Currency.COP)) == "$1.299.900"
    assert format_money(Money(9_999, Currency.USD)) == "US$99,99"
    assert format_money(Money(129_999, Currency.USD)) == "US$1.299,99"


def test_parse_error_carries_code_and_loc() -> None:
    err = ParseError("bad_price", loc="price")
    assert err.code == "bad_price"
    assert err.loc == "price"
    assert "bad_price" in str(err)
