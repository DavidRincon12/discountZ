# discountZ: Jumbo y Pepe Ganga por VTEX (spec de diseño)

- Fecha: 2026-10-20
- Estado: propuesto, pendiente de la revisión de David
- Autor: David Santiago Rincón Bautista
- Base: spec del rastreo (`2026-10-14-rastreo-catalogo-design.md`, ADR 0004) y spec de la v1. Donde este spec no dice nada, mandan ellos.

## 0. Contexto y decisiones

### 0.1 Qué cambia

Jumbo (`jumbo`) y Pepe Ganga (`pepeganga`) entran como tiendas colombianas (COP) con `/seguir`, `/buscar`, confirmación, canario y rastreo de catálogo (`crawl --source jumbo|pepeganga`). Las dos están en VTEX: se reutiliza `sources/vtex.py` tal cual, con dos `VtexStore` nuevos. No hay código de adaptador nuevo.

### 0.2 Decisiones de David (2026-10-20)

| # | Decisión |
|---|---|
| J1 | Sumar las dos (tarjeta de decisión del hilo «auditoría»). |
| J2 | Quedan fuera Droguerías Colsubsidio (su robots.txt bloquea `/api/` y `?O=`), Decathlon (no es VTEX, 403 desde Actions, robots bloquea `/api/*`) y Metro (no responde ni desde Actions). |

Sigue vigente: no se sortea ningún CAPTCHA ni detección de bots; si una tienda bloquea, se apaga con `/fuente off <código>`.

### 0.3 Mediciones (2026-10-20, desde Actions; la red de la sesión no llega a estas tiendas)

| Medida | Jumbo | Pepe Ganga |
|---|---|---|
| Host y API | `www.jumbocolombia.com`, `https://www.jumbocolombia.com/api` (`tiendasjumbo.co` ya no responde) | `www.pepeganga.com`, `https://www.pepeganga.com/api` |
| Cuenta VTEX (fotos) | `jumbocolombiaio` (`jumbocolombiaio.vteximg.com.br` y `.vtexassets.com`) | `pepeganga` |
| Con stock (`isAvailablePerSalesChannel_1:1`) | 45.445 de 46.378 | 12.829 de 12.842 |
| Peso por 50 productos | ~1,7 MB | ~5,9 MB (bajo el tope de 24 MB) |
| Canal de venta | solo `sc=1` (`sc=2` vacío, `sc=3` 400): un precio nacional | solo `sc=1` |
| Vendedores (muestra de 50) | todo «Cencosud - Colombia» | «Pepe Ganga» y algunos de marketplace |
| `ListPrice > Price` (muestra de 50) | 11, ninguno ≥ 2× | 33, 22 ≥ 2×: inflado, como en Éxito |
| Términos | sin cláusula sobre robots (`/institucional/terminos-y-condiciones`) | la página solo trata cambios y devoluciones |
| robots.txt | bloquea `/busca*` y parámetros sobre `/`, no `/api/` | bloquea `/busca/` y `/buscapagina`, no `/api/`; permite `/*search?fq=` |
| Raíces del árbol | 24 (Supermercado 17.110, Hogar y Decoración 8.203, Herramientas 4.329, Ropa 2.987…) | 21 (Juguetería 3.338, Bebés 2.324, Hogar 2.096, Moda 1.031…) |

Una vuelta completa: ~910 peticiones de 50 en Jumbo y ~260 en Pepe Ganga.

## 1. Arquitectura

Sin piezas nuevas. Lo que cambia es dato:

- `sources/vtex.py`: `STORES["jumbo"]` y `STORES["pepeganga"]` con host, `api_base` (`…/api`, sin `/io`) y cuenta. El registro ya crea un `VtexSource` por cada entrada de `STORES`.
- Migración `0008_jumbo_pepeganga` (esquema 8): inserta las dos filas de `source` (`'store','COP',true,NULL`); el `down` borra sus filas de watch, search, listing y catalog_item y luego las fuentes, como la `0007`. `PAUSED_AT_450` (`app/crawl.py`, tiendas grandes que pausan su rastreo con la BD en ≥ 450 MB) suma `jumbo` (45 mil productos, más que Alkosto); Pepe Ganga queda fuera, como Olímpica y Panamericana.
- `config/discountz.toml`: `[sources.jumbo]` y `[sources.pepeganga]` con los valores de Olímpica (grupo propio, 2 s, 20 por corrida, 800 al día); `[crawl.jumbo]` y `[crawl.pepeganga]` con los de Olímpica (2 s, 250 por corrida, 6.000 al día: Jumbo da ~6 vueltas al día y Pepe Ganga ~1 por hora). Ambas en los eventos CO (Black Days, Cyber Lunes, Hot Sale).
- `config/canaries.toml`: un producto genérico por tienda con rango ±60 %, tomado de las fixtures.
- `config/topics.toml`: reglas para las raíces de las dos tiendas, con el mismo criterio de hashtags que las demás; lo que no tenga tema va a `intentional_otros` (Bolsa, Tarjeta Regalo, Logística, Gift Card…). `tests/unit/test_topics.py` las cubre.
- `app/render.py` (nombre visible: «Jumbo», «Pepe Ganga») e `infra/card.py` (color de cada tienda).
- `.github/workflows/crawl.yml`: la matriz suma `jumbo` y `pepeganga`.

## 2. Fixtures

Se graban desde Actions con un workflow temporal que se borra en el mismo PR (como eBay): árbol de categorías, una página de catálogo de 50 con stock, una ficha por slug y una búsqueda por texto de cada tienda. `record-fixture` no sigue redirecciones: se graba la URL final.

## 3. Pruebas

- `tests/sources/test_vtex.py`: parseo de la ficha y del catálogo de cada tienda (precio, moneda, fotos en el host de su cuenta, URL canónica).
- `tests/integration/test_migrations.py` y `test_channels_db.py`: esquema 8 y fuentes sembradas.
- `tests/unit/test_config.py`: presupuestos, canarios y eventos de las dos.
- `tests/unit/test_topics.py`: cada raíz de primer nivel tiene tema o está en `intentional_otros`.

## 4. Documentación

README (lista de tiendas), `CLAUDE.md` (una línea de gotcha: `www.jumbocolombia.com`, solo `sc=1`), `docs/architecture.md` (migración 0008) y `docs/estado.md`. En el mismo PR va el ADR 0010 con el resultado del spike del carril «más descuento primero» (descartado: ordena por el `ListPrice` inflado).

## 5. Riesgos

- Supermercado es el 38 % de Jumbo: más alertas de mercado; se gobierna con los umbrales del tema, como en Éxito y Carulla.
- `ListPrice` inflado en Pepe Ganga: no se usa (regla vigente de VTEX); las alertas salen del histórico propio, que tarda ≥ 24 h en llenarse.
- Términos sin cláusula de robots, pero tampoco permiso expreso: si alguna bloquea o reclama, se apaga con `/fuente off`.
