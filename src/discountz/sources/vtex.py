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
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from discountz.domain.catalog import CatalogObservation, split_price_band
from discountz.domain.errors import NotFound, ParseError, SourceError, Unavailable
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    CardFacts,
    CatalogPage,
    CatalogPartition,
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
DEFAULT_PAGE = 10  # productos que devuelve products/search sin `_from`/`_to`
CATALOG_PAGE = 50
CATALOG_CAP = 2_500  # VTEX no pagina más allá de _from=2500
PRICE_MAX = 999_999_999  # techo de la primera banda de precio (COP)
_ROOT_RE = re.compile(r"^C:(?:/\d{1,12})+/$")
_BAND_RE = re.compile(r"^P:\[(\d{1,12}) TO (\d{1,12})\]$")
_EAN_RE = re.compile(r"^\d{6,14}$")
_AVAILABLE = ("fq", "isAvailablePerSalesChannel_1:1")
SEARCH_PER_PRODUCT = 3
_SKU_RE = re.compile(r"^\d{1,12}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_IMAGE_ID_RE = re.compile(r"^/arquivos/ids/(\d{1,12})(?:-[0-9a-z]{1,6}-[0-9a-z]{1,6})?/")
# La tarjeta dibuja la foto en 920 × 560: el original de un vendedor puede pasar de 5 MB.
CARD_IMAGE_SIZE = "1000-1000"
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


class _Image(_Model):
    imageUrl: str


class _CatalogItem(_Item):
    ean: str | None = None
    images: list[_Image] = []


class _CatalogProduct(_Model):
    productId: str
    productName: str
    linkText: str
    categories: list[str] = []
    items: list[_CatalogItem]


class _CategoryNode(_Model):
    id: int
    name: str


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
    account: str  # cuenta VTEX: subdominio de las imágenes


STORES: dict[str, VtexStore] = {
    "exito": VtexStore("exito", "www.exito.com", "https://www.exito.com/io/api", "exitocol"),
    "carulla": VtexStore("carulla", "www.carulla.com", "https://www.carulla.com/io/api", "carulla"),
    "olimpica": VtexStore(
        "olimpica", "www.olimpica.com", "https://www.olimpica.com/api", "olimpica"
    ),
    "panamericana": VtexStore(
        "panamericana",
        "www.panamericana.com.co",
        "https://www.panamericana.com.co/api",
        "panamericana",
    ),
    # Jumbo vive en jumbocolombia.com (tiendasjumbo.co ya no responde); un solo canal (sc=1).
    "jumbo": VtexStore(
        "jumbo", "www.jumbocolombia.com", "https://www.jumbocolombia.com/api", "jumbocolombiaio"
    ),
    "pepeganga": VtexStore(
        "pepeganga", "www.pepeganga.com", "https://www.pepeganga.com/api", "pepeganga"
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


def _seller(item: _Item, *, cheapest: bool = False) -> _Seller | None:
    """El vendedor por defecto; con `cheapest`, el más barato con stock (regla del rastreo)."""
    if cheapest:
        stocked = [
            (price.minor, s.sellerId != "1", s)
            for s in item.sellers
            if s.commertialOffer.AvailableQuantity > 0
            and (price := _money(s.commertialOffer.Price)) is not None
        ]
        if stocked:
            return min(stocked, key=lambda t: (t[0], t[1]))[2]
    default = [s for s in item.sellers if s.sellerDefault] or item.sellers
    return default[0] if default else None


def _offer(
    product: _Product,
    item: _Item,
    store: VtexStore,
    observed_at: datetime,
    *,
    cheapest: bool = False,
) -> Offer | None:
    seller = _seller(item, cheapest=cheapest)
    if seller is None or not _SKU_RE.fullmatch(item.itemId):
        return None
    co = seller.commertialOffer
    price = _money(co.Price)
    list_price = _money(max(Decimal(co.ListPrice), Decimal(co.PriceWithoutDiscount)))
    title = " ".join((item.nameComplete or product.productName).split())
    first_party = seller.sellerId == "1" and (seller.sellerDefault or cheapest)
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
    body: bytes,
    store: VtexStore,
    observed_at: datetime,
    *,
    max_per_product: int | None = None,
    cheapest: bool = False,
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
            offer = _offer(product, item, store, observed_at, cheapest=cheapest)
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
                {
                    Capability.DETAIL,
                    Capability.SEARCH,
                    Capability.CONFIRM,
                    Capability.RESOLVE,
                    Capability.CATALOG,
                }
            ),
            max_batch=MAX_BATCH,
            external_id_re=_SKU_RE,
            image_hosts=frozenset(
                {f"{store.account}.vteximg.com.br", f"{store.account}.vtexassets.com"}
            ),
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
        return self._fetch_offers(ctx, refs, cheapest=False)

    def fetch_catalog_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        """Confirmación del rastreo: la ficha con la misma regla de vendedor del recorrido."""
        return self._fetch_offers(ctx, refs, cheapest=True)

    def _fetch_offers(
        self, ctx: SourceContext, refs: Sequence[ListingRef], *, cheapest: bool
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for i in range(0, len(refs), MAX_BATCH):
            batch = refs[i : i + MAX_BATCH]
            try:
                resp = ctx.http.request(
                    "GET",
                    f"{self.store.api_base}/catalog_system/pub/products/search",
                    params=_sku_params([r.external_id for r in batch]),
                    headers=_HEADERS,
                )
                found = {
                    o.ref.external_id: o
                    for o in parse_vtex_products(resp.body, self.store, ctx.now, cheapest=cheapest)
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

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[CatalogPartition]:
        """Una partición por categoría raíz: juntas cubren todo el surtido disponible."""
        resp = ctx.http.request(
            "GET", f"{self.store.api_base}/catalog_system/pub/category/tree/1", headers=_HEADERS
        )
        data = _load(resp.body)
        if not isinstance(data, list):
            raise ParseError("not_a_list")
        try:
            nodes = [_CategoryNode.model_validate(raw) for raw in data]
        except ValidationError as exc:
            raise _parse_error(exc) from None
        return [CatalogPartition(f"C:/{n.id}/", f"/{n.name}/") for n in nodes]

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        root, band = _parse_partition(partition)
        start = page * CATALOG_PAGE
        params = [("fq", root)]
        if band is not None:
            params.append(("fq", f"P:[{band[0]} TO {band[1]}]"))
        params.append(_AVAILABLE)
        split = _split_band(band or (1, PRICE_MAX)) if page == 0 else []
        total, bodies = self._range_bodies(
            ctx, params, start, start + CATALOG_PAGE - 1, enough=CATALOG_CAP if split else None
        )
        if split and total > CATALOG_CAP:
            return CatalogPage((), has_more=False, split=[f"{root}|{b}" for b in split])
        items = [o for body in bodies for o in parse_vtex_catalog(body)]
        return CatalogPage(items, has_more=start + CATALOG_PAGE < min(total, CATALOG_CAP))

    def _range_bodies(
        self,
        ctx: SourceContext,
        params: list[tuple[str, str]],
        start: int,
        end: int,
        *,
        enough: int | None = None,
    ) -> tuple[int, list[bytes]]:
        """Productos `start..end` y el total de la cabecera `resources`.

        Si la respuesta no cabe en el tope de tamaño se pide por mitades: con variantes y
        vendedores, 50 productos de Moda y accesorios de Éxito pesaron 42,8 MB el 2026-10-17.
        Con un total mayor que `enough` la partición se va a partir: basta la primera mitad.
        """
        try:
            resp = ctx.http.request(
                "GET",
                f"{self.store.api_base}/catalog_system/pub/products/search",
                params=[*params, ("_from", str(start)), ("_to", str(end))],
                headers=_HEADERS,
            )
        except Unavailable as exc:
            if exc.code != "too_large" or start == end:
                raise
            mid = (start + end) // 2
            total, first = self._range_bodies(ctx, params, start, mid, enough=enough)
            if enough is not None and total > enough:
                return total, first
            _, second = self._range_bodies(ctx, params, mid + 1, end)
            return total, first + second
        return _resources_total(resp), [resp.body]

    def _sku_bodies(self, ctx: SourceContext, ids: Sequence[str]) -> list[bytes]:
        """Lote por SKU; si la respuesta no cabe en el tope de tamaño, por mitades."""
        try:
            resp = ctx.http.request(
                "GET",
                f"{self.store.api_base}/catalog_system/pub/products/search",
                params=_sku_params(ids),
                headers=_HEADERS,
            )
        except Unavailable as exc:
            if exc.code != "too_large" or len(ids) == 1:
                raise
            mid = len(ids) // 2
            return self._sku_bodies(ctx, ids[:mid]) + self._sku_bodies(ctx, ids[mid:])
        return [resp.body]

    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage:
        raise NotImplementedError

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> list[CatalogObservation]:
        """Relectura por lote (capa caliente) con la misma regla del recorrido."""
        ids = [i for i in external_ids if _SKU_RE.fullmatch(i)][:MAX_BATCH]
        if not ids:
            return []
        bodies = self._sku_bodies(ctx, ids)
        return [o for body in bodies for o in parse_vtex_catalog(body) if o.external_id in ids]

    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]:
        ids = [i for i in external_ids if _SKU_RE.fullmatch(i)][:MAX_BATCH]
        if not ids:
            return {}
        out: dict[str, CardFacts] = {}
        products = [p for body in self._sku_bodies(ctx, ids) for p in _catalog_products(body)]
        for product in products:
            for item in product.items:
                if item.itemId not in ids:
                    continue
                title = " ".join((item.nameComplete or product.productName).split())
                image = card_image(item.images[0].imageUrl) if item.images else None
                url = f"https://{self.store.host}/{product.linkText}/p"
                out[item.itemId] = CardFacts(title, url, image)
        return out

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        return detect_block_common(resp)


def card_image(url: str) -> str:
    """Variante de 1000 px de la foto (`ids/<id>-1000-1000/`), con el host sin punto final."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    path = _IMAGE_ID_RE.sub(lambda m: f"/arquivos/ids/{m.group(1)}-{CARD_IMAGE_SIZE}/", parts.path)
    return urlunsplit((parts.scheme, host, path, parts.query, ""))


def _parse_partition(partition: str) -> tuple[str, tuple[int, int] | None]:
    root, _, band = partition.partition("|")
    if not _ROOT_RE.fullmatch(root):
        raise ParseError("bad_partition")
    if not band:
        return root, None
    m = _BAND_RE.fullmatch(band)
    if m is None or int(m.group(1)) > int(m.group(2)):
        raise ParseError("bad_partition")
    return root, (int(m.group(1)), int(m.group(2)))


def _split_band(band: tuple[int, int]) -> list[str]:
    """Parte la banda en el punto medio geométrico; una banda de un solo precio no se parte."""
    return [f"P:[{lo} TO {hi}]" for lo, hi in split_price_band(*band)]


def _sku_params(ids: Sequence[str]) -> list[tuple[str, str]]:
    """Un `fq=skuId:` por SKU. Sin rango, VTEX devuelve solo 10 productos (medido en Éxito el
    2026-10-17): los lotes mayores piden `_from`/`_to`; los de hasta 10 quedan como en la v1."""
    params = [("fq", f"skuId:{i}") for i in ids]
    if len(ids) > DEFAULT_PAGE:
        params += [("_from", "0"), ("_to", str(len(ids) - 1))]
    return params


def _resources_total(resp: HttpResponse) -> int:
    """Total de la cabecera `resources`; sin ella no se sabe si hay más páginas."""
    raw = next((v for k, v in resp.headers.items() if k.lower() == "resources"), None)
    if raw is None:
        raise ParseError("no_resources")
    _, _, total = raw.rpartition("/")
    if not total.isdigit():
        raise ParseError("bad_resources")
    return int(total)


def _catalog_products(body: bytes) -> list[_CatalogProduct]:
    data = _load(body)
    if not isinstance(data, list):
        raise ParseError("not_a_list")
    try:
        return [_CatalogProduct.model_validate(raw) for raw in data]
    except ValidationError as exc:
        raise _parse_error(exc) from None


def parse_vtex_catalog(body: bytes) -> list[CatalogObservation]:
    """Una observación por SKU con algún vendedor en stock: gana el más barato."""
    out: list[CatalogObservation] = []
    for product in _catalog_products(body):
        path = max(product.categories, key=len, default="/")
        for item in product.items:
            if not _SKU_RE.fullmatch(item.itemId):
                continue
            offers = [
                (price, s)
                for s in item.sellers
                if s.commertialOffer.AvailableQuantity > 0
                and (price := _money(s.commertialOffer.Price)) is not None
            ]
            if not offers:
                continue
            price, seller = min(offers, key=lambda o: (o[0].minor, o[1].sellerId != "1"))
            ean = item.ean if item.ean and _EAN_RE.fullmatch(item.ean) else None
            first_party = seller.sellerId == "1"
            out.append(
                CatalogObservation(
                    item.itemId,
                    price,
                    path,
                    ean,
                    SellerClass.FIRST_PARTY if first_party else SellerClass.MARKETPLACE,
                )
            )
    return out


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
