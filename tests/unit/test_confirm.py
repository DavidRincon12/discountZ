"""Comparación de la lectura de confirmación (spec §3.6: ±1 %, stock, mismo vendedor y variante)."""

from __future__ import annotations

import dataclasses

from discountz.app.confirm import matches
from discountz.domain.model import SellerClass
from tests.factories import offer

TOL = 100


def test_same_price_seller_and_variant_matches() -> None:
    assert matches(offer(price=100_000), offer(price=100_500), TOL)


def test_price_outside_tolerance_does_not_match() -> None:
    assert not matches(offer(price=100_000), offer(price=101_500), TOL)


def test_other_seller_does_not_match() -> None:
    reading = offer(price=100_000, seller_class=SellerClass.MARKETPLACE)
    assert not matches(offer(price=100_000), reading, TOL)


def test_out_of_stock_or_missing_reading_does_not_match() -> None:
    assert not matches(offer(price=100_000), offer(price=100_000, in_stock=False), TOL)
    assert not matches(offer(price=100_000), None, TOL)


def test_other_variant_does_not_match() -> None:
    reading = dataclasses.replace(offer(price=100_000, external_id="999"))
    assert not matches(offer(price=100_000), reading, TOL)
