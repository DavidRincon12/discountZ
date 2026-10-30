# 0019. Leer las semillas de Mercado Libre por ventanas de un índice parcial

- Fecha: 2026-10-30
- Estado: Aceptada
- Complementa: 0018 (cómo se eligen las semillas; lo demás sigue igual)
- Plan: `docs/superpowers/plans/2026-10-30-semillas-por-ventanas.md`

## Contexto
Desde la fusión del PR #51 (2026-10-30 02:12Z), las 11 corridas horarias del rastreo fallaron solo en el trabajo de `mercadolibre`: `db.ean_seeds` lanzaba `OperationalError` a los ~15 s en cada intento, el rastreo reconectaba dos veces y salía con `db_unavailable` (`crawl_seeds count=0 found=0`, ninguna petición a la API). El rol `discountz_app` tiene `statement_timeout = '15s'` (guía de puesta en marcha) y Supabase Nano da 250 IOPS y ~5 MB/s de base fuera de su ráfaga diaria. La consulta hacía `Seq Scan` del catálogo, `DISTINCT ON (ean)` con orden en disco y después el orden por precio; en una copia local de 895 mil filas (646 mil con EAN) tocó 34.205 páginas (~267 MB) y escribió 13 MB de orden temporal, y además se repetía entera por cada 200 semillas.

## Decisión
- Índice parcial `catalog_item_seed_ix ON catalog_item (price_minor, id) WHERE currency = 'COP' AND ean IS NOT NULL AND price_minor >= 100000` (8,7 MB en la copia local). `(price_minor, id)` es único, así que sirve de marca exacta.
- `db.ean_seed_window` lee 200 filas de ese índice, del más caro al más barato (a igual precio, `id` de mayor a menor), después de la marca `(price_minor, id)` que el rastreo guarda en `crawl_cursor.seed_price_minor` y `crawl_cursor.seed_item_id` al terminar cada ventana. De esas filas salen las semillas: tema en tarjeta, EAN no buscado en 30 días y no seguido por la fuente, sin repetir EAN. Cuando la ventana viene incompleta se acabó el catálogo: la marca vuelve a `NULL` y la próxima corrida empieza otra vuelta por el más caro. Una corrida lee a lo sumo 25 ventanas.
- Si un EAN está en varias tiendas, manda su fila más cara de un tema en tarjeta: es la primera que aparece en el orden del índice. Antes mandaba la fila vista más recientemente, que obligaba a ordenar todo el catálogo por EAN.
- El piso del índice es 100.000 COP (`db.SEED_FLOOR_MINOR`, el `min_price_cop` de hoy). Las semillas usan `max(min_price_cop, 100.000)` y el piso va como literal en el SQL, para que cualquier plan pueda usar el índice parcial.

## Alternativas consideradas
- Subir el `statement_timeout` solo para esa consulta: deja ~267 MB de lectura y un orden en disco cada hora; con 5 MB/s de base pasaría el minuto y frenaría al resto · descartada.
- Una cola diaria de semillas (tabla aparte llenada una vez al día con la consulta vieja): una tabla más, una lectura completa al día y la misma consulta, que crece con el catálogo · descartada.
- Índice sin piso de precio: 25 MB · descartado por el peso de la BD (374 MB de 500 el 2026-10-30).
- Índice `(price_minor, ean)`: 11 MB, y la marca no es única entre tiendas con el mismo EAN y precio · descartado.
- Recorrer el índice por EAN que ya existe (`catalog_item_ean_ix`): no da el orden del más caro al más barato que eligió David · descartado.
- Spike: ¿una ventana de 200 filas lee menos de 1.000 páginas en frío? · medido 453 lecturas al empezar y 810 y 861 a 100 mil y 200 mil filas (EXPLAIN ANALYZE BUFFERS tras reiniciar Postgres, 895 mil filas, 30 mil EAN buscados y 12 mil seguidos), unos 25 ms en local; a 250 IOPS son ≤ 3,5 s · sí.

## Consecuencias
- Cada consulta de semillas cuesta lo mismo con cualquier tamaño de catálogo y queda lejos de los 15 s; el índice suma ~9 MB y una entrada por cada actualización de una fila en pesos con EAN desde 100.000.
- Un producto que entra o sube de precio por encima de la marca espera a la vuelta siguiente; un tema que pasa a tarjeta, también.
- La migración `0018` construye el índice dentro de una transacción: mientras, las escrituras del rastreo en `catalog_item` esperan hasta su `lock_timeout` de 5 s y la corrida reconecta.
- Con `min_price_cop` por debajo de 100.000, las semillas siguen desde 100.000 hasta otra migración.

## Cuándo revisar
- Se sostiene con (hoy): ~650 mil filas en pesos con EAN, 200 filas por ventana, ≤ 3,5 s por ventana en frío y la BD en 374 MB.
- Revisar si: aparece `db_reconnect source=mercadolibre … error=QueryCanceled`, la BD llega a 450 MB, el catálogo en pesos pasa de 2 millones de filas o `min_price_cop` baja de 100.000.
