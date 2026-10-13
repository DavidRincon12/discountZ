"""Factories de objetos de dominio con valores por defecto válidos."""

from __future__ import annotations

from datetime import UTC, datetime

from discountz.domain.model import ExtractionPath, ListingRef, Offer, SellerClass
from discountz.domain.money import Currency, Money

T = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)


def offer(
    *,
    title: str = "Producto de prueba",
    price: int | None = 100_000,
    list_price: int | None = None,
    currency: Currency = Currency.COP,
    source: str = "exito",
    external_id: str = "123456",
    in_stock: bool = True,
    seller_class: SellerClass = SellerClass.FIRST_PARTY,
    unit_key: str = "un|1",
    observed_at: datetime = T,
) -> Offer:
    return Offer(
        ref=ListingRef(source, external_id),
        parent_external_id=None,
        title=title,
        price=None if price is None else Money(price, currency),
        list_price=None if list_price is None else Money(list_price, currency),
        in_stock=in_stock,
        seller_class=seller_class,
        unit_key=unit_key,
        extraction=ExtractionPath.API_JSON,
        observed_at=observed_at,
    )
