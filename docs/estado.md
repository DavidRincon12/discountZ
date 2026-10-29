# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`); cron-job.org dispara el ciclo cada ~2 min y el rastreo cada hora.
- Rastreo de catálogo (ADR 0004): Éxito, Panamericana, Jumbo y Pepe Ganga (VTEX), Alkosto (ADR 0009), Homecenter (ADR 0016), Falabella (ADR 0017, bloqueada desde Actions), eBay (ADR 0008, `MODEL_LOW`) y, en este PR, Mercado Libre (ADR 0018): sin vuelta, busca por EAN lo que venden las tiendas CO y relee lo hallado. Carulla y Olímpica apagadas y sin catálogo (migración 0011).
- Canales de Telegram (ADR 0005: DescuentosTech, DescuentosHome, Clubgratis, cajitatech, OFERTU), Amazon de Slickdeals «sin verificar» (ADR 0006), 9to5Toys por su RSS (ADR 0014) y Woot por su API (ADR 0015), por casillero los de EE. UU. DealNews apagado (406 desde Actions). Best Buy y Walmart fuera; Amazon da CAPTCHA.
- Perfil de temas (ADR 0013): Belleza, Mercado, Bebés y Mascotas apagados (no se rastrean ni se releen, y sus ofertas de canales quedan vistas); el resto en tarjeta.
- Intereses (esquema 12): semilla de `config/interests.toml` más los de `/interes`; lo que coincide sale con «🎯», hasta 3 por interés al día. Mercado Libre no entra (su lectura no trae título).
- Dos chats: el grupo (todo, temas apagados) y el canal general (`TELEGRAM_CHANNEL_ID`, solo ofertas y resúmenes de ofertas).
- Suite: 1493 pruebas (1210 unitarias y de fuentes, 283 de integración) en verde el 2026-10-29.

## Último cambio
- Rama `claude/project-thread-xsztn5`: Mercado Libre por su API con token de aplicación (`MELI_CLIENT_ID`/`MELI_CLIENT_SECRET`), migración `0017` (`ean_lookup`, `catalog_item.checked_at`, esquema 17), `domain/gtin.py`, protocolo `EanCatalogSource` y rastreo sembrado en `app/crawl.py` (spec `docs/superpowers/specs/2026-10-29-mercadolibre-design.md`, plan ejecutado completo en modo Native). Antes: garantías y servicios listados como producto ya no alertan (`is_service_title`) y la tarjeta de Homecenter toma la foto de la ficha (PR #49).

## Siguiente paso
- Al fusionar: *Actions → migrate* (`to = head`, esquema 17) antes del primer rastreo de `mercadolibre`.
- Primera semana de Mercado Libre: mirar en los logs `crawl_seeds count= found=`, los 429 y el tiempo de la consulta de semillas (si pasa de 2 s, índice parcial); subir de 20 mil peticiones al día solo tras una semana sin 429 y con el tope de 12 mil lleno (ADR 0018, «Cuándo revisar»). Vigilar el peso de la BD (`maintenance db_mb=`).
- Falabella: 403 desde Actions desde el 2026-10-25 (bloqueo por IP, no se evade); `/fuente off falabella` si sigue mandando errores.
- Woot: sus categorías `pc`, `software` y `peripherals` no están en `channel_rules` (76 de 261 ofertas de Computers salen en «otros»); agregarlas con su prueba si David lo pide.
- Revisar 2-3 días cuántos posts de 9to5Toys con «$» dan precio (ADR 0014) y la calidad con `/mnt/project-files/calidad-tiendas/consulta-todas.sql`.
- Ideas sin hacer: botón 👎 que silencia marca o categoría. G10 en `/buscar`: `check_batch` (`domain/guards.py`) no se llama.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase y Telegram por getUpdates (ADR 0001); histórico por segmentos (0002); disparo externo y modo caliente (0003); rastreo completo (0004); canales y feeds «sin verificar» (0005-0007); eBay por la Browse API (0008); Alkosto por Algolia (0009); sin carril por descuento en VTEX (0010); enlace sin referido vía `joylink.io` (0011); tope diario y resumen de la noche (0012); perfil de temas e intereses (0013); 9to5Toys (0014); Woot (0015); Homecenter (0016); Falabella solo lo de la tienda (0017); Mercado Libre por su catálogo sembrado con EAN (0018).
- Sin costo puesto ni TRM. Descuento solo contra precios observados (nunca `ListPrice` ni `original_price`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Con la BD en ≥ 450 MB se pausan las búsquedas, el rastreo de las tiendas grandes y las semillas de Mercado Libre. Sin temas guardados todo va a *General*.
- No se sortea ningún CAPTCHA ni detección de bots. eBay, Slickdeals y Mercado Libre siguen pese al riesgo de sus términos: decisiones de David.

## Pendientes y gotchas
- El mantenimiento no borra el catálogo de una tienda apagada (cuenta desde su última corrida); borrar filas no baja `pg_database_size` sin `VACUUM FULL`, que no corre dentro de una transacción.
- Una tienda que rastrea de noche puede gastar sus 50 tarjetas en la madrugada (sin sonido); el resto del día va al resumen. Lo retenido después de las 21:00 sale al día siguiente.
- BD: el rastreo reconecta hasta 2 veces (`db_reconnect`); el ciclo no. Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner`. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`.
- Resumen diario (13:00Z): `catalog_daily_stats` a veces pasa el `statement_timeout` de 15 s y se reintenta.
- Desde la nube de la sesión: Newegg, Best Buy, Amazon, `*.vtexassets.com`, Jumbo, Pepe Ganga y la API de Mercado Libre bloqueados (no desde Actions); sus fixtures se graban con un workflow temporal. `record-fixture` no sigue redirecciones.
- Homecenter sin foto: no se vio el porqué en los logs (ni `photo_missing` ni `photo_deferred`); si sigue el cuadro gris, revisar `crawl_done` y la foto de la ficha.
- Ofertas con el tema equivocado: término en `channel_rules` y oferta en `tests/fixtures/topics/ofertas-etiquetadas.jsonl` (`test_topic_precision.py` exige ≥ 95 %).
- Falta: `pg_terminate_backend` tras 3 `skipped_locked`; reloj fijo por corrida; orden del barrido VTEX sin medir; parámetros de eBay leídos al importar. Hot Sale 2027 sin fecha publicada.
