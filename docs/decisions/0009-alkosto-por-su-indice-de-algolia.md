# 0009. Rastrear Alkosto por su índice público de Algolia

- Fecha: 2026-10-20
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-20-alkosto-design.md` (§0.2 K1-K3, §1 a §3)

## Contexto
David quiere Alkosto como tienda colombiana más, gratis y con las mismas alertas que Éxito. Su web busca en Algolia (app `QX5IPS1B1Q`, índice `alkostoIndexAlgoliaPRD`) con una clave de solo búsqueda que viene en el HTML de la portada. Medido el 2026-10-20: 14.024 productos con stock; `lowestprice_double` es el precio de la ficha y `pricevalue_cop_double` es un «antes» inflado (un TV marca −57 %); `objectID` suele ser el EAN-13; `getObjects` lee hasta 100 fichas por petición con esa clave; cada consulta devuelve como mucho 1.000 resultados (~1 MB con los campos acotados).

## Decisión
- Fuente `alkosto` (`CrawlSource`, COP, `FIRST_PARTY`) en `sources/alkosto.py`. Todas las lecturas son `POST` a Algolia; la clave se lee de la portada una vez por proceso y vive solo en memoria. Un 403 de clave inválida relee la portada una vez; si sigue, es `HardBlock`. Un 429 es `Unavailable("rate_limited")`.
- Precio = `lowestprice_double`; en stock = `instockflag_boolean` y `stocklevel_int > 0`. El «antes» de la tienda nunca se usa: el descuento sale del histórico propio, como en el resto del rastreo.
- Barrido por franjas de precio (`numericFilters` sobre `lowestprice_double`), empezando en `1-999999999`; una franja con más de 1.000 resultados se parte en el punto medio geométrico, como las bandas de VTEX. Una franja de un solo precio que pasa de 1.000 se lee truncada y se registra.
- `/seguir`, la capa caliente, la confirmación y las tarjetas leen por `getObjects` en lotes de 100. Ritmo: 2 s entre peticiones, 60 por corrida y 1.000 al día para el rastreo; 10 por corrida y 500 al día para el ciclo.
- El tema sale de la categoría de primer nivel con reglas `alkosto:` en `config/topics.toml`. Migración `0007_alkosto` con la fuente; se pausa con la BD en ≥ 450 MB.

## Alternativas consideradas
- Sitemap de productos y fichas HTML: no depende de Algolia · ~1,4 MB por ficha, cientos de veces más tráfico y más frágil · descartada.
- Páginas de categoría (PLP): mismos datos · por detrás pagina la misma Algolia, pesa más y exige parsear HTML · descartada.

## Consecuencias
- Cada producto de Alkosto se revisa cada hora (~20 peticiones por vuelta), frente a ~10 h en Éxito; ~14 mil filas más en `catalog_item`.
- La regla del mismo EAN en 6 h ahora cruza Alkosto con las tiendas VTEX.
- K1, decisión de David: «Sus términos del sitio («Usos NO permitidos») prohíben «spiders… ó cualquier otro dispositivo, programa, algoritmo ó método para acceder, adquirir, copiar ó monitorear» su contenido. David decidió sumarla igual y asume el riesgo (bloqueo o reclamo).» No se sortea ningún CAPTCHA ni detección de bots: si Alkosto bloquea, se apaga con `/fuente off alkosto`.
- Ktronix y Alkomprar quedan fuera (K3).

## Cuándo revisar
- Se sostiene con (hoy): ~14 mil productos, ~20 peticiones por vuelta, una clave pública en la portada.
- Revisar si: Algolia cambia de app o de índice; la clave deja de estar en la portada; o Alkosto reclama o bloquea.
