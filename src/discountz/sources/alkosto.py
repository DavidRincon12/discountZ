"""Adaptador de Alkosto por su índice público de Algolia (spec de Alkosto, ADR 0009).

La web de Alkosto busca en Algolia con una clave de solo búsqueda que publica en su portada. El
adaptador la lee de ahí, la guarda solo en memoria del proceso y nunca la manda a logs, a la BD, a
`repr` ni a mensajes de error. El precio es `lowestprice_double` (el de la ficha); el «antes» de la
tienda (`pricevalue_cop_double`) viene inflado y nunca se lee.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discountz.domain.catalog import CatalogObservation, split_price_band
from discountz.domain.errors import HardBlock, ParseError, SourceError, Unavailable, UnsafeUrl
from discountz.domain.model import ExtractionPath, Lead, ListingRef, Offer, SearchSpec, SellerClass
from discountz.domain.money import Currency, Money
from discountz.domain.urls import SafeUrl
from discountz.infra.http import BlockKind, HttpResponse
from discountz.infra.log import event
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

CODE = "alkosto"
APP_ID = "QX5IPS1B1Q"
INDEX = "alkostoIndexAlgoliaPRD"
ALGOLIA_HOST = f"{APP_ID.lower()}-dsn.algolia.net"
ALGOLIA = f"https://{ALGOLIA_HOST}"
BASE = "https://www.alkosto.com"
HOSTS = frozenset({"www.alkosto.com", "alkosto.com"})
IMAGE_HOSTS = frozenset({"cdn.dam.alkosto.com"})
EXTERNAL_ID_RE = re.compile(r"^[0-9A-Za-z]{1,40}$")
# Solo los campos que se usan: con ellos, 1.000 productos pesan ~1 MB.
ATTRIBUTES: tuple[str, ...] = (
    "objectID",
    "code_string",
    "name_text_es",
    "lowestprice_double",
    "instockflag_boolean",
    "stocklevel_int",
    "url_es_string",
    "img-820wx820h_string",
    "img-750wx750h_string",
    "hierarchicalcategory_string_mv",
)
# La portada la trae como `"apiKey": "<clave>"` en el bloque del buscador (clave segura de Algolia,
# en base64, con el filtro de lo que la tienda muestra).
_API_KEY = re.compile(rb'"apiKey"\s*:\s*"([A-Za-z0-9+/=_]{8,400})"')
_GTIN_LENGTHS = frozenset({8, 12, 13, 14})
_PRODUCT_PATH = re.compile(r"^/[^/]+/p/([0-9A-Za-z]{1,40})/?$")
QUERY_URL = f"{ALGOLIA}/1/indexes/{INDEX}/query"
OBJECTS_URL = f"{ALGOLIA}/1/indexes/*/objects"
MAX_BATCH = 100
SEARCH_HITS = 20
BAD_KEY = "bad_key"
_INVALID_KEY = b"Invalid Application-ID or API key"
# Algolia no pagina más allá de 1.000 resultados por consulta: el barrido parte el catálogo en
# franjas de precio de ≤ 1.000 productos, cada una leída en una sola página.
MAX_HITS = 1000
PRICE_MAX = 999_999_999
_PARTITION = re.compile(r"^([1-9]\d{0,11})-([1-9]\d{0,11})$")

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlkostoItem:
    external_id: str
    title: str
    price: Money | None  # None si no está en stock
    in_stock: bool
    ean: str | None
    category_path: str
    url: str
    image_url: str | None


class _Hit(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    objectID: str
    name_text_es: str
    url_es_string: str
    code_string: str | None = None
    lowestprice_double: Decimal | int | None = None
    instockflag_boolean: bool = False
    stocklevel_int: int = 0
    img_820: str | None = Field(None, alias="img-820wx820h_string")
    img_750: str | None = Field(None, alias="img-750wx750h_string")
    hierarchicalcategory_string_mv: dict[str, str | list[str]] = Field(default_factory=dict)


class _Query(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    hits: list[dict[str, object]]
    nbHits: int


class _Objects(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    results: list[dict[str, object] | None]


def parse_api_key(html: bytes) -> str:
    match = _API_KEY.search(html)
    if match is None:
        raise ParseError("api_key")
    return match.group(1).decode("ascii")


def _load(body: bytes) -> object:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ParseError("bad_json") from exc


def parse_query(body: bytes) -> tuple[list[AlkostoItem], int]:
    """Productos válidos de una consulta y su `nbHits`.

    Un hit inválido se descarta solo; si había hits y ninguno sirve, el formato cambió y es
    `ParseError` (cuenta para el breaker en vez de pasar como una página vacía).
    """
    try:
        page = _Query.model_validate(_load(body))
    except ValidationError as exc:
        raise ParseError("bad_query") from exc
    parsed = [_item(raw) for raw in page.hits]
    items = [i for i in parsed if isinstance(i, AlkostoItem)]
    if parsed and not items:
        raise ParseError("no_valid_hits")
    return items, page.nbHits


def parse_objects(body: bytes, ids: Sequence[str]) -> dict[str, AlkostoItem | ParseError]:
    """Resultado de `getObjects` por id pedido (Algolia responde uno por id, en orden).

    Un id inexistente (`null`) se omite; uno que existe pero no se puede leer queda como
    `ParseError`, para que no pase por «retirado».
    """
    try:
        page = _Objects.model_validate(_load(body))
    except ValidationError as exc:
        raise ParseError("bad_objects") from exc
    if len(page.results) != len(ids):
        raise ParseError("bad_objects")
    out: dict[str, AlkostoItem | ParseError] = {}
    for wanted, raw in zip(ids, page.results, strict=True):
        if raw is None:
            continue
        item = _item(raw)
        if isinstance(item, AlkostoItem) and item.external_id != wanted:
            item = ParseError("other_id")
        out[wanted] = item
    return out


def _item(raw: dict[str, object]) -> AlkostoItem | ParseError:
    try:
        return _to_item(_Hit.model_validate(raw))
    except ValidationError:
        code = "bad_item"
    except UnsafeUrl:
        code = "bad_url"
    except ParseError as exc:
        code = exc.code
    event(log, "alkosto_item_skipped", source=CODE, code=code)
    return ParseError(code)


def _to_item(hit: _Hit) -> AlkostoItem:
    if not EXTERNAL_ID_RE.fullmatch(hit.objectID):
        raise ParseError("bad_id")
    title = hit.name_text_es.strip()
    if not title:
        raise ParseError("no_title")
    if not hit.url_es_string.startswith("/") or hit.url_es_string.startswith("//"):
        raise UnsafeUrl("bad_url")
    url = str(SafeUrl.parse(BASE + hit.url_es_string, HOSTS))
    in_stock = hit.instockflag_boolean and hit.stocklevel_int > 0
    return AlkostoItem(
        external_id=hit.objectID,
        title=title,
        price=_price(hit.lowestprice_double) if in_stock else None,
        in_stock=in_stock,
        ean=_ean(hit.code_string),
        category_path=_category_path(hit.hierarchicalcategory_string_mv),
        url=url,
        image_url=_image(hit.img_820 or hit.img_750),
    )


def _price(value: Decimal | int | None) -> Money:
    """Pesos enteros > 0; un decimal, un cero o la falta del campo es formato roto."""
    if value is None or isinstance(value, bool):
        raise ParseError("bad_price")
    amount = Decimal(value)
    if amount <= 0 or amount != amount.to_integral_value():
        raise ParseError("bad_price")
    return Money(int(amount), Currency.COP)


def _ean(code: str | None) -> str | None:
    if code is None or not code.isdigit() or len(code) not in _GTIN_LENGTHS:
        return None
    return code


def _category_path(levels: dict[str, str | list[str]]) -> str:
    """`TV > Smart TV` ⇒ `/tv/smart tv/` (nivel 1, o el 0 si no hay)."""
    raw = levels.get("lvl1") or levels.get("lvl0") or ""
    text = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(text, str):
        return "/"
    parts = [p.strip().lower() for p in text.split(">") if p.strip()]
    return "/" + "".join(f"{p}/" for p in parts)


def _image(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return str(SafeUrl.parse(raw, IMAGE_HOSTS))
    except UnsafeUrl:
        return None


def _offer(item: AlkostoItem, observed_at: datetime) -> Offer:
    return Offer(
        ref=ListingRef(CODE, item.external_id),
        parent_external_id=None,
        title=item.title,
        price=item.price,
        list_price=None,  # el «antes» de la tienda viene inflado: nunca se usa
        in_stock=item.in_stock,
        seller_class=SellerClass.FIRST_PARTY,  # el índice no trae vendedores de marketplace
        unit_key=pack_unit_key(item.title) or "un|1",
        extraction=ExtractionPath.API_JSON,
        observed_at=observed_at,
        url_path=item.url.removeprefix(BASE),
    )


class AlkostoSource:
    def __init__(self) -> None:
        self.descriptor = SourceDescriptor(
            code=CODE,
            kind="store",
            currency=Currency.COP,
            hosts=HOSTS,
            client="httpx",
            capabilities=frozenset(
                {Capability.DETAIL, Capability.SEARCH, Capability.CONFIRM, Capability.CATALOG}
            ),
            max_batch=MAX_BATCH,
            external_id_re=EXTERNAL_ID_RE,
            image_hosts=IMAGE_HOSTS,
        )
        self._key: str | None = None

    def __repr__(self) -> str:
        return f"AlkostoSource(key={self._key is not None})"

    # --- clave y peticiones -----------------------------------------------------------------

    def _api_key(self, ctx: SourceContext, *, reread: bool) -> str:
        if self._key is None or reread:
            self._key = None
            self._key = parse_api_key(ctx.http.request("GET", BASE + "/").body)
        return self._key

    def _post(self, ctx: SourceContext, url: str, body: Mapping[str, object]) -> bytes:
        """POST a Algolia; una clave rechazada relee la portada una vez y, si sigue, es bloqueo."""
        for reread in (False, True):
            headers = {
                "X-Algolia-Application-Id": APP_ID,
                "X-Algolia-API-Key": self._api_key(ctx, reread=reread),
                "Content-Type": "application/json",
            }
            try:
                return ctx.http.request("POST", url, json=body, headers=headers).body
            except Unavailable as exc:
                if exc.code != BAD_KEY:
                    raise
        self._key = None
        raise HardBlock(CODE, "api_key", 403)

    def _objects(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, AlkostoItem | ParseError]:
        ids = [i for i in external_ids if EXTERNAL_ID_RE.fullmatch(i)]
        found: dict[str, AlkostoItem | ParseError] = {}
        for i in range(0, len(ids), MAX_BATCH):
            batch = ids[i : i + MAX_BATCH]
            requests = [
                {"indexName": INDEX, "objectID": x, "attributesToRetrieve": list(ATTRIBUTES)}
                for x in batch
            ]
            found |= parse_objects(self._post(ctx, OBJECTS_URL, {"requests": requests}), batch)
        return found

    def _valid(self, ctx: SourceContext, external_ids: Sequence[str]) -> list[AlkostoItem]:
        """Las fichas legibles; las rotas ya quedaron registradas como `alkosto_item_skipped`."""
        return [i for i in self._objects(ctx, external_ids).values() if isinstance(i, AlkostoItem)]

    def _query(
        self, ctx: SourceContext, body: Mapping[str, object]
    ) -> tuple[list[AlkostoItem], int]:
        query = {"attributesToRetrieve": list(ATTRIBUTES), **body}
        return parse_query(self._post(ctx, QUERY_URL, query))

    # --- contrato de la v1 ------------------------------------------------------------------

    def parse_url(self, url: SafeUrl) -> ListingRef | NeedsResolution | None:
        m = _PRODUCT_PATH.match(url.path)
        return ListingRef(CODE, m.group(1)) if m else None

    def resolve(self, ctx: SourceContext, pending: NeedsResolution) -> ListingRef:
        raise ParseError("no_resolution")  # los enlaces de Alkosto ya traen el código

    def fetch_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        out: list[ListingOutcome] = []
        for i in range(0, len(refs), MAX_BATCH):
            batch = refs[i : i + MAX_BATCH]
            try:
                found = self._objects(ctx, [r.external_id for r in batch])
            except (SourceError, ParseError) as exc:
                out.extend(ListingFailed(r, exc) for r in batch)
                continue
            for r in batch:
                item = found.get(r.external_id)
                if isinstance(item, ParseError):
                    out.append(ListingFailed(r, item))
                elif item is None:
                    out.append(ListingGone(r))
                else:
                    out.append(ListingFound(_offer(item, ctx.now)))
        return out

    def fetch_catalog_listings(
        self, ctx: SourceContext, refs: Sequence[ListingRef]
    ) -> list[ListingOutcome]:
        return self.fetch_listings(ctx, refs)  # un solo vendedor: la misma ficha

    def search(self, ctx: SourceContext, spec: SearchSpec, page: int) -> SearchPage:
        query = " ".join(spec.query.split())[:100]
        if not query:
            return SearchPage([], 0)
        items, hits = self._query(ctx, {"query": query, "hitsPerPage": SEARCH_HITS, "page": page})
        return SearchPage([_offer(i, ctx.now) for i in items], hits)

    def confirm(self, ctx: SourceContext, offer: Offer) -> Offer | None:
        """Relee la ficha por `getObjects`: None si ya no existe o se agotó."""
        item = self._objects(ctx, [offer.ref.external_id]).get(offer.ref.external_id)
        if isinstance(item, ParseError):
            raise item
        if item is None or not item.in_stock:
            return None
        return _offer(item, ctx.now)

    def fetch_leads(self, ctx: SourceContext) -> list[Lead]:
        return []

    def detect_block(self, resp: HttpResponse) -> BlockKind | None:
        """En Algolia, una clave rechazada o el límite de peticiones no son bloqueos anti-bots."""
        if (urlsplit(resp.url).hostname or "").lower() == ALGOLIA_HOST:  # httpx lo da en minúsculas
            if resp.status == 429:
                raise Unavailable(CODE, "rate_limited", 429)
            if resp.status == 403 and _INVALID_KEY in resp.body[:500]:
                raise Unavailable(CODE, BAD_KEY, 403)
        return detect_block_common(resp)

    # --- catálogo ---------------------------------------------------------------------------

    def catalog_partitions(self, ctx: SourceContext) -> Sequence[str]:
        return [f"1-{PRICE_MAX}"]

    def fetch_catalog_page(self, ctx: SourceContext, partition: str, page: int) -> CatalogPage:
        """Una franja de precio por página; si pasa de 1.000 productos se parte en dos."""
        lo, hi = _parse_partition(partition)
        if page > 0:
            return CatalogPage((), False)
        filters = [f"lowestprice_double>={lo}", f"lowestprice_double<={hi}"]
        items, hits = self._query(
            ctx, {"query": "", "hitsPerPage": MAX_HITS, "page": 0, "numericFilters": filters}
        )
        if hits > MAX_HITS:
            halves = split_price_band(lo, hi)
            if halves:
                return CatalogPage((), False, split=[f"{a}-{b}" for a, b in halves])
            # Un solo precio con más de 1.000 productos: se leen los que llegan.
            event(log, "alkosto_band_truncated", source=CODE, count=hits)
        return CatalogPage(_observations(items), False)

    def fetch_changed(self, ctx: SourceContext, since: datetime, page: int) -> CatalogPage:
        return CatalogPage((), False)  # Algolia no lista cambios: capa caliente por id

    def fetch_catalog_items(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> list[CatalogObservation]:
        return list(_observations(self._valid(ctx, external_ids)))

    def fetch_card_facts(
        self, ctx: SourceContext, external_ids: Sequence[str]
    ) -> dict[str, CardFacts]:
        return {
            i.external_id: CardFacts(i.title, i.url, i.image_url)
            for i in self._valid(ctx, external_ids)
        }


def _parse_partition(partition: str) -> tuple[int, int]:
    m = _PARTITION.fullmatch(partition)
    if m is None or int(m.group(1)) > int(m.group(2)):
        raise ParseError("bad_partition")
    return int(m.group(1)), int(m.group(2))


def _observations(items: Iterable[AlkostoItem]) -> tuple[CatalogObservation, ...]:
    """Solo lo que está en stock (y por eso tiene precio)."""
    return tuple(
        CatalogObservation(i.external_id, i.price, i.category_path, i.ean, SellerClass.FIRST_PARTY)
        for i in items
        if i.in_stock and i.price is not None
    )


register(AlkostoSource())
