"""Adaptador VTEX legacy para Éxito, Carulla, Olímpica y Panamericana (spec §4.2 «VTEX»).

Mini-spikes del 2026-10-13 (desde la sesión de desarrollo):
- M2: `fq=skuId:A&fq=skuId:B&fq=skuId:C` devuelve los 3 ítems en < 0,3 s en las 4 tiendas
  ⇒ `max_batch = 50`.
- M3: la simulación anónima (`sc=1`, sin código postal) responde 200 con `sellingPrice`
  coherente (centavos) en las 4 tiendas ⇒ todas tienen ruta independiente.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, ValidationError

from discountz.domain.errors import NotFound, ParseError, SourceError
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    ListingFailed,
    ListingFound,
    ListingGone,
    ListingOutcome,
    NeedsResolution,
    SearchPage,
    SourceContext,
    SourceDescriptor,
    pack_unit_key,
)
from discountz.sources.blocks import detect_block_common
from discountz.sources.registry import register

MAX_BATCH = 50
SEARCH_PER_PRODUCT = 3
_SKU_RE = re.compile(r"^\d{1,12}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "es-CO,es;q=0.9",
}

Number = Decimal | int


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _CommertialOffer(_Model):
    Price: Number
    ListPrice: Number
    PriceWithoutDiscount: Number
    AvailableQuantity: int


class _Seller(_Model):
    sellerId: str
    sellerDefault: bool
    commertialOffer: _CommertialOffer


class _Item(_Model):
    itemId: str
    name: str = ""
    nameComplete: str = ""
    measurementUnit: str = "un"
    unitMultiplier: Number = 1
    sellers: list[_Seller]


class _Product(_Model):
    productId: str
    productName: str
    linkText: str
    items: list[_Item]


class _SimItem(_Model):
    id: str
    sellingPrice: Number | None = None
    availability: str = ""
    seller: str = ""


class _Simulation(_Model):
    items: list[_SimItem]


@dataclass(frozen=True, slots=True)
class VtexStore:
    code: str
    host: str
    api_base: str


STORES: dict[str, VtexStore] = {
    "exito": VtexStore("exito", "www.exito.com", "https://www.exito.com/io/api"),
    "carulla": VtexStore("carulla", "www.carulla.com", "https://www.carulla.com/io/api"),
    "olimpica": VtexStore("olimpica", "www.olimpica.com", "https://www.olimpica.com/api"),
    "panamericana": VtexStore(
        "panamericana", "www.panamericana.com.co", "https://www.panamericana.com.co/api"
    ),
}


def _load(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise ParseError("bad_json") from exc


def _parse_error(exc: ValidationError) -> ParseError:
    first = exc.errors(include_input=False, include_url=False)[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))[:60]
    return ParseError("schema", loc=loc or None)


def _money(value: Number) -> Money | None:
    minor = to_minor(Decimal(value), Currency.COP)
    return Money(minor, Currency.COP) if minor > 0 else None


def _unit_key(item: _Item, title: str) -> str:
    unit = re.sub(r"[^a-z]", "", item.measurementUnit.lower())[:8] or "un"
    mult = format(Decimal(item.unitMultiplier).normalize(), "f")
    key = f"{unit}|{mult}"
    pack = pack_unit_key(title)
    return pack if key == "un|1" and pack else key


def _seller(item: _Item) -> _Seller | None:
    default = [s for s in item.sellers if s.sellerDefault] or item.sellers
    return default[0] if default else None


def _offer(product: _Product, item: _Item, store: VtexStore, observed_at: datetime) -> Offer | None:
    seller = _seller(item)
    if seller is None or not _SKU_RE.fullmatch(item.itemId):
        return None
    co = seller.commertialOffer
    price = _money(co.Price)
    list_price = _money(max(Decimal(co.ListPrice), Decimal(co.PriceWithoutDiscount)))
    title = " ".join((item.nameComplete or product.productName).split())
    first_party = seller.sellerId == "1" and seller.sellerDefault
    return Offer(
        ref=ListingRef(store.code, item.itemId),
        parent_external_id=product.productId if _SKU_RE.fullmatch(product.productId) else None,
        title=title,
        price=price,
        list_price=list_price,
        in_stock=price is not None and co.AvailableQuantity > 0,
        seller_class=SellerClass.FIRST_PARTY if first_party else SellerClass.MARKETPLACE,
        unit_key=_unit_key(item, title),
        extraction=ExtractionPath.API_JSON,
        observed_at=observed_at,
        url_path=f"/{product.linkText}/p",
    )


def parse_vtex_products(
    body: bytes, store: VtexStore, observed_at: datetime, *, max_per_product: int | None = None
) -> list[Offer]:
    """Un `Offer` por ítem (SKU). En búsquedas, `max_per_product` limita los ítems en stock."""
    data = _load(body)
    if not isinstance(data, list):
        raise ParseError("not_a_list")
    offers: list[Offer] = []
    for raw in data:
        try:
            product = _Product.model_validate(raw)
        except ValidationError as exc:
            raise _parse_error(exc) from None
        kept = 0
        for item in product.items:
            offer = _offer(product, item, store, observed_at)
            if offer is None:
                continue
            if max_per_product is not None:
                if not offer.in_stock or kept >= max_per_product:
                    continue
                kept += 1
            offers.append(offer)
    return offers


def parse_vtex_simulation(
    body: bytes, store: VtexStore, sku: str, observed_at: datetime
) -> Offer | None:
    """Ruta independiente: `sellingPrice` en centavos ÷ 100 con `Decimal`."""
    try:
        sim = _Simulation.model_validate(_load(body))
    except ValidationError as exc:
        raise _parse_error(exc) from None
    item = next((i for i in sim.items if i.id == sku), None)
    if item is None or item.sellingPrice is None:
        return None
    price = _money(Decimal(item.sellingPrice) / 100)
    return Offer(
        ref=ListingRef(store.code, sku),
        parent_external_id=None,
        title="",
        price=price,
        list_price=None,
        in_stock=price is not None and item.availability == "available",
        seller_class=SellerClass.FIRST_PARTY if item.seller == "1" else SellerClass.MARKETPLACE,
        unit_key="un|1",
        extraction=ExtractionPath.API_JSON,
        observed_at=observed_at,
    )


class VtexSource:
    def __init__(self, store: VtexStore) -> None:
        self.store = store
        self.descriptor = SourceDescriptor(
            code=store.code,
            kind="store",
            currency=Currency.COP,
            hosts=frozenset({store.host}),
            client="httpx",
            capabilities=frozenset(
                {Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM, Capability.RESOLVE}
            ),
            max_batch=MAX_BATCH,
            external_id_re=_SKU_RE,
        )
        # sku → vendedor por defecto visto en este proceso (la simulación lo necesita).
        self._sellers: dict[str, str] = {}

    def _remember(self, body: bytes) -> None:
        for sku, seller in _default_sellers(body).items():
            self._sellers[sku] = seller

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        parts = [p for p in url.path.split("/") if p]
        if len(parts) != 2 or parts[1] != "p" or not _SLUG_RE.fullmatch(parts[0]):
            return None
        if len(parts[0]) > 150:
            return None
        ref = f"slug:{parts[0]}"
        sku = dict(url.query).get("skuId")
        if sku and _SKU_RE.fullmatch(sku):
            ref += f"|sku:{sku}"
        return NeedsResolution(self.store.code, ref)

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        body = pending.pending_ref.removeprefix("slug:")
        slug, _, sku_part = body.partition("|")
        if not _SLUG_RE.fullmatch(slug):
            raise ParseError("bad_slug")
        resp = ctx.http.request(
            "GET",
            f"{self.store.api_base}/catalog_system/pub/products/search/{slug}/p",
            headers=_HEADERS,
        )
        offers = parse_vtex_products(resp.body, self.store, ctx.now)
        wanted = sku_part.removeprefix("sku:") if sku_part else None
        for offer in offers:
            if wanted is None or offer.ref.external_id == wanted:
                return offer.ref
        raise NotFound(self.store.code, "slug_not_found", resp.status)

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for i in range(0, len(refs), MAX_BATCH):
            batch = refs[i : i + MAX_BATCH]
            params = [("fq", f"skuId:{r.external_id}") for r in batch]
            try:
                resp = ctx.http.request(
                    "GET",
                    f"{self.store.api_base}/catalog_system/pub/products/search",
                    params=params,
                    headers=_HEADERS,
                )
                found = {
                    o.ref.external_id: o
                    for o in parse_vtex_products(resp.body, self.store, ctx.now)
                }
                self._remember(resp.body)
            except (SourceError, ParseError) as exc:
                out.extend(ListingFailed(r, exc) for r in batch)
                continue
            for r in batch:
                offer = found.get(r.external_id)
                out.append(ListingFound(offer) if offer else ListingGone(r))
        return out

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        del page  # una sola página: _from=0&_to=49
        resp = ctx.http.request(
            "GET",
            f"{self.store.api_base}/catalog_system/pub/products/search",
            params={"ft": spec.query, "_from": "0", "_to": "49"},
            headers=_HEADERS,
        )
        offers = parse_vtex_products(
            resp.body, self.store, ctx.now, max_per_product=SEARCH_PER_PRODUCT
        )
        self._remember(resp.body)
        return SearchPage(offers, None)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        sku = offer.ref.external_id
        seller = self._sellers.get(sku)
        if seller is None and offer.seller_class is SellerClass.FIRST_PARTY:
            seller = "1"
        if seller is None:
            detail = ctx.http.request(
                "GET",
                f"{self.store.api_base}/catalog_system/pub/products/search",
                params={"fq": f"skuId:{sku}"},
                headers=_HEADERS,
            )
            self._remember(detail.body)
            seller = self._sellers.get(sku, "1")
        resp = ctx.http.request(
            "POST",
            f"{self.store.api_base}/checkout/pub/orderForms/simulation",
            params={"sc": "1"},
            json={"items": [{"id": sku, "quantity": 1, "seller": seller}], "country": "COL"},
            headers=_HEADERS,
        )
        return parse_vtex_simulation(resp.body, self.store, sku, ctx.now)

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


def _default_sellers(body: bytes) -> dict[str, str]:
    data = _load(body)
    out: dict[str, str] = {}
    if not isinstance(data, list):
        return out
    for raw in data:
        try:
            product = _Product.model_validate(raw)
        except ValidationError:
            continue
        for item in product.items:
            seller = _seller(item)
            if seller is not None:
                out[item.itemId] = seller.sellerId
    return out


for _store in STORES.values():
    register(VtexSource(_store))
