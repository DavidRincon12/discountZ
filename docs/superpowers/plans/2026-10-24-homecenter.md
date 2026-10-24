# Homecenter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (Native) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rastrear el catálogo de Homecenter (13 categorías del perfil) y alertar bajadas de precio de lo que vende la tienda.

**Architecture:** adaptador `sources/homecenter.py` que implementa `CrawlSource` leyendo `__NEXT_DATA__` de las páginas de categoría; fichas solo para confirmar. Sin cambios de capas ni de `app/`.

**Tech Stack:** Python 3.13, pydantic v2, `json` con `parse_float=Decimal`, pytest, fixtures en `tests/fixtures/homecenter/`.

**Spec:** `docs/superpowers/specs/2026-10-24-homecenter-design.md` (ADR `docs/decisions/0016-homecenter-por-el-json-de-sus-categorias.md`).

## Global Constraints

- Dinero en enteros COP (`Money(…, Currency.COP)`); nunca `float`; JSON con `parse_float=Decimal`.
- Precio = `INTERNET`, si no `NORMAL`; `CMR` y el «antes» de la tienda nunca se usan.
- Logs sin URLs con query, cuerpos ni mensajes de pydantic (`infra.log.event`).
- Solo `/homecenter-co/category/…?currentpage=N` y fichas; nunca `/search`, `/browse` ni `?queryId=`.
- Todo texto redactado en español; commits `tipo(alcance): descripción`, sin atribución a IA.

## Review Focus

- Página con un solo producto legible entre muchos rotos: se descartan los rotos; si ninguno sirve, `ParseError`.
- Producto sin precio `INTERNET` ni `NORMAL` (solo `CMR`): se descarta, no se toma el de tarjeta.
- Ficha de marketplace o sin stock: la confirmación no alerta.
- Id repetido en la misma página y patrocinados: una sola observación.
- `has_more` exacto en la última página (31 de 191) y con `count = 0`.

---

### Task 1: Lectura pura de categorías y fichas

**Files:**
- Create: `src/discountz/sources/homecenter.py`
- Test: `tests/sources/test_homecenter.py`

**Interfaces:**
- Produces: `parse_category(body: bytes) -> CategoryPage` (`items: tuple[HomecenterItem, ...]`, `total: int`); `parse_product(body: bytes) -> ProductInfo` (`title: str`, `price: Money | None`, `in_stock: bool`, `marketplace: bool`, `url: str | None`); `HomecenterItem(external_id, title, price, image_url, path)`.

- [ ] **Step 1: pruebas que fallan** sobre las fixtures: `category-page` da 5 observaciones (el patrocinado fuera), `INTERNET` gana a `NORMAL`, `CMR`+`NORMAL` da `NORMAL`, `total == 8868`, `path` del enlace guardado; sin `__NEXT_DATA__` ⇒ `ParseError("no_next_data")`; sin ningún producto legible ⇒ `ParseError("no_valid_items")`; `product` da `Money(499900)`, en stock, propio; `product-marketplace` ⇒ `marketplace`; `product-out-of-stock` ⇒ `in_stock` falso.
- [ ] **Step 2: correrlas y ver el fallo** (`uv run pytest tests/sources/test_homecenter.py -q`; falla por importación).
- [ ] **Step 3: implementar** el extractor de `__NEXT_DATA__` por expresión regular sobre bytes, modelos pydantic estrictos (`extra="ignore"`, `hide_input_in_errors`), `_price(prices)` y las dos funciones.
- [ ] **Step 4: pruebas en verde**, y commit `feat(homecenter): lee categorías y fichas del JSON de la página`.

### Task 2: `HomecenterSource` y registro

**Files:**
- Modify: `src/discountz/sources/homecenter.py`, `src/discountz/sources/registry.py`
- Test: `tests/sources/test_homecenter.py`, `tests/sources/test_registry.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `HomecenterSource` con `descriptor` (spec §1), `catalog_partitions` (13 `CatalogPartition(key, path)`), `fetch_catalog_page(ctx, partition, page) -> CatalogPage`, `fetch_catalog_listings`, `fetch_listings`, `fetch_card_facts`, `fetch_catalog_items` (vacío), `fetch_changed` (vacío), `confirm`, `parse_url`, `search` (vacío), `detect_block`.

- [ ] **Step 1: pruebas que fallan** con un `HttpClient` falso: la partición pide `…/category/<clave>/?currentpage=N` (sin parámetro en la 1); `has_more` falso en la página 5 de 191 y verdadero en la 1 de 8.868; una partición desconocida ⇒ `ParseError("bad_partition")`; `fetch_catalog_listings` devuelve `ListingFound` con `FIRST_PARTY` y `url_path`, `ListingGone` para marketplace y sin stock, `ListingFailed` ante `ParseError`; `fetch_card_facts` usa título, enlace y foto del listado ya leído; `parse_url` de `/homecenter-co/product/915074/x/915074/` ⇒ `ListingRef("homecenter", "915074")` y de una categoría ⇒ `None`; `route_url` enruta la URL de un producto.
- [ ] **Step 2: ver el fallo.**
- [ ] **Step 3: implementar** y registrar el módulo en `ADAPTER_MODULES`.
- [ ] **Step 4: pruebas en verde** y commit `feat(homecenter): adaptador de rastreo con particiones, fichas y tarjeta`.

### Task 3: Configuración, migración y temas

**Files:**
- Create: `migrations/0015_homecenter.up.sql`, `migrations/0015_homecenter.down.sql`
- Modify: `config/discountz.toml`, `.github/workflows/crawl.yml`, `src/discountz/infra/db.py` (`SCHEMA_VERSION = 15`), `tests/unit/test_config.py`, `tests/unit/test_topics.py`, `tests/integration/test_migrations.py`, `tests/integration/test_crawl_homecenter.py` (nuevo)

- [ ] **Step 1: pruebas que fallan**: `[sources.homecenter]` y `[crawl.homecenter]` con los valores del spec §2; `homecenter` en el evento Black Days; las 13 rutas caen en `ferreteria`, `tecnologia` y `hogar`; la matriz de `crawl.yml` incluye `homecenter` (`test_crawl_workflow_matrix_matches_catalog_sources`); la migración inserta la fuente y baja limpia; un crawl con fixtures guarda productos.
- [ ] **Step 2: ver el fallo.**
- [ ] **Step 3: implementar** la migración (igual que `0007`), la configuración, la matriz y `SCHEMA_VERSION`.
- [ ] **Step 4: verificación completa** (`ruff check`, `ruff format --check`, `mypy`, `lint-imports`, `pytest -q` y la integración con Postgres) y commit `feat(homecenter): migración 0015, presupuesto, evento y matriz del rastreo`.

### Task 4: Documentación y PR

**Files:**
- Modify: `docs/estado.md`, `CLAUDE.md`, `README.md`, `docs/architecture.md`

- [ ] **Step 1:** una línea de gotcha en `CLAUDE.md`, Homecenter en la lista de tiendas del README y de `architecture.md`, y `docs/estado.md` reescrito.
- [ ] **Step 2:** `bash .claude/hooks/git-guard.sh check`, push de la rama y PR con la plantilla del repo, en español y sin pie de IA.
