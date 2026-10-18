"""Adaptador de eBay por la Browse API (spec de eBay §2, ADR 0008).

Solo publicaciones nuevas, a precio fijo y con envío dentro de EE. UU. (al casillero de Miami): el
precio observado incluye el envío más barato a `ship_to_zip`. El token de aplicación (OAuth client
credentials) vive en memoria del proceso y nunca va a logs, a la BD ni a `repr`.

La lectura en lote (`getItems`) responde 403 sin acceso especial (prueba del 2026-10-18): toda ficha
se lee de a una con `getItem`, de la misma cuota que la búsqueda.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, ValidationError

from discountz.config import PARAMS_PATH, EbayParams, load_params
from discountz.domain.catalog import CatalogObservation
from discountz.domain.errors import (
    CredentialUnavailable,
    NotFound,
    ParseError,
    SourceError,
    Unavailable,
)
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money, format_money, to_minor
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.sources.base import (
    Capability,
    CardFacts,
    CatalogPage,
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

CODE = "ebay_us"
API = "https://api.ebay.com"
BROWSE = f"{API}/buy/browse/v1"
TOKEN_URL = f"{API}/identity/v1/oauth2/token"
TAXONOMY_URL = f"{API}/commerce/taxonomy/v1/category_tree/0"
SCOPE = "https://api.ebay.com/oauth/api_scope"
SEARCH_LIMIT = 50
# `conditionIds:{1000}` y no `conditions:{NEW}`: ese grupo trae «Open box» (1500), que en la prueba
# llenó 5 de 6 lugares de una categoría y bajaría la mediana del modelo.
FILTER = (
    "conditionIds:{1000},buyingOptions:{FIXED_PRICE},deliveryCountry:US,"
    "price:[15..],priceCurrency:USD"
)
NEW_CONDITION = "1000"
RENEW_BEFORE = timedelta(minutes=5)
QUOTA_ERROR_ID = 2001
EXTERNAL_ID_RE = re.compile(r"^\d{9,15}(?:V\d{1,20})?$")
_ITEM_ID = re.compile(r"^v1\|(\d{9,15})\|(\d{1,20})$")
_EXTERNAL = re.compile(r"^(\d{9,15})(?:V(\d{1,20}))?$")
_ITM_PATH = re.compile(r"^/itm/(?:[^/]+/)?(\d{9,15})/?$")
_DIGITS = re.compile(r"^\d{1,20}$")
_EPID = re.compile(r"^\d{4,15}$")
_PARTITION = re.compile(r"^\d{1,12}$")
PARTITION_LEVEL = 3  # categorías de nivel 3 (o de nivel 2 sin hijos): ~2.054 particiones
_IN_STOCK = frozenset({"IN_STOCK", "LIMITED_STOCK"})


def from_item_id(item_id: str) -> str | None:
    """`v1|<legacy>|<variación>` ⇒ `<legacy>` o `<legacy>V<variación>` (la BD pide alfanumérico)."""
    m = _ITEM_ID.fullmatch(item_id)
    if m is None:
        return None
    legacy, variation = m.groups()
    return legacy if int(variation) == 0 else f"{legacy}V{variation}"


def to_item_id(external_id: str) -> str:
    m = _EXTERNAL.fullmatch(external_id)
    if m is None:
        raise ParseError("bad_external_id")
    legacy, variation = m.groups()
    return f"v1|{legacy}|{variation or 0}"


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)


class _Amount(_Model):
    value: str
    currency: str


class _Seller(_Model):
    feedbackPercentage: str | None = None
    feedbackScore: int | None = None


class _Shipping(_Model):
    shippingCost: _Amount | None = None


class _Category(_Model):
    categoryId: str
    categoryName: str


class _Image(_Model):
    imageUrl: str | None = None


class _Availability(_Model):
    estimatedAvailabilityStatus: str | None = None


class _Listing(_Model):
    itemId: str
    title: str = ""
    price: _Amount | None = None
    seller: _Seller | None = None
    conditionId: str | None = None
    shippingOptions: list[_Shipping] = []
    epid: str | None = None
    itemGroupHref: str | None = None
    image: _Image | None = None
    categories: list[_Category] = []  # búsqueda: la hoja primero y luego desde la raíz
    categoryPath: str | None = None  # ficha: `A|B|C`
    estimatedAvailabilities: list[_Availability] = []
    lotSize: int | None = None


class _SearchPage(_Model):
    total: int = 0
    itemSummaries: list[_Listing] = []


class _TreeCategory(_Model):
    categoryId: str


class _TreeNode(_Model):
    category: _TreeCategory
    categoryTreeNodeLevel: int = 0
    childCategoryTreeNodes: list[_TreeNode] = []


class _Tree(_Model):
    rootCategoryNode: _TreeNode


class _Token(_Model):
    access_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class EbayRules:
    min_feedback_pct: int
    min_feedback_score: int
    allow_variants: bool


@dataclass(frozen=True, slots=True)
class EbayItem:
    external_id: str
    title: str
    price: Money  # con el envío más barato a Miami
    shipping: Money
    seller_class: SellerClass
    seller_pct: Decimal
    seller_score: int
    model_key: str | None
    category_path: str
    image_url: str | None
    web_url: str
    in_stock: bool


def _load(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise ParseError("bad_json") from exc


def _validate[M: BaseModel](model: type[M], data: object) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False)[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))[:60]
        raise ParseError("schema", loc=loc or None) from None


def _usd(amount: _Amount | None) -> Money | None:
    if amount is None or amount.currency != "USD":
        return None
    try:
        minor = to_minor(Decimal(amount.value), Currency.USD)
    except InvalidOperation:
        return None
    return Money(minor, Currency.USD) if minor >= 0 else None


def _cheapest_shipping(options: list[_Shipping]) -> Money | None:
    costs = [m for o in options if (m := _usd(o.shippingCost)) is not None]
    return min(costs, key=lambda m: m.minor) if costs else None


def _category_path(listing: _Listing) -> str:
    if listing.categoryPath:
        names = listing.categoryPath.split("|")
    elif listing.categories:
        leaf, *ancestors = listing.categories
        names = [c.categoryName for c in ancestors] + [leaf.categoryName]
    else:
        names = []
    return "/" + "".join(f"{n}/" for n in names)


def _seller(listing: _Listing, rules: EbayRules) -> tuple[SellerClass, Decimal, int]:
    seller = listing.seller or _Seller()
    try:
        pct = Decimal(seller.feedbackPercentage or "0")
    except InvalidOperation:
        pct = Decimal(0)
    if not pct.is_finite():  # «NaN» o «Infinity» no se comparan: vendedor sin dato
        pct = Decimal(0)
    score = seller.feedbackScore or 0
    ok = pct >= rules.min_feedback_pct and score >= rules.min_feedback_score
    return (SellerClass.FIRST_PARTY if ok else SellerClass.MARKETPLACE), pct, score


def _web_url(external_id: str) -> str:
    m = _EXTERNAL.fullmatch(external_id)
    assert m is not None  # noqa: S101 - external_id sale de from_item_id
    legacy, variation = m.groups()
    return f"https://www.ebay.com/itm/{legacy}" + (f"?var={variation}" if variation else "")


def _item(listing: _Listing, rules: EbayRules) -> EbayItem | None:
    """Lo común a búsqueda y ficha: id válido, precio en USD y costo de envío conocido."""
    external_id = from_item_id(listing.itemId)
    price = _usd(listing.price)
    shipping = _cheapest_shipping(listing.shippingOptions)
    if external_id is None or price is None or price.minor == 0 or shipping is None:
        return None
    if listing.itemGroupHref and not rules.allow_variants:
        return None
    if listing.lotSize is not None and listing.lotSize > 1:
        return None
    seller_class, pct, score = _seller(listing, rules)
    statuses = [a.estimatedAvailabilityStatus for a in listing.estimatedAvailabilities]
    in_stock = not statuses or any(s in _IN_STOCK for s in statuses)
    epid = listing.epid if listing.epid and _EPID.fullmatch(listing.epid) else None
    return EbayItem(
        external_id=external_id,
        title=" ".join(listing.title.split()),
        price=Money(price.minor + shipping.minor, Currency.USD),
        shipping=shipping,
        seller_class=seller_class,
        seller_pct=pct,
        seller_score=score,
        model_key=epid,
        category_path=_category_path(listing),
        image_url=listing.image.imageUrl if listing.image else None,
        web_url=_web_url(external_id),
        in_stock=in_stock,
    )


def parse_search(body: bytes, rules: EbayRules) -> list[EbayItem]:
    """Una página de búsqueda: solo nuevas y de vendedores que pasan B4 (spec §2)."""
    page = _validate(_SearchPage, _load(body))
    out: list[EbayItem] = []
    for listing in page.itemSummaries:
        if listing.conditionId != NEW_CONDITION:
            continue
        item = _item(listing, rules)
        if item is not None and item.seller_class is SellerClass.FIRST_PARTY:
            out.append(item)
    return out


def parse_item(body: bytes, rules: EbayRules) -> EbayItem | None:
    """Una ficha (`getItem`): el vendedor bajo B4 queda como `MARKETPLACE`, no se descarta."""
    return _item(_validate(_Listing, _load(body)), rules)


def _offer(item: EbayItem, observed_at: datetime) -> Offer:
    variant = "V" in item.external_id
    return Offer(
        ref=ListingRef(CODE, item.external_id),
        parent_external_id=item.external_id.split("V", 1)[0] if variant else None,
        title=item.title,
        price=item.price if item.in_stock else None,
        list_price=None,  # el precio tachado lo pone el vendedor: nunca es «antes»
        in_stock=item.in_stock,
        seller_class=item.seller_class,
        unit_key=pack_unit_key(item.title) or "un|1",
        extraction=ExtractionPath.API_JSON,
        observed_at=observed_at,
        url_path=item.web_url.removeprefix("https://www.ebay.com"),
    )


def _partitions(node: _TreeNode) -> list[str]:
    if node.categoryTreeNodeLevel >= PARTITION_LEVEL or not node.childCategoryTreeNodes:
        return [node.category.categoryId]
    return [p for child in node.childCategoryTreeNodes for p in _partitions(child)]


def _observation(item: EbayItem) -> CatalogObservation:
    return CatalogObservation(
        item.external_id, item.price, item.category_path, None, item.seller_class, item.model_key
    )


def _note(item: EbayItem) -> str:
    pct = f"{item.seller_pct.normalize():f}".replace(".", ",")
    score = f"{item.seller_score:,}".replace(",", ".")
    note = f"vendedor {pct} % ({score})"
    if item.shipping.minor > 0:
        note += f" · incluye envío a Miami {format_money(item.shipping)}"
    return note


class EbaySource:
    def __init__(self, params: EbayParams) -> None:
        self.params = params
        self.rules = EbayRules(params.min_feedback_pct, params.min_feedback_score, False)
        # Una ficha pedida por id ya es la variante elegida: se acepta aunque tenga grupo.
        self._item_rules = EbayRules(params.min_feedback_pct, params.min_feedback_score, True)
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.USD,
            hosts=frozenset({"www.ebay.com", "ebay.com"}),
            client="httpx",
            capabilities=frozenset(
                {Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM, Capability.CATALOG}
            ),
            max_batch=1,  # `getItems` sin acceso: una ficha por petición
            external_id_re=EXTERNAL_ID_RE,
            auth="oauth_client",
            image_hosts=frozenset({"i.ebayimg.com"}),
        )
        self._headers = {
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "X-EBAY-C-ENDUSERCTX": "contextualLocation="
            + quote(f"country=US,zip={params.ship_to_zip}", safe=""),
        }
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def __repr__(self) -> str:
        return f"EbaySource(token={self._token is not None})"

    # --- token y peticiones -----------------------------------------------------------------

    def _access_token(self, ctx: SourceContext, *, renew: bool) -> str:
        fresh = (
            self._token is not None
            and self._expires_at is not None
            and ctx.now < self._expires_at - RENEW_BEFORE
        )
        if self._token is not None and fresh and not renew:
            return self._token
        basic = ctx.credentials.bearer("ebay").get_secret_value()
        try:
            resp = ctx.http.request(
                "POST",
                TOKEN_URL,
                data={"grant_type": "client_credentials", "scope": SCOPE},
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except Unavailable as exc:
            if exc.http_status in (400, 401):
                raise CredentialUnavailable(CODE, "bad_key", exc.http_status) from None
            raise
        token = _validate(_Token, _load(resp.body))
        self._token = token.access_token
        self._expires_at = ctx.now + timedelta(seconds=token.expires_in)
        return self._token

    def _get(self, ctx: SourceContext, url: str, params: dict[str, str] | None = None) -> bytes:
        """GET con el token; un 401 renueva el token una vez y, si persiste, la clave es mala."""
        for renew in (False, True):
            token = self._access_token(ctx, renew=renew)
            headers = {**self._headers, "Authorization": f"Bearer {token}"}
            try:
                return ctx.http.request("GET", url, params=params, headers=headers).body
            except Unavailable as exc:
                if exc.http_status != 401:
                    raise
        self._token = None
        raise CredentialUnavailable(CODE, "bad_key", 401)

    def _fetch_item(self, ctx: SourceContext, external_id: str) -> EbayItem | None:
        """Una ficha por `getItem`; None si no existe o ya no es comparable (lote, sin envío)."""
        if not EXTERNAL_ID_RE.fullmatch(external_id):
            return None
        url = f"{BROWSE}/item/{quote(to_item_id(external_id), safe='')}"
        try:
            item = parse_item(self._get(ctx, url), self._item_rules)
        except NotFound:
            return None
        return item if item is not None and item.external_id == external_id else None

    def _items(self, ctx: SourceContext, external_ids: Sequence[str]) -> list[EbayItem]:
        out: list[EbayItem] = []
        for external_id in external_ids:
            item = self._fetch_item(ctx, external_id)
            if item is not None:
                out.append(item)
        return out

    def _search(self, ctx: SourceContext, params: dict[str, str]) -> list[EbayItem]:
        body = self._get(ctx, f"{BROWSE}/item_summary/search", {**params, "filter": FILTER})
        return parse_search(body, self.rules)

    # --- contrato de la v1 ------------------------------------------------------------------

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _ITM_PATH.match(url.path)
        if m is None:
            return None
        var = dict(url.query).get("var", "0")
        variation = int(var) if _DIGITS.fullmatch(var) else 0
        external_id = m.group(1) if variation == 0 else f"{m.group(1)}V{variation}"
        return ListingRef(CODE, external_id)

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # los enlaces de eBay ya traen el id

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        del page  # una sola página de 50
        query = " ".join(spec.query.split())[:100]
        if not query:
            return SearchPage([], 0)
        items = self._search(ctx, {"q": query, "limit": str(SEARCH_LIMIT)})
        return SearchPage([_offer(i, ctx.now) for i in items], None)

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        quota: Unavailable | None = None
        for ref in refs:
            if quota is not None:  # sin cuota no se gastan más peticiones
                out.append(ListingFailed(ref, quota))
                continue
            try:
                item = self._fetch_item(ctx, ref.external_id)
            except (SourceError, ParseError) as exc:
                if isinstance(exc, CredentialUnavailable):
                    raise
                if isinstance(exc, Unavailable) and exc.code == "quota":
                    quota = exc
                out.append(ListingFailed(ref, exc))
                continue
            out.append(ListingFound(_offer(item, ctx.now)) if item else ListingGone(ref))
        return out

    def fetch_catalog_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return self.fetch_listings(ctx, refs)  # la ficha trae el vendedor: misma regla B4

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        """Ruta independiente de la búsqueda: la ficha de `getItem` con el envío a Miami."""
        item = self._fetch_item(ctx, offer.ref.external_id)
        return _offer(item, ctx.now) if item is not None else None

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    # --- catálogo ---------------------------------------------------------------------------

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[str]:
        tree = _validate(_Tree, _load(self._get(ctx, TAXONOMY_URL)))
        excluded = {str(r) for r in self.params.excluded_roots}
        roots = [
            n
            for n in tree.rootCategoryNode.childCategoryTreeNodes
            if n.category.categoryId not in excluded
        ]
        found = {p for root in roots for p in _partitions(root) if _PARTITION.fullmatch(p)}
        return sorted(found, key=int)

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        """Una búsqueda por categoría en el orden de eBay; cada partición es una sola página."""
        if not _PARTITION.fullmatch(partition):
            raise ParseError("bad_partition")
        if page > 0:
            return CatalogPage((), False)
        limit = str(self.params.items_per_partition)
        items = self._search(ctx, {"category_ids": partition, "limit": limit})
        return CatalogPage(tuple(_observation(i) for i in items if i.in_stock), False)

    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage:
        return CatalogPage((), False)  # la Browse API no lista cambios: capa caliente por id

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> list[CatalogObservation]:
        return [_observation(i) for i in self._items(ctx, external_ids) if i.in_stock]

    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]:
        return {
            i.external_id: CardFacts(i.title, i.web_url, i.image_url, note=_note(i))
            for i in self._items(ctx, external_ids)
        }

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        """Cuota y permisos de la API no son bloqueos anti-bots: no abren el breaker."""
        if resp.status == 429 or _error_ids(resp) & {QUOTA_ERROR_ID}:
            raise Unavailable(CODE, "quota", resp.status)
        if resp.status == 403 and _error_ids(resp):
            raise CredentialUnavailable(CODE, "forbidden", 403)
        return detect_block_common(resp)


def _error_ids(resp: HttpResponse) -> set[int]:
    if resp.status < 400 or not resp.body.lstrip().startswith(b"{"):
        return set()
    try:
        data = json.loads(resp.body[:65536])
    except ValueError:
        return set()
    errors = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errors, list):
        return set()
    return {
        e["errorId"] for e in errors if isinstance(e, dict) and isinstance(e.get("errorId"), int)
    }


register(EbaySource(load_params(PARAMS_PATH).ebay))
