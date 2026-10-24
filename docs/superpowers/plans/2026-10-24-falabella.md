# Falabella Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (Native) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rastrear lo que vende la propia Falabella en 14 categorías del perfil y alertar bajadas de precio.

**Architecture:** adaptador `sources/falabella.py` que implementa `CrawlSource` leyendo `__NEXT_DATA__` de los listados filtrados por vendedor; fichas solo para confirmar. Sin cambios de capas ni de `app/`.

**Tech Stack:** Python 3.13, pydantic v2, `json` con `parse_float=Decimal`, pytest, fixtures en `tests/fixtures/falabella/`.

**Spec:** `docs/superpowers/specs/2026-10-24-falabella-design.md` (ADR `docs/decisions/0017-falabella-solo-lo-que-vende-la-tienda.md`).

## Global Constraints

- Dinero en enteros COP (`Money(…, Currency.COP)`); nunca `float`; JSON con `parse_float=Decimal`.
- Precio = el menor de `internetPrice` y `eventPrice` sin tachar; `cmrPrice` y el «antes» tachado nunca se usan.
- Logs sin URLs con query, cuerpos ni mensajes de pydantic (`infra.log.event`).
- Solo `/falabella-co/category/<clave>/<slug>?f.derived.variant.sellerId=FALABELLA&page=N` y fichas.
- Todo texto redactado en español; commits `tipo(alcance): descripción`, sin atribución a IA.

## Review Focus

- Un producto del marketplace en el listado o en la ficha nunca alerta.
- Precio con dos valores: el menor, igual en listado y ficha.
- Producto solo con `cmrPrice`: se descarta, no se toma el de tarjeta.
- Patrocinados y ids repetidos de la página: una sola observación.
- Filtro con un solo producto (la web responde con la ficha): página vacía, no error.
- `has_more` exacto en la última página (219 de 219) y con `count = 0`.

---

### Task 1: Lectura pura de listados y fichas

**Files:**
- Create: `src/discountz/sources/falabella.py`
- Test: `tests/sources/test_falabella.py`

**Interfaces:**
- Produces: `parse_category(body: bytes) -> CategoryPage` (`items: tuple[FalabellaItem, ...]`, `total: int`, `single: bool`); `parse_product(body: bytes) -> ProductInfo` (`title`, `price: Money | None`, `in_stock`, `marketplace`, `url`); `FalabellaItem(external_id, title, price, image_url, url)`.

- [ ] **Step 1: pruebas que fallan** sobre las fixtures: `category-page` da 2 observaciones (el marketplace y el patrocinado fuera, los repetidos una vez), `cmrPrice` ignorado, `total` real; `category-ropa` da el menor de dos valores; `eventPrice` sin `internetPrice` se lee; sin ningún precio legible se descarta; sin `__NEXT_DATA__` ⇒ `ParseError("no_next_data")`; sin ningún producto legible ⇒ `ParseError("no_valid_items")`; una página con `productData` y sin `results` ⇒ `single`; `product` da `Money(2_599_900)`, en stock, propio; `product-marketplace` ⇒ `marketplace`; `product-out-of-stock` ⇒ `in_stock` falso.
- [ ] **Step 2: correrlas y ver el fallo** (falla por importación).
- [ ] **Step 3: implementar** el extractor, los modelos pydantic estrictos (`extra="ignore"`, `hide_input_in_errors`), `_price(prices)` y las dos funciones.
- [ ] **Step 4: pruebas en verde**, y commit `feat(falabella): lee listados y fichas del JSON de la página`.

### Task 2: `FalabellaSource` y registro

**Files:**
- Modify: `src/discountz/sources/falabella.py`, `src/discountz/sources/registry.py`
- Test: `tests/sources/test_falabella.py`, `tests/sources/test_registry.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `FalabellaSource` con `descriptor`, `catalog_partitions` (14 `CatalogPartition(key, path)`), `fetch_catalog_page`, `fetch_catalog_listings`, `fetch_listings`, `fetch_card_facts`, `fetch_catalog_items` (vacío), `fetch_changed` (vacío), `confirm`, `parse_url`, `search` (vacío), `detect_block`.

- [ ] **Step 1: pruebas que fallan** con un `HttpClient` falso: la partición pide `…/category/<clave>/<slug>` con `f.derived.variant.sellerId=FALABELLA` y `page=N`; `has_more` falso en la última página y verdadero con más; filtro de un solo producto ⇒ página vacía sin más páginas; partición desconocida ⇒ `ParseError("bad_partition")`; `fetch_catalog_listings` devuelve `ListingFound` con `FIRST_PARTY`, `ListingGone` para marketplace y `ListingFound` sin stock para agotado, `ListingFailed` ante `ParseError`; `fetch_card_facts` usa lo ya leído; `parse_url` de `/falabella-co/product/73557831/x` ⇒ `ListingRef("falabella", "73557831")`; `route_url` enruta una URL de producto.
- [ ] **Step 2: ver el fallo.**
- [ ] **Step 3: implementar** y registrar el módulo en `ADAPTER_MODULES`.
- [ ] **Step 4: pruebas en verde** y commit `feat(falabella): adaptador de rastreo con particiones, fichas y tarjeta`.

### Task 3: Configuración, migración y temas

**Files:**
- Create: `migrations/0016_falabella.up.sql`, `migrations/0016_falabella.down.sql`, `tests/integration/test_crawl_falabella.py`
- Modify: `config/discountz.toml`, `.github/workflows/crawl.yml`, `src/discountz/infra/db.py` (`SCHEMA_VERSION = 16`), `tests/unit/test_config.py`, `tests/unit/test_topics.py`, `tests/integration/test_migrations.py`, `tests/integration/test_channels_db.py`

- [ ] **Step 1: pruebas que fallan**: `[sources.falabella]` y `[crawl.falabella]` con los valores del spec §2; `falabella` en el evento Black Days; las 14 rutas caen en `tecnologia`, `electro`, `moda` y `libros`; la matriz de `crawl.yml` incluye `falabella`; la migración inserta la fuente y baja limpia; un crawl con fixtures guarda productos.
- [ ] **Step 2: ver el fallo.**
- [ ] **Step 3: implementar** la migración (igual que `0015`), la configuración, la matriz y `SCHEMA_VERSION`.
- [ ] **Step 4: verificación completa** (`ruff check`, `ruff format --check`, `mypy`, `lint-imports`, `pytest -q` y la integración con Postgres) y commit `feat(falabella): migración 0016, presupuesto, evento y matriz del rastreo`.

### Task 4: Documentación y PR

**Files:**
- Modify: `docs/estado.md`, `CLAUDE.md`, `README.md`, `docs/architecture.md`

- [ ] **Step 1:** una línea de gotcha en `CLAUDE.md`, Falabella en el README y en `architecture.md`, y `docs/estado.md` reescrito.
- [ ] **Step 2:** `bash .claude/hooks/git-guard.sh check`, push de la rama y PR con la plantilla del repo, en español y sin pie de IA.
