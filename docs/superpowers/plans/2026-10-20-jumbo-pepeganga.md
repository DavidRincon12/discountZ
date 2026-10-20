# Jumbo y Pepe Ganga Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sumar Jumbo (`jumbo`) y Pepe Ganga (`pepeganga`) como tiendas CO con `/seguir`, `/buscar`, confirmación, canario y rastreo, reutilizando el adaptador VTEX.

**Architecture:** Dos `VtexStore` nuevos en `sources/vtex.py` (el módulo ya registra un `VtexSource` por entrada), migración 0008 que siembra las fuentes y configuración (presupuestos, eventos, canarios, temas, matriz de `crawl.yml`). Sin código de adaptador nuevo.

**Tech Stack:** Python 3.13, uv, pytest, psycopg 3, PostgreSQL 17, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-20-jumbo-pepeganga-design.md`

## Global Constraints

- Jumbo: host `www.jumbocolombia.com`, `api_base` `https://www.jumbocolombia.com/api`, cuenta `jumbocolombiaio`.
- Pepe Ganga: host `www.pepeganga.com`, `api_base` `https://www.pepeganga.com/api`, cuenta `pepeganga`.
- Fuentes nuevas: `('jumbo','store','COP',true,NULL)` y `('pepeganga','store','COP',true,NULL)`; esquema 8.
- `[sources.*]` como Olímpica: grupo propio, `min_pause_s = 2`, `jitter_s = 2`, `max_per_run = 20`, `max_per_24h = 800`, `watch_interval_min = 1`, `search_interval_min = 180`, `max_batch = 50`.
- `[crawl.*]` como Olímpica: `min_pause_s = 2`, `jitter_s = 1`, `max_per_run = 250`, `max_per_24h = 6000`.
- `PAUSED_AT_450` suma `jumbo`, no `pepeganga`.
- Nombres visibles «Jumbo» y «Pepe Ganga». Colores de tarjeta: Jumbo `#00A650`, Pepe Ganga `#F97316` (distintos de los existentes).
- Fixtures grabadas desde Actions (la red de la sesión no llega): `record-fixture` sin redirecciones, URL final; el workflow temporal se borra en la misma tarea.
- Reglas de David: commits `tipo(alcance): descripción en español` a su nombre, sin trailers de IA; `bash .claude/hooks/git-guard.sh check` antes de cada push; TDD estricto.

## Review Focus

1. Producto de marketplace en Pepe Ganga (vendedor distinto de «Pepe Ganga»): `seller_class` correcto en la ficha grabada, como en Éxito. Lo fija el `expected.json` de la Task 2.
2. Foto servida desde `jumbocolombiaio.vteximg.com.br`: el host debe pasar la lista blanca de fotos (`{account}.vteximg.com.br`). Lo fija `test_store_image_hosts_follow_their_account` (Task 2).
3. Página de catálogo de Pepe Ganga (~5,9 MB por 50) bajo el tope de 24 MB: no exige partir; lo cubre la fixture de catálogo (Task 2).
4. Raíces sin tema (Bolsa, Tarjeta Regalo, Logistica, Especiales, Gift Card, Llantas, Entretenimiento): van a `intentional_otros` o a un tema; lo fija `test_every_first_level_category_is_mapped` (Task 4).
5. Bajar la migración con datos de las tiendas: `0008` down borra watch, search, listing y catalog_item antes de la fuente; lo fija `test_0008_up_down` (Task 3).

---

### Task 1: ADR 0010, el carril «más descuento primero» descartado

**Files:**
- Create: `docs/decisions/0010-sin-carril-por-descuento-en-vtex.md`

- [ ] **Step 1: Escribir el ADR** con la plantilla de `arquitectura-y-datos`. Contexto: Éxito tarda ~10 h en volver a ver un producto. Decisión: no agregar el carril `O=OrderByBestDiscountDESC`. Spike: «¿≥ 50 % de las bajadas reales aparecen en las 2 primeras páginas de “más descuento” de su categoría?» · medido: en Tecnología, Mercado, Electrodomésticos y Hogar de Éxito (2026-10-20, `products/search` con `isAvailablePerSalesChannel_1:1`, 2 páginas de 50) 378 de 381 productos con `ListPrice ≥ 2 × Price` y ninguno con `PriceWithoutDiscount > Price` salvo 2: el orden lo dicta el `ListPrice` inflado · no. Cuándo revisar: si VTEX ordena por `PriceWithoutDiscount` o Éxito deja de inflar el `ListPrice`.
- [ ] **Step 2: Commit** `docs(adr): descarta el carril por descuento de VTEX`.

### Task 2: Las dos tiendas registradas, con fixtures y configuración

**Files:**
- Modify: `src/discountz/sources/vtex.py` (`STORES`), `src/discountz/app/render.py` (`SOURCE_NAMES`), `src/discountz/infra/card.py` (`STORE_COLORS`), `config/discountz.toml`, `config/canaries.toml`, `.github/workflows/crawl.yml`
- Create (temporal, se borra en el último paso): `.github/workflows/tmp-fixtures.yml`
- Create: `tests/fixtures/vtex/{jumbo,pepeganga}{-category-tree,-catalog,_busqueda_<q>,_ficha_<sku>,_simulacion_<sku>}.*` y sus `.expected.json`
- Test: `tests/sources/test_vtex.py`, `tests/unit/test_config.py`, `tests/unit/test_card.py`

**Interfaces:**
- Produces: `STORES["jumbo"]`, `STORES["pepeganga"]`; fuentes `jumbo` y `pepeganga` en `load_all()`; fixtures `vtex/jumbo-category-tree` y `vtex/pepeganga-category-tree` (las usa la Task 4).

- [ ] **Step 1: Pruebas que fallan.**
  - `test_vtex.py`: sumar `("jumbo", "<sku>")` y `("pepeganga", "<sku>")` a `FICHAS` y `"jumbo_busqueda_<q>"`, `"pepeganga_busqueda_<q>"` a la lista de búsquedas; `test_store_image_hosts_follow_their_account`: `STORES["jumbo"].account == "jumbocolombiaio"` y la foto de la ficha grabada de cada tienda queda con host `{account}.vteximg.com.br` o `{account}.vtexassets.com`; ampliar el `assert ... <= set(load_all())` a las seis tiendas.
  - `test_config.py`: `test_canaries_cover_vtex_stores` con las seis; `test_crawl_budgets_match_spec` con `jumbo` y `pepeganga` y `(c["jumbo"].min_pause_s, c["jumbo"].max_per_24h) == (2, 6_000)` (igual para `pepeganga`); los eventos «Black Days», «Cyber Lunes» y «Hot Sale» incluyen las dos.
  - `test_card.py`: `STORE_COLORS["jumbo"] == "#00A650"` y `STORE_COLORS["pepeganga"] == "#F97316"`.
- [ ] **Step 2:** `uv run pytest tests/sources/test_vtex.py tests/unit/test_config.py tests/unit/test_card.py -q` → FAIL (fixtures y claves faltan).
- [ ] **Step 3: Registrar las tiendas** en `STORES` (valores de Global Constraints), `SOURCE_NAMES`, `STORE_COLORS`, `[sources.*]`, `[crawl.*]`, los tres eventos CO y la matriz de `crawl.yml` (`tests/arch/test_smoke.py` exige que coincida con las fuentes con catálogo).
- [ ] **Step 4: Grabar fixtures desde Actions.** Workflow temporal disparado por push a la rama de la sesión que corre `uv sync` y `uv run python -m discountz record-fixture vtex <nombre> <url> --folder vtex` (y `--post-json` para la simulación, con el cuerpo que usa `VtexSource.confirm`), y luego imprime `tar czf - tests/fixtures/vtex/{jumbo,pepeganga}* | base64 -w0` en el log. Por tienda: árbol `…/catalog_system/pub/category/tree/1`, catálogo `…/products/search?fq=C:/<raíz>/&fq=isAvailablePerSalesChannel_1:1&_from=0&_to=9`, búsqueda `?ft=<q>&_from=0&_to=9` (Jumbo `televisor`, Pepe Ganga `lego`), ficha `…/products/search/<linkText>/p` del primer resultado de la búsqueda y su simulación. En Pepe Ganga, preferir para la ficha un producto con al menos un vendedor distinto de «Pepe Ganga» si la búsqueda lo trae. Decodificar el log en local, escribir los `.expected.json` a mano desde las fixtures y borrar el workflow en un commit aparte.
- [ ] **Step 5: Canarios:** un `[[jumbo]]` y un `[[pepeganga]]` con el SKU de la ficha y rango ±60 % de su precio.
- [ ] **Step 6:** misma corrida del Step 2 → PASS; luego `uv run pytest tests/arch -q` → PASS.
- [ ] **Step 7: Commits** `ci(tmp): graba fixtures de Jumbo y Pepe Ganga`, `ci(tmp): quita el workflow de fixtures` y `feat(vtex): agrega Jumbo y Pepe Ganga`.

### Task 3: Migración 0008

**Files:**
- Create: `migrations/0008_jumbo_pepeganga.up.sql`, `migrations/0008_jumbo_pepeganga.down.sql`
- Modify: `src/discountz/infra/db.py` (`SCHEMA_VERSION = 8`), `src/discountz/app/crawl.py` (`PAUSED_AT_450`)
- Test: `tests/integration/test_migrations.py`, `tests/integration/test_channels_db.py`, `tests/unit` donde se pruebe `PAUSED_AT_450` (si no hay, agregar `test_paused_at_450_includes_jumbo` en `tests/unit/test_crawl.py`)

- [ ] **Step 1: Pruebas que fallan:** `test_0008_up_down` (como `test_0007_up_down`, con un `catalog_item` y un `listing` de `jumbo`; `migrate(db, 7) == [8]` deja 0 filas de las dos fuentes; `migrate(db, "head") == [8]` las devuelve); listas de versiones con `8` al frente; conteo de fuentes `20`; el diccionario de siembra con `"jumbo": True, "pepeganga": True`; `"jumbo" in PAUSED_AT_450 and "pepeganga" not in PAUSED_AT_450`.
- [ ] **Step 2:** `DISCOUNTZ_TEST_DATABASE_URL=… uv run pytest -q -m integration tests/integration/test_migrations.py tests/integration/test_channels_db.py` → FAIL.
- [ ] **Step 3: Implementar** las dos migraciones con el formato de la `0007` (cabecera con `lock_timeout` y `search_path`; el down borra watch, search, listing y catalog_item de las dos antes de las fuentes), `SCHEMA_VERSION = 8` y `PAUSED_AT_450`.
- [ ] **Step 4:** corrida del Step 2 y la unitaria → PASS.
- [ ] **Step 5: Commit** `feat(db): migración 0008 con Jumbo y Pepe Ganga`.

### Task 4: Temas de sus categorías

**Files:**
- Modify: `config/topics.toml`
- Test: `tests/unit/test_topics.py`

- [ ] **Step 1: Prueba que falla:** `STORES = ("exito", "carulla", "olimpica", "panamericana", "jumbo", "pepeganga")` en `test_topics.py` (parametriza `test_every_first_level_category_is_mapped`) y casos puntuales de subcategoría si una raíz manda a otro tema algo que en Éxito va distinto (p. ej. `assign_topic(rules, "jumbo", "/Supermercado/<mascotas>/") == "mascotas"`, según el árbol grabado).
- [ ] **Step 2:** `uv run pytest tests/unit/test_topics.py -q` → FAIL con las raíces sin tema.
- [ ] **Step 3: Reglas:** prefijos nuevos al comodín `*` cuando el nombre normalizado no existe ya (p. ej. `/supermercado/`, `/televisores y audio/`, `/jugueteria/`, `/videojuegos consolas/`, `/habitacion y colchones/`), reglas de fuente para subcategorías que cambian de tema, y `intentional_otros` para `jumbo:/bolsa/`, `jumbo:/tarjeta regalo/`, `jumbo:/logistica/`, `jumbo:/especiales/`, `pepeganga:/gift card/`, `pepeganga:/llantas/`, `pepeganga:/entretenimiento/`. Cada producto debe llevar el mismo hashtag que en las otras tiendas.
- [ ] **Step 4:** `uv run pytest tests/unit/test_topics.py tests/unit/test_topic_precision.py -q` → PASS (la precisión de canales no debe bajar del 95 %).
- [ ] **Step 5: Commit** `feat(temas): categorías de Jumbo y Pepe Ganga`.

### Task 5: Documentación y cierre

**Files:**
- Modify: `README.md` (lista de tiendas), `CLAUDE.md` (una línea: Jumbo vive en `www.jumbocolombia.com`, solo `sc=1`; Pepe Ganga con `ListPrice` inflado), `docs/architecture.md` (migración 0008), `docs/estado.md` (reescrito)

- [ ] **Step 1:** editar los cuatro archivos.
- [ ] **Step 2: Verificación completa:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports && uv run pytest -q` y la integración con Postgres → verde.
- [ ] **Step 3: Commit** `docs(tiendas): Jumbo y Pepe Ganga en README, CLAUDE.md y estado`; `git-guard.sh check`; push; PR con la plantilla del repo. Al fusionar, David corre `migrate` (esquema 8).
