# discountZ: Falabella, solo lo que vende la tienda (spec de diseño)

- Fecha: 2026-10-24
- Estado: aprobado por David (tarjeta «Homecenter y Falabella», 2026-10-23: «Falabella restringido a Vendido por FALABELLA»)
- Autor: David Santiago Rincón Bautista
- Base: spec de Homecenter (`2026-10-24-homecenter-design.md`) y del rastreo. Donde este spec no dice nada, mandan ellos.

## 0. Contexto y decisiones

### 0.1 Qué cambia
Falabella (`www.falabella.com.co`, COP) entra como tienda rastreada, solo en lo que vende la propia tienda: tecnología, electrohogar, moda, relojes y papelería. Es la primera fuente de moda con tienda propia y entra al evento Black Days (26-29 de noviembre).

### 0.2 Decisiones
| # | Decisión |
|---|---|
| F1 | Solo «Vendido por FALABELLA»: filtro `f.derived.variant.sellerId=FALABELLA` en el listado, comprobado de nuevo por producto (`sellerName` del listado y `offerings[].sellerId` de la ficha). |
| F2 | El riesgo de términos es de David (H1 de Homecenter): sus términos no mencionan lectores automáticos y `robots.txt` solo cierra la cesta y la cuenta. Si bloquea, `/fuente off falabella`. No se sortea ningún bloqueo. |
| F3 | Precio = el menor de `internetPrice` y `eventPrice` sin tachar; `cmrPrice` nunca; con dos valores, el menor. |
| F4 | Sin `Capability.SEARCH`: solo rastreo, `/seguir` y confirmación. |

### 0.3 Mediciones (2026-10-24, nube)
| Medida | Valor |
|---|---|
| Datos | `<script id="__NEXT_DATA__">` → `props.pageProps.{results, pagination}`; 48 productos por página + hasta 8 patrocinados (`isSponsored`), ~2,2 MB |
| Paginación | `?page=N`; `pagination.count` da el total; el listado se corta en la página 200 |
| Propios por categoría | Computadores 219, Celulares 97, Audio 181, TV 122, Smartwatch 92, Gaming 84, Fotografía 55, Smart Home 21, Electrohogar 721, Ropa mujer 2.738, Ropa hombre 1.055, Calzado 2.664, Relojes 1.761, Papelería 7: 9.817 en total (≈ 215 páginas) |
| Producto | `productId`, `skuId`, `displayName`, `url` completa, `prices[{type, crossed, price[]}]`, `mediaUrls` (`media.falabella.com.co`), `sellerId`, `sellerName`; sin EAN ni stock |
| Precios | `internetPrice`, `eventPrice`, `cmrPrice` (tarjeta, se ignora) y `normalPrice` tachado (el «antes», se ignora); `price` trae uno o dos textos `"44.990"` |
| Ficha | `productData.variants[].{prices, isPurchaseable, offerings[].sellerId}`; id solo en la URL responde 200 |
| Un solo resultado | con el filtro, una categoría de un producto responde con la ficha, no con el listado |
| Herramientas | Falabella vende por sí misma 1 producto de «Herramientas y máquinas»: no se rastrea (Homecenter cubre ese perfil) |

## 1. Arquitectura
- Adaptador `sources/falabella.py` (`FalabellaSource`, código `falabella`), `CrawlSource`; parseo puro con `json.loads(..., parse_float=Decimal)` y modelos pydantic estrictos. Sin cambios de capas.
- `SourceDescriptor`: `kind="store"`, `currency=COP`, `hosts={"www.falabella.com.co"}`, `image_hosts={"media.falabella.com.co"}`, `capabilities={DETAIL, CONFIRM, CATALOG}`, `max_batch=1`, `external_id_re=^\d{1,12}$`.
- **Particiones:** 14 categorías con `path` fijo bajo prefijos que `topics.toml` ya conoce (`/tecnologia/`, `/electrodomesticos/`, `/moda/`, `/papeleria/`).
- **Página:** `has_more = página × 48 < pagination.count`. Se descartan los repetidos de la página (los patrocinados repiten un producto) y lo que no sea de la tienda; una página sin `results` pero con `productData` es vacía.
- **Capa caliente:** `fetch_catalog_items` vacío (no hay lectura barata por id).
- **Confirmación y tarjeta:** `fetch_catalog_listings` lee la ficha (`/falabella-co/product/<id>`, o el enlace guardado del listado): precio de la ficha por la misma regla, en stock si alguna variante tiene `isPurchaseable`, y vendedor distinto de `FALABELLA_COLOMBIA` ⇒ `ListingGone`. `fetch_card_facts` usa título, enlace y foto del listado.
- `parse_url`: `/falabella-co/product/<id>/…` ⇒ `ListingRef("falabella", <id>)`. Bloqueos: `detect_block_common`.

## 2. Ritmo y configuración
- `[crawl.falabella]`: `min_pause_s = 3`, `jitter_s = 2`, `max_per_run = 100`, `max_per_24h = 700` (≈ 3 vueltas), `hot_share_bp = 0`. `[sources.falabella]` para `/seguir`: `max_per_run = 10`, `max_per_24h = 300`, `watch_interval_min = 60`, `max_batch = 1`.
- `crawl.yml`: `falabella` en la matriz. `[[events]]` Black Days: se agrega `falabella`.
- Migración `0016_falabella` (`SCHEMA_VERSION = 16`): `INSERT` de la fuente. ~10 mil filas de `catalog_item`.

## 3. Pruebas
- Fixtures recortadas en `tests/fixtures/falabella/`: un listado sin filtro (propios, marketplace y patrocinado repetido), la última página de un listado con filtro, una ficha propia, una de marketplace y una sin stock (las dos sintéticas, con `"nota"`).
- `tests/sources/test_falabella.py`: precio (`internetPrice`, `eventPrice`, dos valores, `cmrPrice` y tachado ignorados), marketplace y patrocinados fuera, `has_more`, ficha sola como página vacía, `__NEXT_DATA__` ausente, ficha (stock, vendedor), `parse_url`.
- Integración: un crawl con fixtures guarda productos; la migración `0016` sube y baja. Arquitectura y configuración: matriz de `crawl.yml`, eventos, temas de las 14 rutas.

## 4. Fuera de alcance
Productos de marketplace, `/buscar`, precios por zona, herramientas, hogar y el resto de categorías.
