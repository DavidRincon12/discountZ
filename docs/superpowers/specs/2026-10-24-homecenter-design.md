# discountZ: Homecenter por el JSON de sus páginas de categoría (spec de diseño)

- Fecha: 2026-10-24
- Estado: aprobado por David (tarjeta «Homecenter y Falabella», 2026-10-23)
- Autor: David Santiago Rincón Bautista
- Base: spec del rastreo (`2026-10-14-rastreo-catalogo-design.md`) y spec de Alkosto. Donde este spec no dice nada, mandan ellos.

## 0. Contexto y decisiones

### 0.1 Qué cambia

Homecenter (Sodimac Colombia, `homecenter.com.co`, COP) entra como tienda rastreada: herramientas, computadores, celulares, relojes inteligentes y sillas de escritorio. Es la tienda con más herramientas del perfil de David y hoy ninguna fuente la cubre.

### 0.2 Decisiones

| # | Decisión |
|---|---|
| H1 | David aprobó construirla (2026-10-23) tras la prueba desde Actions: las páginas responden 200 sin desafío, con fotos. Sus términos limitan el contenido a «revisión, compra y consulta» y prohíben copiarlo o distribuirlo, y no mencionan lectores automáticos: riesgo medio, como Alkosto (ADR 0009). Si bloquea, se apaga con `/fuente off homecenter`. No se sortea ningún bloqueo. |
| H2 | Solo se lee lo que `robots.txt` permite: `/homecenter-co/category/…` con `?currentpage=N`. Nunca `/search`, `/browse` ni `?queryId=`. Por eso no tiene `Capability.SEARCH` (`/buscar` no la usa). |
| H3 | Solo lo que vende Homecenter. El listado no trae vendedor; la ficha sí (`attributes.is_marketplace_product`). La confirmación descarta lo del marketplace. |

### 0.3 Mediciones (2026-10-23, nube y Actions)

| Medida | Valor |
|---|---|
| Datos | `<script id="__NEXT_DATA__">` → `props.pageProps.searchProps.searchData.{results, pagination}`; 40 productos por página y ~1,9 MB |
| Paginación | `?currentpage=N` sin tope (página 222 de 222 en la categoría de 8.868); `pagination.count` da el total |
| Producto | `productId`, `skuId`, `displayName`, `brand`, `prices[{type, priceWithoutFormatting}]`, `mediaUrls`, `isPromoted`; sin vendedor, sin EAN, sin enlace |
| Precios | `INTERNET` + `NORMAL`, `CMR` (tarjeta de la tienda) + `NORMAL`, o solo `NORMAL` |
| Patrocinados | `isPromoted = true` (anuncios con `position` y `auctionedId`; ~3 % de la página) |
| Enlace | `/homecenter-co/product/<id>/<slug>/<sku>/`; con `x` de slug, `301` al canónico |
| Ficha | `productProps.result.variants[0].{price, availability[].hasStock}`, `attributes.is_marketplace_product`, `breadcrumbs` |
| Fotos | `media.falabella.com/sodimacCO/<id>/public` responde desde Actions |
| Categorías del perfil | 13, suman 14.812 productos (≈ 370 páginas, ≈ 700 MB por vuelta) |
| Slug | en minúsculas; con mayúsculas responde `301` |

## 1. Arquitectura

- Adaptador nuevo `sources/homecenter.py` (`HomecenterSource`, código `homecenter`), `CrawlSource`; parseo puro: se extrae el `<script id="__NEXT_DATA__">` con una expresión regular sobre bytes y `json.loads(..., parse_float=Decimal)`; modelos pydantic estrictos. Sin cambios de capas.
- `SourceDescriptor`: `kind="store"`, `currency=COP`, `hosts={"www.homecenter.com.co"}`, `image_hosts={"media.falabella.com"}`, `capabilities={DETAIL, CONFIRM, CATALOG}`, `max_batch=1`, `external_id_re=^\d{1,12}$`.
- **Precio** = `INTERNET` si existe; si no, `NORMAL`. `CMR` nunca (exige tarjeta). El «antes» no se lee: el descuento sale del histórico propio. Un producto sin precio legible se descarta.
- **Particiones:** una por categoría del perfil, clave `cat1670400/herramientas-electricas-e-inalambricas`; `path` fijo por categoría (`/herramientas y ferreteria/…`, `/tecnologia/…`, `/hogar/…`) para que `crawl_wanted` y las reglas existentes de `topics.toml` decidan el tema sin reglas nuevas.
- **Página:** `has_more = página × 40 < pagination.count`. Se descartan los patrocinados y los ids repetidos de la página.
- **Capa caliente:** no hay lectura por id barata, así que `fetch_catalog_items` devuelve vacío; la vuelta completa es el único recorrido.
- **Confirmación y tarjeta:** `fetch_catalog_listings` lee la ficha de cada candidato (el enlace sale del listado, guardado en memoria; si falta, `…/product/<id>/x/<id>/` y un salto de `301` validado con `SafeUrl`). Precio `INTERNET`/`NORMAL` de la ficha, en stock si algún `hasStock` es verdadero, y `is_marketplace_product = true` ⇒ `ListingGone`. `fetch_card_facts` usa el título, el enlace y la foto del listado.
- `parse_url`: `/homecenter-co/product/<id>/…` ⇒ `ListingRef("homecenter", <id>)`, sin red. `search` devuelve vacío.
- **Bloqueos:** `detect_block_common`; un `301` inesperado no es bloqueo.

## 2. Ritmo y configuración

- `[crawl.homecenter]`: `min_pause_s = 3`, `jitter_s = 2`, `max_per_run = 100`, `max_per_24h = 800` (≈ 2 vueltas al día). `[sources.homecenter]` para `/seguir`: `max_per_run = 10`, `max_per_24h = 300`, `watch_interval_min = 60`, `max_batch = 1`.
- `crawl.yml`: `homecenter` en la matriz (lo exige `test_crawl_workflow_matrix_matches_catalog_sources`). `[[events]]` Black Days: se agrega `homecenter`.
- Migración `0015_homecenter` (`SCHEMA_VERSION = 15`): `INSERT` de la fuente. ~15 mil filas de `catalog_item` (~6 MB).

## 3. Pruebas

- Fixtures reales recortadas en `tests/fixtures/homecenter/`: una página de categoría (con patrocinados, `CMR` y solo `NORMAL`), la última página de una categoría, una ficha propia y una de marketplace sintética marcada con `"nota"`.
- `tests/sources/test_homecenter.py`: precio (`INTERNET` > `NORMAL`, `CMR` ignorado), patrocinados y repetidos fuera, `has_more`, enlace, `__NEXT_DATA__` ausente ⇒ `ParseError`, ficha (stock, marketplace), `parse_url`.
- Integración: un crawl de `homecenter` con fixtures guarda productos; la migración `0015` sube y baja.
- Arquitectura y configuración: matriz de `crawl.yml`, eventos, temas de las 13 rutas.

## 4. Fuera de alcance

Falabella (spec propio), `/buscar` en Homecenter, precios por zona o ciudad, y el resto de categorías de la tienda.
