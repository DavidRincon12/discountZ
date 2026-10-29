"""Adaptador de Mercado Libre Colombia por la API de su catálogo (spec de Mercado Libre, ADR 0018).

Con el token de la app solo abren el catálogo de productos, sus vendedores con precio y la
reputación de cada vendedor: la búsqueda general y las publicaciones dan 403. El precio es el del
vendedor elegible más barato (nuevo, sin envío internacional y en pesos); `original_price` nunca se
lee, porque el descuento sale del histórico propio. El token de la app (OAuth client credentials)
vive en memoria del proceso y nunca va a logs, a la BD ni a `repr`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discountz.domain.catalog import CatalogObservation
from discountz.domain.errors import (
    CredentialUnavailable,
    NotFound,
    ParseError,
    Unavailable,
    UnsafeUrl,
)
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.infra.log import event
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

CODE = "mercadolibre"
API = "https://api.mercadolibre.com"
WEB = "https://www.mercadolibre.com.co"
TOKEN_URL = f"{API}/oauth/token"
HOSTS = frozenset({"www.mercadolibre.com.co"})
IMAGE_HOSTS = frozenset({"http2.mlstatic.com"})
EXTERNAL_ID_RE = re.compile(r"^MCO\d{1,12}$")
RENEW_BEFORE = timedelta(minutes=10)
MAX_SELLER_LOOKUPS = 3  # vendedores sin tienda oficial que se miran por producto
_PRODUCT_PATH = re.compile(r"/(?:[^/]+/)?p/(MCO\d{1,12})/?")
_GREEN = "5_green"
_LEADERS = frozenset({"platinum", "gold"})
_MAX_PRICE = 10**10  # más que esto en pesos no es un precio, y no cabría en el redondeo

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MeliSeller:
    seller_id: int
    price: Money
    official: bool  # tienda oficial de la marca o de la cadena (`official_store_id`)
    free_shipping: bool


@dataclass(frozen=True, slots=True)
class MeliProduct:
    name: str
    image_url: str | None


@dataclass(frozen=True, slots=True)
class _Choice:
    seller: MeliSeller
    leader: str | None  # `None`: tienda oficial; si no, «platinum» o «gold»

    @property
    def seller_class(self) -> SellerClass:
        return SellerClass.FIRST_PARTY if self.leader is None else SellerClass.MARKETPLACE


class _Results(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    results: list[object]


class _Shipping(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    free_shipping: bool = False


class _Seller(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    seller_id: int
    price: int | Decimal
    currency_id: str
    condition: str
    international_delivery_mode: str
    official_store_id: int | None = None
    shipping: _Shipping = Field(default_factory=_Shipping)


class _Product(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    name: str
    pictures: list[object] | None = None


class _Reputation(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    level_id: str | None = None
    power_seller_status: str | None = None


class _User(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    seller_reputation: _Reputation


class _Token(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    access_token: str
    expires_in: int


def _json(body: bytes, code: str) -> dict[str, object]:
    try:
        data = json.loads(body, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:  # ValueError incluye UnicodeDecodeError
        raise ParseError(code) from exc
    if not isinstance(data, dict):
        raise ParseError(code)
    return data


def parse_search(body: bytes) -> str | None:
    """El id del primer producto colombiano (`MCO…`) de una búsqueda, o nada si no hay."""
    try:
        search = _Results.model_validate(_json(body, "bad_search"))
    except ValidationError as exc:
        raise ParseError("bad_search") from exc
    for raw in search.results:
        found = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(found, str) and EXTERNAL_ID_RE.fullmatch(found):
            return found
    return None


def _seller(row: _Seller) -> MeliSeller | None:
    """El vendedor si es elegible; `None` si no lo es o si su precio no sirve."""
    if (row.condition, row.international_delivery_mode, row.currency_id) != ("new", "none", "COP"):
        return None
    if not 0 < row.price <= _MAX_PRICE:
        return None
    minor = to_minor(Decimal(row.price), Currency.COP)
    if minor <= 0:
        return None
    return MeliSeller(
        row.seller_id,
        Money(minor, Currency.COP),
        official=row.official_store_id is not None,
        free_shipping=row.shipping.free_shipping,
    )


def parse_sellers(body: bytes) -> list[MeliSeller]:
    """Vendedores elegibles de un producto, del más barato al más caro (empate por id).

    Un vendedor que no valida se descarta solo; si había vendedores y ninguno valida, el formato
    cambió y es `ParseError`.
    """
    try:
        items = _Results.model_validate(_json(body, "bad_items"))
    except ValidationError as exc:
        raise ParseError("bad_items") from exc
    sellers: list[MeliSeller] = []
    invalid = 0
    for raw in items.results:
        try:
            row = _Seller.model_validate(raw)
        except ValidationError:
            invalid += 1
            continue
        seller = _seller(row)
        if seller is not None:
            sellers.append(seller)
    if invalid:
        event(log, "mercadolibre_sellers_skipped", source=CODE, count=invalid)
    if items.results and invalid == len(items.results):
        raise ParseError("bad_items")
    return sorted(sellers, key=lambda s: (s.price.minor, s.seller_id))


def _picture(pictures: Sequence[object]) -> str | None:
    """La primera foto, solo si está en el CDN de Mercado Libre por https."""
    first = pictures[0] if pictures else None
    raw = first.get("url") if isinstance(first, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return str(SafeUrl.parse(raw, IMAGE_HOSTS))
    except UnsafeUrl:
        return None


def parse_product(body: bytes) -> MeliProduct:
    """Nombre y primera foto de un producto del catálogo; sin nombre es `ParseError`."""
    try:
        product = _Product.model_validate(_json(body, "bad_product"))
    except ValidationError as exc:
        raise ParseError("bad_product") from exc
    name = product.name.strip()
    if not name:
        raise ParseError("bad_product")
    return MeliProduct(name, _picture(product.pictures or []))


def parse_leader(body: bytes) -> str | None:
    """`"platinum"` o `"gold"` si el vendedor es MercadoLíder con reputación verde; si no, nada."""
    try:
        user = _User.model_validate(_json(body, "bad_user"))
    except ValidationError as exc:
        raise ParseError("bad_user") from exc
    reputation = user.seller_reputation
    if reputation.level_id != _GREEN or reputation.power_seller_status not in _LEADERS:
        return None
    return reputation.power_seller_status


def _offer(external_id: str, choice: _Choice, product: MeliProduct, observed_at: datetime) -> Offer:
    return Offer(
        ref=ListingRef(CODE, external_id),
        parent_external_id=None,
        title=product.name,
        price=choice.seller.price,
        list_price=None,  # el precio tachado lo pone el vendedor: el descuento sale del histórico
        in_stock=True,
        seller_class=choice.seller_class,
        unit_key=pack_unit_key(product.name) or "un|1",
        extraction=ExtractionPath.API_JSON,
        observed_at=observed_at,
        url_path=f"/p/{external_id}",
    )


def _note(choice: _Choice) -> str:
    who = "Tienda oficial" if choice.leader is None else f"MercadoLíder {choice.leader.title()}"
    return f"{who} · Envío gratis" if choice.seller.free_shipping else who


class MercadoLibreSource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.COP,
            hosts=HOSTS,
            client="httpx",
            capabilities=frozenset({Capability.DETAIL, Capability.CONFIRM, Capability.CATALOG}),
            max_batch=1,  # una petición de vendedores por producto
            external_id_re=EXTERNAL_ID_RE,
            auth="oauth_client",
            image_hosts=IMAGE_HOSTS,
        )
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._leaders: dict[int, str | None] = {}  # reputación por vendedor, en todo el proceso
        self._chosen: dict[
            str, tuple[_Choice, MeliProduct]
        ] = {}  # la última ficha, para la tarjeta

    def __repr__(self) -> str:
        return f"MercadoLibreSource(token={self._token is not None})"

    # --- token y peticiones -----------------------------------------------------------------

    def _access_token(self, ctx: SourceContext, *, renew: bool) -> str:
        fresh = self._expires_at is not None and ctx.now < self._expires_at - RENEW_BEFORE
        if self._token is not None and fresh and not renew:
            return self._token
        form = {
            "grant_type": "client_credentials",
            "client_id": ctx.credentials.public_id(CODE),
            "client_secret": ctx.credentials.bearer(CODE).get_secret_value(),
        }
        try:
            resp = ctx.http.request(
                "POST",
                TOKEN_URL,
                data=form,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except Unavailable as exc:
            if exc.http_status in (400, 401):
                raise CredentialUnavailable(CODE, "bad_key", exc.http_status) from None
            raise
        try:
            token = _Token.model_validate(_json(resp.body, "bad_token"))
        except ValidationError as exc:
            raise ParseError("bad_token") from exc
        self._token = token.access_token
        self._expires_at = ctx.now + timedelta(seconds=token.expires_in)
        return self._token

    def _get(self, ctx: SourceContext, path: str, params: dict[str, str] | None = None) -> bytes:
        """GET con el token; un 401 renueva el token una vez y, si persiste, la clave es mala."""
        for renew in (False, True):
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token(ctx, renew=renew)}",
            }
            try:
                return ctx.http.request("GET", API + path, params=params, headers=headers).body
            except Unavailable as exc:
                if exc.http_status != 401:
                    raise
        self._token = None
        raise CredentialUnavailable(CODE, "bad_key", 401)

    def _search(self, ctx: SourceContext, ean: str) -> str | None:
        params = {"status": "active", "site_id": "MCO", "product_identifier": ean}
        try:
            return parse_search(self._get(ctx, "/products/search", params))
        except NotFound:
            # Sin la ruta de búsqueda, cada EAN quedaría anotado como «no está»: que el breaker
            # lo vea como formato roto, igual que un 400.
            raise ParseError("bad_request") from None
        except Unavailable as exc:
            if exc.http_status == 400:
                raise ParseError("bad_request") from None
            raise

    # --- regla de vendedor (spec M3) -----------------------------------------------------------

    def _choose(self, ctx: SourceContext, product_id: str) -> _Choice | None:
        """El vendedor elegible más barato: tienda oficial o MercadoLíder Platinum o Gold verde.

        Se miran a lo sumo `MAX_SELLER_LOOKUPS` vendedores sin tienda oficial por producto, estén
        o no en la caché, para que la elección no dependa de lo leído antes en el proceso.
        """
        try:
            sellers = parse_sellers(self._get(ctx, f"/products/{product_id}/items"))
        except NotFound:
            return None  # «No winners found»: nadie lo vende
        looked = 0
        for seller in sellers:
            if seller.official:
                return _Choice(seller, None)
            if looked == MAX_SELLER_LOOKUPS:
                continue
            looked += 1
            leader = self._leader(ctx, seller.seller_id)
            if leader is not None:
                return _Choice(seller, leader)
        return None

    def _leader(self, ctx: SourceContext, seller_id: int) -> str | None:
        if seller_id not in self._leaders:
            self._leaders[seller_id] = self._read_leader(ctx, seller_id)
        return self._leaders[seller_id]

    def _read_leader(self, ctx: SourceContext, seller_id: int) -> str | None:
        """Un perfil que no se deja leer (403, 404 o cambiado) deja al vendedor por fuera."""
        try:
            return parse_leader(self._get(ctx, f"/users/{seller_id}"))
        except NotFound:
            return None
        except Unavailable as exc:
            if exc.http_status != 403:
                raise
            return None
        except ParseError as exc:
            event(log, "mercadolibre_user_skipped", source=CODE, code=exc.code)
            return None

    def _listing(self, ctx: SourceContext, external_id: str) -> Offer | None:
        """La oferta del vendedor elegido con el nombre del producto; nada si no hay elegible."""
        self._chosen.pop(external_id, None)
        if not EXTERNAL_ID_RE.fullmatch(external_id):
            return None
        choice = self._choose(ctx, external_id)
        if choice is None:
            return None
        product = parse_product(self._get(ctx, f"/products/{external_id}"))
        self._chosen[external_id] = (choice, product)
        return _offer(external_id, choice, product, ctx.now)

    # --- contrato de la v1 ------------------------------------------------------------------

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _PRODUCT_PATH.fullmatch(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # los enlaces de producto ya traen el id

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        return SearchPage((), 0)  # la búsqueda general da 403 con el token de la app

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for ref in refs:
            try:
                offer = self._listing(ctx, ref.external_id)
            except (ParseError, NotFound, Unavailable) as exc:
                out.append(ListingFailed(ref, exc))
                continue
            out.append(ListingGone(ref) if offer is None else ListingFound(offer))
        return out

    def fetch_catalog_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return self.fetch_listings(ctx, refs)  # la misma regla de vendedor del rastreo

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        """Ruta independiente del rastreo: los vendedores y el producto, leídos otra vez."""
        return self._listing(ctx, offer.ref.external_id)

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    # --- catálogo ---------------------------------------------------------------------------

    def find_by_ean(self, ctx: SourceContext, ean: str) -> str | None:
        """El producto del catálogo con ese EAN; si no hay y empieza por 0, sin los ceros."""
        found = self._search(ctx, ean)
        bare = ean.lstrip("0")
        if found is None and bare and bare != ean:
            found = self._search(ctx, bare)
        return found

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[CatalogPartition]:
        return ()  # sin recorrido: el catálogo se siembra con EAN (spec §1.3)

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        return CatalogPage((), False)

    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage:
        return CatalogPage((), False)

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> list[CatalogObservation]:
        """Precio y clase del vendedor elegido; el rastreo pone el EAN y el tema de la semilla."""
        out: list[CatalogObservation] = []
        for external_id in external_ids:
            if not EXTERNAL_ID_RE.fullmatch(external_id):
                continue
            choice = self._choose(ctx, external_id)
            if choice is not None:
                out.append(
                    CatalogObservation(
                        external_id, choice.seller.price, "", None, choice.seller_class
                    )
                )
        return out

    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]:
        """Reutiliza la ficha de la confirmación; sin ella, la lee otra vez."""
        out: dict[str, CardFacts] = {}
        for external_id in external_ids:
            if external_id not in self._chosen:
                self._listing(ctx, external_id)
            seen = self._chosen.get(external_id)
            if seen is None:
                continue
            choice, product = seen
            url = f"{WEB}/p/{external_id}"
            out[external_id] = CardFacts(product.name, url, product.image_url, note=_note(choice))
        return out

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        """El perfil de un vendedor puede negarse (403) sin que la API esté bloqueada."""
        if urlsplit(resp.url).path.startswith("/users/"):
            return None
        return detect_block_common(resp)


register(MercadoLibreSource())
