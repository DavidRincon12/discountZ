# discountZ: eBay por la Browse API (spec de diseño, SP-4)

- Fecha: 2026-10-18
- Estado: propuesto, pendiente de la revisión de David
- Autor: David Santiago Rincón Bautista
- Base: spec de la v1 (`2026-10-13-discountz-v1-design.md`, fila SP-4 y §4.2) y spec del rastreo (`2026-10-14-rastreo-catalogo-design.md`). Donde este spec no dice nada, mandan ellos.
- Borrador aprobado: `/mnt/project-files/ebay/diseno-propuesto.md` (David, 2026-10-18).

## 0. Contexto y decisiones

### 0.1 Qué cambia

eBay entra como tienda de EE. UU. por su API oficial (Browse API) con las claves de producción de David. Funciona con `/seguir` y `/buscar` como Newegg y, además, tiene un job propio en el rastreo de catálogo (`crawl`) que descubre ofertas solo. Detecta dos cosas: bajadas de precio de una misma publicación y publicaciones mucho más baratas que las demás del mismo modelo.

### 0.2 Decisiones de David (2026-10-18; prevalecen sobre el resto del documento)

| # | Decisión |
|---|---|
| B1 | Solo publicaciones que envían a una dirección de EE. UU. (el casillero). No se mira el envío directo a Colombia. Precios en USD y «🇺🇸 Compra por casillero». |
| B2 | eBay funciona con `/seguir` y `/buscar`, igual que Newegg. |
| B3 | Además, un barrido por categorías que abarque todo lo posible y compare cada publicación con las demás del mismo modelo. |
| B4 | Vale el borrador: 19 categorías raíz, artículos nuevos, precio fijo, vendedor con ≥ 98 % positivo y ≥ 100 calificaciones, precio ≥ USD 15 y tope de 150 mil publicaciones guardadas. |

### 0.3 Mediciones (2026-10-18, desde GitHub Actions con las claves de David)

| Medida | Valor |
|---|---|
| Token de aplicación (`client_credentials`, scope `https://api.ebay.com/oauth/api_scope`) | HTTP 200, dura 7.200 s |
| Cuota diaria (`developer/analytics/v1_beta/rate_limit`) | `buy.browse` 5.000 y `buy.browse.item.bulk` 5.000, por separado |
| Árbol EBAY_US (`commerce/taxonomy/v1/category_tree/0`) | 34 raíces y 15.111 hojas. En las 19 raíces elegidas: 281 categorías de nivel 2, 2.054 de nivel 3 y 9.234 hojas |
| Publicaciones nuevas, a precio fijo y con envío al 33166 (Miami) | Tarjetas gráficas 29 mil, portátiles 53 mil, celulares 153 mil, consolas 55 mil, televisores 9 mil, audífonos 192 mil, tenis 2,0 millones |
| Con `epid` (modelo de eBay), en muestras de 200 | 13-56 %, en promedio ~35 % |
| Con costo de envío conocido | 67-99 % (el resto es envío calculado) |
| Ubicadas en EE. UU. | 77-100 % |
| Con precio tachado (`marketingPrice`) | 5-50 % |
| Orden por defecto, misma consulta leída dos veces seguidas | comparte 182/200 y 199/200; la página 2 repite 5 y 2 |
| Tope de paginación | `offset` ≤ 10.000 por consulta |
| BD de producción (`/estado`) | 290 MB de 500 |

**Consecuencia:** barrer todo eBay no cabe en la cuota (unas 720 mil lecturas al día con 3.600 búsquedas de 200) ni en la BD. El barrido sigue un subconjunto amplio y fijo: las publicaciones mejor posicionadas de cada categoría de nivel 3.

## 1. Arquitectura

- Adaptador nuevo `sources/ebay.py` (`EbaySource`, código `ebay_us`): implementa `CrawlSource` (v1 más catálogo) como `sources/bestbuy.py`. Funciones de parseo puras, con modelos pydantic estrictos y `parse_float=Decimal`.
- Sin cambios de capas: el dominio gana una función pura y el rastreo gana un paso. La regla `app → sources → infra → domain` y los contratos de import-linter no cambian.
- El token OAuth lo pide el adaptador por `ctx.http` (con ritmo, presupuesto y registro de bloqueos de siempre). `CredentialStore.bearer("ebay")` devuelve `SecretStr` con el `Basic` ya armado (`base64(app_id:cert_id)`), leído de `DISCOUNTZ_EBAY_APP_ID` y `DISCOUNTZ_EBAY_CERT_ID`.
- El token vive en memoria del proceso. Se pide la primera vez y se renueva cuando faltan menos de 5 min para que venza según `ctx.now`, o una vez tras un 401. Nunca va a logs, a la BD ni a `repr`.

## 2. Lectura de la API

Todas las peticiones llevan `X-EBAY-C-MARKETPLACE-ID: EBAY_US` y `X-EBAY-C-ENDUSERCTX: contextualLocation=country=US,zip=33166` (B1). El código postal va en `config/discountz.toml` (`[ebay] ship_to_zip`).

| Uso | Recurso | Cuota |
|---|---|---|
| Barrido y `/buscar` | `GET /buy/browse/v1/item_summary/search` | `buy.browse` |
| `/seguir`, capa caliente, `fetch_catalog_items`, `fetch_catalog_listings` y `fetch_card_facts` | `GET /buy/browse/v1/item/?item_ids=` (hasta 20) | `buy.browse.item.bulk` |
| Confirmación independiente de la v1 (`confirm`) | `GET /buy/browse/v1/item/{item_id}` | `buy.browse` |
| Árbol de categorías (una vez por corrida de `crawl`) | `GET /commerce/taxonomy/v1/category_tree/0` | aparte |

Filtro común de búsqueda: `conditions:{NEW},buyingOptions:{FIXED_PRICE},deliveryCountry:US,price:[15..],priceCurrency:USD`. Después del filtro de la API, el adaptador descarta en el parseo:

- `seller.feedbackPercentage` < 98 o `seller.feedbackScore` < 100 (B4);
- publicaciones con variantes (`itemGroupHref`) en el barrido; en `/seguir` se sigue la variante del enlace;
- `lotSize` > 1 cuando la ficha lo trae;
- publicaciones sin costo de envío conocido en `shippingOptions` (envío calculado).

**Precio observado** = `price` + costo del envío más barato a Miami, en centavos de USD. Así, una bajada de precio compensada con un envío más caro no da alerta. El `marketingPrice` nunca se usa como «antes», igual que el `ListPrice` de VTEX.

**Ids:** `external_id` es el `itemId` de la Browse API (`v1|<legacy>|<variación>`, `^v1\|\d{9,15}\|\d{1,15}$`). `parse_url` acepta `www.ebay.com/itm/<legacy>` y `/itm/<slug>/<legacy>`, con `?var=<id>` opcional, y lo traduce sin red a `v1|<legacy>|<var o 0>`.

**Vendedor:** las publicaciones que pasan el filtro de B4 cuentan como `SellerClass.FIRST_PARTY`, como anticipaba la fila SP-4 de la v1 (`seller_class` según `feedbackScore`). Así pasan la regla `allow_marketplace = false` de Tecnología. En `/seguir`, un vendedor bajo el umbral da `MARKETPLACE` y queda sujeto a esa regla (G14).

**Bloqueos y errores:** 401 ⇒ se renueva el token una vez y, si persiste, `CredentialUnavailable("ebay_us", "bad_key")`. 429 o `errorId` 2001 (cuota) ⇒ `Unavailable("ebay_us", "quota")`, que no abre el breaker. Lo demás pasa por `detect_block_common`.

## 3. Barrido (`crawl --source ebay_us`)

- **Particiones:** las categorías de nivel 3 de las 19 raíces (las de nivel 2 sin hijos cuentan como partición), leídas del árbol al empezar la corrida y ordenadas por id. Hoy son ~2.054. Las raíces excluidas (`[ebay] excluded_roots`) son: 1, 99, 237, 260, 316, 550, 870, 1305, 3252, 10542, 11116, 20081, 45100, 64482 y 172008.
- **Página:** `limit = [ebay] items_per_partition` (75 por defecto) en el orden por defecto de eBay, solo `offset = 0`. Cada partición es una página, así que `has_more = false`. 2.054 × 75 ≈ 154 mil publicaciones, que es el tope de B4.
- **Ritmo:** `[crawl.ebay_us]` con `max_per_run = 150` y `max_per_24h = 3600` búsquedas. Una vuelta completa tarda ~14 h y deja ~1.400 búsquedas al día para `/buscar` y `confirm`.
- **Capa caliente:** relee por `item_ids` (cuota aparte) lo que cambió de precio en 7 días, hasta 200 lotes de 20 por corrida (4.000 publicaciones por hora).
- **Tope de la BD:** si `catalog_item` de `ebay_us` pasa de `[ebay] max_items` (150.000), la corrida no inserta publicaciones nuevas. Solo actualiza las que ya conoce. Con la BD en ≥ 450 MB, `ebay_us` se pausa igual que Éxito y Carulla.
- **Tema:** reglas `ebay_us:` en `config/topics.toml` sobre la ruta de categorías de eBay (`/consumer electronics/tv, video & home audio/…`). Las 19 raíces quedan asignadas: ninguna cae en Otros sin querer.

## 4. Detección

### 4.1 Bajada de la misma publicación

Es `domain/catalog.observe` sin cambios: «antes» estable 24 h, 2 lecturas al mismo precio, umbral `t_bp` del tema y confirmación releyendo la ficha (±1 %, en stock y vendedor).

### 4.2 Precio bajo frente al mismo modelo (B3)

- Función pura nueva `domain/catalog.model_candidate(price, peers, topic) -> CatalogCandidate | None`, con `peers = (mediana_minor, n)` de las demás publicaciones del mismo modelo.
- Hay candidato si `n ≥ 5` y `price ≤ mediana × (1 − t_bp)`. El descuento es `drop_bp = (mediana − price) / mediana` y la alerta es de clase nueva `AlertKind.MODEL_LOW`. Si además `drop_bp ≥ g_bp` del tema, la alerta va como `PRICE_ERROR` al tema de errores, igual que en el rastreo.
- **Pares:** publicaciones de `ebay_us` con el mismo `model_key` vistas en 7 días, excluida la propia, con una consulta por página para todos los modelos de la página (`percentile_disc(0.5)` agrupado por `model_key`).
- `model_key` es el `epid` (texto `^\d{4,15}$`). En la búsqueda, las publicaciones sin `epid` no tienen modelo y solo usan §4.1.
- Una publicación no alerta por §4.2 dos veces sin bajar más su precio: aplica el mismo `last_alert_at`/`last_alert_minor` de §4.1. Si en la misma lectura salen §4.1 y §4.2, gana la de mayor `drop_bp`.
- **Duplicados:** otra publicación del mismo `model_key` que ya alertó en 6 h a ±1 % descarta la nueva. Es la regla del EAN aplicada dentro de la misma fuente.
- La confirmación es la de §4.1. Además, la ficha debe seguir trayendo el mismo `epid`.

## 5. Tarjeta y mensaje

Es la tarjeta del rastreo con estos cambios:

- La línea de tienda dice «eBay · vendedor 99,6 % (12.345)».
- El precio grande es el observado (precio + envío). Debajo, «incluye envío a Miami USD 12,99» cuando el envío no es gratis.
- En §4.1 la referencia es «Antes USD X»; en §4.2, «Mediana de N publicaciones del mismo modelo USD X».
- Pie «🇺🇸 Compra por casillero» y botón «Ver en eBay» con `itemWebUrl` sin parámetros de seguimiento. Las fotos vienen de `i.ebayimg.com`, que entra en `image_hosts`.

## 6. Datos (migración `0005_ebay`, `SCHEMA_VERSION = 5`)

- `INSERT` de la fuente `ebay_us` (`kind = 'store'`) en `source`, como las migraciones anteriores.
- `catalog_item.model_key text NULL CHECK (model_key ~ '^[0-9]{4,15}$')` e índice parcial `(source, model_key, last_seen_at) WHERE model_key IS NOT NULL`.
- `alert.kind` admite `model_low`.
- El `down` deshace los tres cambios (borra antes las filas `ebay_us` y `model_low`).
- `CatalogObservation` gana `model_key: str | None = None`, con valor por defecto para no tocar las demás fuentes.

## 7. Configuración y puesta en marcha

- `Settings`: `ebay_app_id: str | None` y `ebay_cert_id: SecretStr | None`. `check-config` avisa si falta uno de los dos.
- `cycle.yml` y `crawl.yml` pasan `DISCOUNTZ_EBAY_APP_ID` y `DISCOUNTZ_EBAY_CERT_ID` desde los secretos `EBAY_APP_ID` y `EBAY_CERT_ID`, que ya existen. La matriz de `crawl` gana `ebay_us`.
- `[sources.ebay_us]` en `config/discountz.toml` con un presupuesto para `/seguir` y `/buscar`, y `max_batch = 20`.
- Se borra `.github/workflows/ebay-probe.yml`.
- Lo que hace David tras fusionar: correr `migrate` con `to = head`. Nada más: los secretos ya están y el job entra solo a la matriz.

## 8. Errores y salud

- El breaker, la salud y `/estado` son los de siempre. Una cuota agotada (`Unavailable`) no cuenta como bloqueo.
- Las fallas del árbol de categorías dan `ParseError("taxonomy")`, y la corrida termina sin tocar el cursor.
- Logs con la lista blanca: código de fuente, partición (id de categoría), conteos y códigos de error. Nunca token, query ni cuerpo.

## 9. Pruebas

- **Fixtures reales**, grabadas desde Actions con un paso temporal del workflow de prueba: una búsqueda por categoría, una búsqueda por texto, `getItems` con 3 ids (uno con variación), `getItem`, un 401, un 429 y un recorte del árbol de categorías. El token se enmascara y no se graba.
- **Unitarias del adaptador:** `parse_url` (con y sin `?var=`, rutas inválidas, hosts ajenos), parseo (filtro de vendedor, variantes, envío calculado, precio + envío, `epid`) y errores (401 con renovación única, cuota, JSON roto).
- **Dominio:** `model_candidate` con hypothesis (n < 5, mediana igual al precio, límites de `t_bp` y `g_bp`, montos grandes sin desbordes).
- **Integración (Postgres):** migración `0005` arriba y abajo, mediana por modelo excluyendo la propia publicación, duplicado por `model_key` y tope de `max_items`.
- **e2e:** una corrida de `crawl` con fixtures que da una alerta §4.1 y una §4.2 con sus tarjetas, y otra que no alerta por vendedor bajo el umbral.

## 10. Riesgos

- **Términos de eBay:** según una fuente secundaria, el acuerdo de la API pide que lo mostrado no tenga más de 6 h de atraso y prohíbe entrenar IA con los datos. Toda alerta relee la ficha antes de enviarse, solo se guardan números y nada se redistribuye. Se revisa el texto oficial antes de activar el job.
- **Cobertura:** el barrido sigue las 75 mejor posicionadas por categoría de nivel 3, no todo eBay. Una publicación nueva que sale barata y no entra en ese corte no se ve, salvo por `/buscar` o `/seguir`.
- **Modelo mal asignado** por el vendedor: el umbral `t_bp` y n ≥ 5 lo amortiguan, y la confirmación exige el mismo `epid`.
- **Cuota:** con 5.000 diarias no hay margen para más particiones. El presupuesto de 24 h la protege.
- **BD:** ~70 MB estimados con 150 mil publicaciones (de 290 a ~360 MB).

## 11. Fuera de alcance

- Envío directo a Colombia, subastas, artículos usados o reacondicionados y variantes en el barrido.
- Búsquedas vigiladas con precio máximo (la opción que no se eligió).
- La Deal API de eBay (exige acceso especial).
