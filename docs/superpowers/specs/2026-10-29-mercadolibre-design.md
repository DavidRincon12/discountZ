# discountZ: Mercado Libre por su catálogo, sembrado con los EAN de las tiendas (spec de diseño)

- Fecha: 2026-10-29
- Estado: en revisión de David (las referencias salen de la tarjeta «¿De dónde saco las referencias de Mercado Libre?», 2026-10-29)
- Autor: David Santiago Rincón Bautista
- Base: spec del rastreo (`2026-10-14-rastreo-catalogo-design.md`) y spec de eBay (`2026-10-18-ebay-design.md`). Donde este spec no dice nada, mandan ellos.

## 0. Contexto y decisiones

### 0.1 Qué cambia

Mercado Libre Colombia (`mercadolibre.com.co`, COP) entra como fuente rastreada. Su búsqueda general responde 403 aun con el token de la app, y el catálogo no se puede recorrer por categorías: la búsqueda de productos exige palabras o un código y se corta en 300 resultados. Lo que sí responde con el token es cada producto del catálogo con sus vendedores y precios. Por eso el descubrimiento va por EAN: los que publican las tiendas colombianas que ya rastreamos se buscan en Mercado Libre, y cada producto hallado se relee a diario y se compara con su propio histórico. Además, `/seguir` acepta enlaces de producto de Mercado Libre.

### 0.2 Decisiones

| # | Decisión |
|---|---|
| M1 | David creó la app de desarrollador, de solo lectura («Publicación y sincronización» y «Métricas del negocio»), y subió `MELI_CLIENT_ID` y `MELI_CLIENT_SECRET` como secretos del repo (2026-10-29). El token es de aplicación (`client_credentials`, 6 h) y vive en memoria, como el de eBay. Los términos permiten usar la API «únicamente para facilitar el uso del Sitio» y solo con copias intermedias: el riesgo se parece al de eBay (ADR 0008) y el peor caso es que suspendan la app. Si pasa, la fuente se apaga con `/fuente off mercadolibre`. Nada del rastreo toca la web, que desde Actions responde 302 a una verificación de cuenta. |
| M2 | Referencias: los EAN de todos los temas en tarjeta (tarjeta de decisión del 2026-10-29). Salen de `catalog_item` de las tiendas en COP que publican EAN (hoy Éxito, Jumbo, Pepe Ganga, Panamericana y Alkosto), con precio ≥ `min_price_cop`, porque lo más barato no puede alertar. Se buscan del más caro al más barato: con el mismo presupuesto, primero lo que más puede ahorrar. |
| M3 | Vendedor elegible: producto nuevo, sin envío internacional, en COP, y tienda oficial o MercadoLíder Platinum o Gold con reputación verde (`5_green`). El precio es el del vendedor elegible más barato; `original_price` nunca se usa, porque el descuento sale del histórico propio. La tienda oficial cuenta como `FIRST_PARTY` y el MercadoLíder como `MARKETPLACE`, así que mandan las reglas de siempre: en tecnología (`allow_marketplace = false`) solo alertan las tiendas oficiales, y el posible error de precio es solo de tienda oficial. |
| M4 | Tema: el de la semilla, es decir, el que la tienda colombiana le dio a ese EAN, guardado en `catalog_item.topic`. No se leen las categorías de Mercado Libre: mapear su árbol costaría peticiones y reglas nuevas en `topics.toml`. |
| M5 | Enlace de la tarjeta: `https://www.mercadolibre.com.co/p/<id>`. David lo abrió en su celular el 2026-10-29 («Si llega el taladro»). |
| M6 | Sin `/buscar`: la búsqueda general da 403 y la del catálogo no trae precios (serían 1 + N peticiones por página). `/seguir` solo acepta enlaces de producto (`/p/MCO…`); los de `articulo.` son publicaciones, y su lectura (`/items/{id}`) da 403. |

### 0.3 Mediciones (2026-10-29, Actions, con un workflow temporal ya borrado)

| Medida | Valor |
|---|---|
| Token | `POST /oauth/token` con `client_credentials`: 200, vence en 21.600 s. Ninguna respuesta trae cabeceras de cuota |
| Responde con el token | `/products/search`, `/products/{id}`, `/products/{id}/items`, `/users/{id}`, `/categories`, `/sites/MCO`, `/trends/MCO` |
| Cerrado aun con permisos | `/sites/MCO/search` (403 `forbidden`); `/items/{id}`, `/items?ids=`, `/items/{id}/sale_price` y `/items/{id}/prices` (403 `access_denied`); `/highlights` (403 de PolicyAgent) |
| Búsqueda de productos | exige `q`, `product_identifier`, `parent_product_id` o `attributes`; `domain_id` solo filtra y `offset` ≥ 300 da 400 |
| Por palabras | con vendedores: 5 de 50 taladros, 12 de 50 televisores y 5 de 50 portátiles |
| Por EAN | 11 de 18 EAN de electrónica de nuestras fixtures (Éxito, Jumbo, Olímpica y Panamericana) dieron un producto, y 10 de ellos tenían vendedores. Se probó el EAN tal cual y, si no acertaba, sin los ceros de la izquierda; no quedó registrado qué forma acertó |
| Vendedores (`/products/{id}/items`) | `price`, `original_price`, `currency_id`, `condition`, `official_store_id`, `seller_id`, `international_delivery_mode`, `shipping{free_shipping, logistic_type, mode}`, `tags` y `deal_ids`. El taladro DeWalt MCO18924972 tiene 13, y 3 de los 4 más baratos son tiendas oficiales. Sin vendedores, 404 «No winners found». `buy_box_winner` siempre viene `null` |
| Producto (`/products/{id}`) | `name`, `pictures[].url` (`https://http2.mlstatic.com/D_….jpg`, 200 desde Actions), `attributes` y `domain_id`; `permalink` viene vacío |
| Vendedor (`/users/{id}`) | `seller_reputation.level_id` (`5_green`) y `seller_reputation.power_seller_status` (`platinum`) |
| Web | `/p/MCO…` y `articulo.…` responden 302 a `account-verification` desde Actions |

## 1. Arquitectura

### 1.1 Adaptador

`sources/mercadolibre.py` (`MercadoLibreSource`, código `mercadolibre`): parseo puro con modelos pydantic estrictos y `json.loads(..., parse_float=Decimal)`. Las capas no cambian.

- `SourceDescriptor`: `kind="store"`, `currency=COP`, `hosts={"www.mercadolibre.com.co"}`, `image_hosts={"http2.mlstatic.com"}`, `capabilities={DETAIL, CONFIRM, CATALOG}`, `max_batch=1`, `external_id_re=^MCO\d{1,12}$` y `auth="oauth_client"`.
- **Token:** `POST https://api.mercadolibre.com/oauth/token` con `grant_type=client_credentials`, el id de `credentials.public_id("mercadolibre")` y el secreto de `credentials.bearer("mercadolibre")`. Vive en memoria hasta 10 min antes de vencer; un 401 lo renueva una vez y, si sigue, la fuente lanza `CredentialUnavailable("bad_key")`, como eBay. Nunca va a logs ni a fixtures.
- **`find_by_ean(ctx, ean)`** (contrato nuevo, §1.2): `GET /products/search?status=active&site_id=MCO&product_identifier=<ean>`; si no hay resultados y el EAN empieza por 0, se busca una vez más sin los ceros de la izquierda. Devuelve el `id` del primer resultado o `None`. Un 400 es `ParseError`: el rastreo lo cuenta como error de parseo y anota el EAN como buscado (§1.3).
- **Precio** (`fetch_catalog_items`, una petición por producto): `GET /products/{id}/items`. De los vendedores con `condition = new`, `international_delivery_mode = none` y `currency_id = COP`, de menor a mayor precio, gana el primero elegible. Una tienda oficial (`official_store_id`) vale sin más peticiones; si no lo es, se pide `GET /users/{seller_id}` y vale si es Platinum o Gold con `5_green`. Se hacen a lo sumo 3 de esas consultas por producto, con caché en memoria del proceso; pasado el tope, solo cuentan las tiendas oficiales. Un vendedor que no valida se descarta solo; si no valida la respuesta entera, `ParseError`. Con un 404 o sin elegibles, el producto no se ve en esa lectura. La observación lleva el precio entero en COP, `category_path=""`, la clase de M3 y ningún EAN (lo pone el rastreo).
- **Confirmación** (`fetch_catalog_listings`, y `fetch_listings` para `/seguir`): los vendedores con la misma regla más `GET /products/{id}` para el nombre. La `Offer` lleva `list_price=None`, `in_stock=True`, `unit_key = pack_unit_key(nombre)` o `"un|1"`, `extraction=API_JSON` y `url_path="/p/<id>"`. Sin elegibles, `ListingGone`. `confirm` relee igual.
- **Tarjeta** (`fetch_card_facts`): nombre, primera foto (solo de `http2.mlstatic.com`), el enlace de M5 y la nota «Tienda oficial» o «MercadoLíder Platinum» (o Gold), con «· Envío gratis» cuando aplica. Reutiliza en memoria lo que leyó la confirmación.
- `parse_url`: `…/p/MCO<dígitos>`, con o sin slug antes, da `ListingRef("mercadolibre", "MCO…")` sin red. `search`, `catalog_partitions`, `fetch_catalog_page` y `fetch_changed` devuelven vacío.
- **Bloqueos:** un 403 de PolicyAgent o `forbidden` en una ruta que respondía es `HardBlock` y abre el breaker; así se vería que Mercado Libre cerró también los vendedores, como cerró la búsqueda. Un 429, tras los reintentos del cliente HTTP, es `Unavailable`, como en las demás tiendas.

### 1.2 Contrato

`sources/base.py` suma el protocolo `EanCatalogSource`, que es `CrawlSource` más `find_by_ean(ctx, ean) -> str | None`. El rastreo solo trata distinto a las fuentes que lo implementan; las demás no cambian.

### 1.3 Rastreo (`app/crawl.py`)

En una `EanCatalogSource`, la corrida cambia la capa caliente y la vuelta completa por dos capas:

1. **Relectura de los seguidos** (hasta `hot_share_bp` de la corrida): `db.catalog_refresh_ids` da los productos de la fuente que no se pidieron en las últimas 6 h, primero el pedido hace más tiempo (`coalesce(checked_at, last_seen_at)`), sin los de temas `off`, cada uno con su EAN y su tema guardados, que el rastreo pone en la observación. Cada lectura terminada marca `checked_at`, haya o no precio. Así, un producto sin vendedores elegibles no se relee en cada corrida y vence con la retención de siempre (30 días sin verse). Una lectura con `ParseError` no se marca y se repite en la próxima corrida.
2. **Semillas:** `db.ean_seeds` da los EAN de M2 que la fuente no sigue (ningún `catalog_item` suyo con ese EAN) y que no buscó en los últimos 30 días (`ean_lookup`), del más caro al más barato. Si el EAN está en varias tiendas, mandan el precio y el tema de la fila vista más recientemente. Un EAN que no es GTIN válido (§1.4) se anota como buscado sin pedir nada. Los demás pasan por `find_by_ean` y se anota el resultado; un `ParseError` cuenta para el breaker y deja el EAN anotado sin producto, para que no se repita en cada corrida. Si el producto es nuevo para la fuente, se lee su precio y entra con el EAN y el tema de la semilla. Las semillas paran con la BD en ≥ 450 MB (`discovery_paused`) o al llegar a `[mercadolibre] max_items` productos seguidos.
3. Lo que las semillas no gastan vuelve a la relectura.

`process` recibe un mapa opcional de producto a tema que manda sobre `assign_topic`; solo lo usa esta fuente. Las lecturas se procesan en lotes de 20, para no abrir una transacción por producto. El resto es el rastreo de siempre: estado, candidato, duplicados por EAN entre tiendas (6 h), confirmación, tope diario y tarjeta. Un log `crawl_seeds` cuenta por corrida los EAN buscados y los productos hallados.

### 1.4 Dominio

`domain/gtin.py`, puro: `gtin_ok(code)` acepta códigos de 8, 12, 13 o 14 dígitos (los de 14, solo con un 0 adelante) con el dígito de control GS1 correcto y fuera de la circulación restringida, que son los códigos internos y de peso variable de las tiendas: 0 o 2 al inicio en los de 8 dígitos, y 02, 04 o 20-29 en la forma de 13.

### 1.5 Datos (migración `0017_mercadolibre`, `SCHEMA_VERSION = 17`)

- `INSERT` de la fuente (`store`, COP).
- Tabla `ean_lookup (source text REFERENCES source(code) ON DELETE CASCADE, ean text, checked_at timestamptz NOT NULL, external_id text NULL, PRIMARY KEY (source, ean))`, con el mismo `CHECK` de `catalog_item.ean`: una fila por EAN buscado, con el producto hallado o sin él. El mantenimiento borra las de más de 30 días, y esa es la espera antes de volver a buscar un EAN.
- `catalog_item.checked_at timestamptz`, nula y sin valor por defecto, así que no reescribe la tabla.
- Permisos de `discountz_app` sobre `ean_lookup`. La bajada borra la tabla, la columna y lo de la fuente, como `0016`.
- Tamaño: a lo sumo 12 mil filas de `catalog_item` (~5 MB) y una fila de `ean_lookup` por EAN candidato (se estiman ≤ 100 mil, ~12 MB).

### 1.6 Credenciales y configuración

- `Settings.meli_client_id` y `Settings.meli_client_secret`, los dos o ninguno, desde `DISCOUNTZ_MELI_CLIENT_ID` y `DISCOUNTZ_MELI_CLIENT_SECRET`. `crawl.yml` y `cycle.yml` los toman de los secretos `MELI_CLIENT_ID` y `MELI_CLIENT_SECRET`; el ciclo los necesita para `/seguir` y para el botón «Seguir» de las tarjetas. `EnvCredentials` entrega el id por `public_id` y el secreto por `bearer`, y su `repr` no los muestra.
- `crawl.yml`: `mercadolibre` entra en la matriz. En `[[events]]`, Hot Sale y Black Days suman `mercadolibre`.
- El PR actualiza `CLAUDE.md`, el README (secretos) y `docs/architecture.md`. La primera tarea del plan escribe el ADR 0018 con las decisiones M1 a M6.

## 2. Ritmo y presupuesto

Costo en peticiones: buscar un EAN cuesta 1 (2 si empieza por 0 y no acierta); leer un precio, 1 más a lo sumo 3 de vendedor, con caché; confirmar un candidato, 2; el token, 1 por proceso (dura 6 h y la corrida, 45 min).

- `[crawl.mercadolibre]`: `min_pause_s = 0.5`, `jitter_s = 0.5`, `max_per_run = 2000`, `max_per_24h = 20000` y `hot_share_bp = 7000` para la relectura. Son unas 830 peticiones por hora, de las que unas 580 pueden ir a releer.
- `[mercadolibre]`: `max_items = 12000`, que es lo que alcanza a releerse una vez al día con ese presupuesto.
- `[sources.mercadolibre]`, para `/seguir` y la confirmación del ciclo, como eBay: `min_pause_s = 1`, `jitter_s = 1`, `max_per_run = 10`, `max_per_24h = 1000`, `watch_interval_min = 120`, `search_interval_min = 1440` y `max_batch = 1`.
- Mercado Libre no publica cuota para estas rutas. Se arranca en 20 mil peticiones al día y se sube solo si pasa una semana sin 429 y con el tope de productos lleno (ADR 0018, «Cuándo revisar»). Al principio casi todo va a semillas: si los EAN candidatos son unos 100 mil, se buscan en alrededor de una semana.
- La consulta de semillas corre una vez por corrida sobre el catálogo de las demás tiendas. Si en producción tarda más de 2 s, se le agrega un índice parcial.

## 3. Pruebas

- Fixtures reales en `tests/fixtures/mercadolibre/`, grabadas con un workflow temporal, porque la nube no tiene las claves (la respuesta del token no se guarda): búsqueda por EAN con y sin resultado, los vendedores del taladro (13, con tiendas oficiales), un producto sin vendedores (404), la ficha del producto y un vendedor Platinum. Las que no aparezcan reales van sintéticas y marcadas con `"nota"`: vendedor de usado, con envío internacional o con reputación roja.
- `tests/sources/test_mercadolibre.py`: la regla de vendedor (tienda oficial sin consultar, MercadoLíder verde, rojo o Silver fuera, usado e internacional fuera, tope de 3 consultas y caché), 404 sin observación, token reutilizado y renovado tras un 401, búsqueda sin ceros, 400 como `ParseError`, 403 de PolicyAgent como `HardBlock`, `parse_url`, y nombre, foto y nota de la tarjeta.
- `tests/unit/test_gtin.py`: dígitos de control válidos e inválidos, prefijos restringidos y una propiedad con hypothesis (todo código con su dígito calculado pasa).
- Integración: `0017` sube y baja; `ean_seeds` (solo temas en tarjeta, COP y precio mínimo; sin los seguidos ni los buscados; del más caro al más barato; tema de la fila más reciente); `catalog_refresh_ids` (`checked_at`, 6 h y temas `off`); un rastreo de `mercadolibre` con HTTP falso, donde la semilla deja el producto con su EAN y tema, la relectura marca `checked_at` y, tras el calentamiento, una bajada sale en tarjeta; semillas en pausa con 450 MB y con el tope; y el mantenimiento borra lo viejo de `ean_lookup`.
- Configuración: la matriz de `crawl.yml` (prueba existente), los eventos y las credenciales de a dos.

## 4. Fuera de alcance

`/buscar` en Mercado Libre; enlaces `articulo.` y `meli.la` (sin medir); intereses (🎯) sobre Mercado Libre, porque la lectura de precio no trae el título y pedirlo costaría una petición más por producto; las categorías propias de Mercado Libre; semillas de eBay (USD) o por palabras; las promociones de Mercado Libre (`deal_ids`, descuentos con Mercado Pago) y el costo de envío.
