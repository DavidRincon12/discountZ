# discountZ: Alkosto por su índice de Algolia (spec de diseño)

- Fecha: 2026-10-20
- Estado: propuesto, pendiente de la revisión de David
- Autor: David Santiago Rincón Bautista
- Base: spec del rastreo (`2026-10-14-rastreo-catalogo-design.md`) y spec de la v1. Donde este spec no dice nada, mandan ellos.
- Borrador aprobado: `/mnt/project-files/alkosto/diseno.md` (David, 2026-10-20).

## 0. Contexto y decisiones

### 0.1 Qué cambia

Alkosto entra como tienda colombiana (COP) con `/seguir`, `/buscar` y un job propio de rastreo de catálogo (`crawl --source alkosto`). Se lee el mismo índice de Algolia que usa su web para buscar, con la clave pública de solo búsqueda que publica su portada.

### 0.2 Decisiones de David (2026-10-20; prevalecen sobre el resto)

| # | Decisión |
|---|---|
| K1 | Sus términos del sitio («Usos NO permitidos») prohíben «spiders… ó cualquier otro dispositivo, programa, algoritmo ó método para acceder, adquirir, copiar ó monitorear» su contenido. David decidió sumarla igual y asume el riesgo (bloqueo o reclamo). |
| K2 | Enfoque: API de Algolia (no HTML), catálogo completo cada hora, mismas reglas de alerta que el rastreo. |
| K3 | Ktronix y Alkomprar quedan fuera de este spec. |

Sigue vigente: no se sortea ningún CAPTCHA ni detección de bots. Si Alkosto bloquea, se apaga con `/fuente off alkosto`.

### 0.3 Mediciones (2026-10-20, desde la nube de la sesión)

| Medida | Valor |
|---|---|
| Índice | app `QX5IPS1B1Q`, índice `alkostoIndexAlgoliaPRD`, host `QX5IPS1B1Q-dsn.algolia.net` |
| Clave | en la portada (`https://www.alkosto.com/`, ~1,4 MB), patrón `apiKey": "<clave>"` |
| Productos | 14.024, todos `instockflag_boolean = true` en la muestra de 1.000; `stocklevel_int` > 0 |
| Paginación | 1.000 resultados por consulta como máximo (`paginationLimitedTo`); la página 1 con `hitsPerPage = 1000` responde vacía |
| Peso | ~1 MB por 1.000 productos con `attributesToRetrieve` acotado |
| Precio | `lowestprice_double` = precio actual (igual al de la ficha: TV Kalley 60" $1.649.900). `pricevalue_cop_double` = «antes» de la tienda, inflado (el mismo TV dice −57 %) |
| Ids | `objectID` = `code_string`, casi siempre el EAN-13 (`7705946474559`) |
| Ficha por lote | `POST /1/indexes/*/objects` (`getObjects`) responde 200 con la clave pública |
| Fotos | `img-820wx820h_string` en `cdn.dam.alkosto.com` (webp; Pillow lo lee) |
| Categorías | `hierarchicalcategory_string_mv.lvl0..lvl3`; 25 de primer nivel (Llantas 2.407, Electrodomésticos 1.835, Accesorios Automotores 1.803, Juguetes 1.184…) |

## 1. Arquitectura

- Adaptador nuevo `sources/alkosto.py` (`AlkostoSource`, código `alkosto`) que implementa `CrawlSource`. Parseo puro con modelos pydantic estrictos y `json.loads(..., parse_float=Decimal)`. Sin cambios de capas ni de contratos de import-linter.
- Cliente `httpx` del proyecto. Todas las peticiones a Algolia son `POST` con JSON y las cabeceras `X-Algolia-Application-Id` y `X-Algolia-API-Key`.
- **Clave:** el adaptador la pide una vez por proceso leyendo la portada por `ctx.http` y la guarda en memoria. Nunca va a logs, a la BD, a `repr` ni a fixtures (se reemplaza por `CLAVE_DE_PRUEBA` al grabar). Si un 403 de Algolia dice que la clave no vale, se vuelve a leer la portada una vez. Si la portada no trae la clave, es `ParseError("api_key")`.
- `SourceDescriptor`: `kind="store"`, `currency=COP`, `hosts={"www.alkosto.com", "alkosto.com"}`, `image_hosts={"cdn.dam.alkosto.com"}`, `capabilities={DETAIL, SEARCH, CATALOG}`, `max_batch=100`, `external_id_re=^[0-9A-Za-z]{1,40}$`, vendedor `FIRST_PARTY` (el índice no trae vendedor de marketplace).

## 2. Lectura

| Uso | Petición |
|---|---|
| Barrido | `POST /1/indexes/alkostoIndexAlgoliaPRD/query` con `hitsPerPage=1000`, `page=0`, `numericFilters=["lowestprice_double>=lo","lowestprice_double<=hi"]` y `attributesToRetrieve` acotado |
| `/seguir`, capa caliente, `fetch_catalog_items`, `fetch_catalog_listings`, `fetch_card_facts` y `confirm` | `POST /1/indexes/*/objects` con hasta 100 `objectID` |
| `/buscar` | `query` con el texto, `hitsPerPage=20` |

- **Observación:** precio = `lowestprice_double` en pesos enteros (`Money` COP, exponente 0); un valor no entero o ≤ 0 es `ParseError`. En stock = `instockflag_boolean` y `stocklevel_int > 0`; lo demás se descarta del barrido y en `confirm` da agotado. `pricevalue_cop_double` y `percentagediscount_string` nunca se usan.
- **Producto:** título `name_text_es`, URL `https://www.alkosto.com` + `url_es_string` (debe pasar `SafeUrl` con host de la fuente), foto `img-820wx820h_string` (si falta, `img-750wx750h_string`), EAN = `code_string` si son 8, 12, 13 o 14 dígitos; ruta de categoría = `/` + `lvl1` en minúsculas con `>` como separador (`/tv/smart tv/`).
- **`parse_url`:** `www.alkosto.com/<slug>/p/<código>` ⇒ `ListingRef("alkosto", <código>)`, sin red.
- **Bloqueos:** 403 de Algolia con cuerpo de clave inválida ⇒ relectura de la clave y, si persiste, `HardBlock`. 429 ⇒ `Unavailable("rate_limited")` (pasa por los reintentos de siempre). Lo demás va por `detect_block_common`.

## 3. Barrido (`crawl --source alkosto`)

- **Particiones:** franjas de precio `alkosto|lo-hi`, empezando por `1-999999999`. En la página 0, si `nbHits > 1000`, la página devuelve `split` con las dos mitades (punto medio geométrico, como `_split_band` de VTEX) y ningún producto. Una franja de un solo precio con más de 1.000 se lee igual (se pierden los que pasan de 1.000 y se registra `alkosto_band_truncated`).
- **Página:** cada franja es una página (`has_more = false`). Con 14 mil productos son ~15–20 peticiones.
- **Ritmo:** `[crawl.alkosto]` con `min_pause_s = 2`, `jitter_s = 1`, `max_per_run = 60`, `max_per_24h = 1000`, `hot_share_bp = 2500`. Cada corrida horaria hace una vuelta completa y el cursor vuelve a empezar al terminar, como hoy.
- **Capa caliente:** la de siempre, con `getObjects` en lotes de 100.
- **Tema:** reglas `alkosto:` en `config/topics.toml` sobre `lvl0`: Accesorios de Electrónica, Audio, Cámaras, Casa Inteligente - Domótica, Celulares, Computadores y Tablet, Relojes y Anillos inteligentes, TV y Videojuegos ⇒ tecnologia; Electrodomésticos ⇒ electro; Colchones, Hogar, Mesa y cocina, Muebles, Navidad y Ropa Hogar ⇒ hogar; Vestuario-Ropa ⇒ moda; Deportes ⇒ deportes; Consumo ⇒ mercado; Juguetes ⇒ bebes; Accesorios Automotores y Llantas ⇒ ferreteria; Servicios, Pines e Instrumentos Musicales ⇒ `intentional_otros`. La prueba existente de cobertura de temas incluye a `alkosto`.

## 4. Detección y tarjeta

- Sin cambios: `domain/catalog.observe` («antes» estable 24 h, 2 lecturas, umbral del tema, confirmación ±1 % y en stock) y la regla del mismo EAN en 6 h entre tiendas, que ahora también cruza Alkosto con Éxito, Carulla, Olímpica y Panamericana.
- Tarjeta del rastreo con la tienda «Alkosto» y botón «Ver en Alkosto». Sin «🇺🇸».

## 5. Datos (migración `0007_alkosto`, `SCHEMA_VERSION = 7`)

- `INSERT` de la fuente `alkosto` (`kind='store'`, `currency='COP'`, `enabled=true`), igual que `0005_ebay` (el breaker nace con la fuente, como en las anteriores). El `down` borra sus filas dependientes y la fuente.
- Sin columnas nuevas. Peso estimado: ~14 mil filas de `catalog_item` (Éxito tiene ~305 mil). `alkosto` entra en `PAUSED_AT_450` (se pausa con la BD en ≥ 450 MB).

## 6. Configuración y operación

- `crawl.yml`: `alkosto` en la matriz (la prueba `test_crawl_workflow_matrix_matches_catalog_sources` lo exige).
- `[sources.alkosto]` para `/seguir`, `/buscar` y la confirmación: `max_per_run = 10`, `max_per_24h = 500`, `watch_interval_min = 30`, `max_batch = 100`.
- Sin secretos nuevos. README: Alkosto en la lista de tiendas, con la nota de K1.
- `CLAUDE.md`: una línea con el gotcha (clave leída de la portada, límite de 1.000 por consulta, «antes» inflado).

## 7. Pruebas

- Fixtures reales en `tests/fixtures/alkosto/`: portada recortada al `<script>` con la clave (reemplazada), una consulta de franja, una con `nbHits > 1000`, un `getObjects` y una búsqueda. `record-fixture` graba la URL final.
- `tests/sources/test_alkosto.py`: parseo (precio, stock, EAN, URL, foto, ruta), `parse_url`, clave ausente ⇒ `ParseError`, franja grande ⇒ `split`, 403 de clave ⇒ relectura una vez y luego `HardBlock`, `pricevalue_cop_double` nunca usado.
- Integración: un crawl de `alkosto` con fixtures contra Postgres guarda productos y respeta el tope; migración `0007` sube y baja.
- Arquitectura: matriz de `crawl.yml` y contratos de import-linter.

## 8. Fuera de alcance

Ktronix y Alkomprar, el carril «más descuento primero», leer el catálogo en cada ciclo de 2 min y el `percentagediscount_string` de la tienda como señal.
