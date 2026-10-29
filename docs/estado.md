# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`); cron-job.org dispara el ciclo cada ~2 min y el rastreo cada hora.
- Rastreo de catálogo (ADR 0004): Éxito, Panamericana, Jumbo y Pepe Ganga (VTEX), Alkosto (ADR 0009), Homecenter (ADR 0016, 13 categorías de herramientas y tecnología), Falabella (ADR 0017, 14 categorías, solo lo que vende la tienda) y eBay (ADR 0008, `MODEL_LOW`). Carulla y Olímpica apagadas y sin catálogo (migración 0011).
- Canales de Telegram (ADR 0005: DescuentosTech, DescuentosHome, Clubgratis, cajitatech, OFERTU), Amazon de Slickdeals «sin verificar» (ADR 0006), 9to5Toys por su RSS (ADR 0014, precio desde el título) y Woot por su API (ADR 0015, en el PR #36), todos por casillero los de EE. UU. DealNews apagado (406 desde Actions). Best Buy y Walmart fuera; Amazon da CAPTCHA.
- Perfil de temas (ADR 0013): Belleza, Mercado, Bebés y Mascotas apagados (no se rastrean ni se releen, y sus ofertas de canales quedan vistas); el resto en tarjeta (Hogar, Deportes y Otros desde el PR #40).
- Intereses (esquema 12): 55 de la semilla (`config/interests.toml`) más los de `/interes`; lo que coincide (el término antes de «para», «con», «for»…) sale con «🎯», sin perfil, tope de la fuente ni mínimos de precio, hasta 3 por interés al día; después, lo que saldría sin el interés usa el tope de la fuente.
- Dos chats: el grupo (todo, temas apagados) y el canal general (`TELEGRAM_CHANNEL_ID`, solo ofertas y resúmenes de ofertas).
- Suite: 1394 pruebas (1134 unitarias y de fuentes, 260 de integración) en verde el 2026-10-28.

## Último cambio
- eBay (2026-10-29): una caída > 95 % contra la mediana del modelo sale como MODEL_LOW, no como error de precio (`MODEL_ERROR_MAX_BP`); los anuncios de pago de envío o diferencia y los enlaces «for VIP» ya no entran (`is_fee_listing_title`, `domain/catalog.py`, filtrados en `sources/ebay.py`); eran «errores de precio» falsos contra la mediana de su `epid`. Antes: garantías de Jumbo (PR #49).

## Siguiente paso
- Mercado Libre: la búsqueda pública da 403 aun con token y el catálogo responde con token de aplicación. Plan en el informe «Plan para sumar Mercado Libre a discountZ» (https://claude.ai/artifact/RDLrKpPRSFenZg5xTQ74im). Falta que David cree la app y suba `MELI_CLIENT_ID` y `MELI_CLIENT_SECRET`; luego, prueba desde Actions (token, y `/products/search` y `/products/{id}/items` con 200 en MCO) antes del spec.
- Falabella: Actions recibe 403 desde el 2026-10-25 (bloqueo por IP, no se evade); el 2026-10-28 su job del rastreo terminaba en 2 s (apagada o con el breaker abierto). Homecenter y Falabella están en Black Days (26-29 nov).
- Woot: 76 de 261 ofertas de Computers salen con tema «otros» porque sus categorías `pc`, `software` y `peripherals` no están en `channel_rules`; agregarlas con su prueba si David lo pide. Unas 22 ofertas «New (Brown Box)», «Bulk Packaging» o «International Version» se saltan por la regla de solo nuevas, como en eBay.
- Revisar 2-3 días cuántos posts de 9to5Toys con «$» dan precio (ADR 0014).
- Revisar con David la semilla ajustada de intereses (manda `config/interests.toml`). Un falso positivo nuevo va a `tests/unit/test_interest_seed.py` antes de tocar la semilla.
- Medir la calidad 2-3 días con `/mnt/project-files/calidad-tiendas/consulta-todas.sql` (contar también `alert.delivery = 'held'`, `offer_log`, `channel_post` con `baseline` por `topic` y `crawl_done skipped=`) y ajustar `[pricing]`, `[cards]` o los modos si David lo pide.
- Ideas sin hacer: comparar por EAN con otras tiendas y botón 👎 que silencia marca o categoría. G10 en `/buscar`: `check_batch` (`domain/guards.py`) no se llama.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase y Telegram por getUpdates (ADR 0001); histórico por segmentos (0002); disparo externo y modo caliente (0003); rastreo completo (0004); canales y feeds «sin verificar» (0005, 0006, 0007); eBay por la Browse API (0008); Alkosto por Algolia asumiendo sus términos (0009); sin carril por descuento en VTEX (0010); enlace sin referido vía `joylink.io` (0011); tope diario y resumen de la noche (0012); perfil de temas e intereses (0013); 9to5Toys con el precio del título (0014); Woot como canal sin verificar (0015).
- Sin costo puesto ni TRM. Descuento solo contra precios observados (nunca `ListPrice`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito, Carulla, eBay, Alkosto y Jumbo. Sin temas guardados todo va a *General*.
- No se sortea ningún CAPTCHA ni detección de bots. eBay se deja pese a sus términos (§9.5 y §3.1(b), revisados 2026-10-20) y Slickdeals sigue encendido pese a los suyos y a su robots.txt (spec del perfil §7): decisiones de David.

## Pendientes y gotchas
- El mantenimiento no borra el catálogo de una tienda apagada (cuenta desde su última corrida); borrar filas no baja `pg_database_size` sin `VACUUM FULL`, que no corre dentro de una transacción. Las categorías que el perfil deja de rastrear vencen solas con la retención.
- Una tienda que rastrea de noche puede gastar sus 50 tarjetas en la madrugada (sin sonido); el resto del día va al resumen. Lo retenido después de las 21:00 sale al día siguiente.
- BD: el rastreo reconecta hasta 2 veces (`db_reconnect`); el ciclo no. Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner`. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`.
- Resumen diario (13:00Z): `catalog_daily_stats` a veces pasa el `statement_timeout` de 15 s y se reintenta; el peso sale en el log `maintenance db_mb=`.
- Desde la nube de la sesión: Newegg, Best Buy, Amazon, `*.vtexassets.com`, Jumbo y Pepe Ganga bloqueados (no desde Actions); sus fixtures se graban con un workflow temporal. La API y los docs de Mercado Libre dan 403 (PolicyAgent) desde la nube. `record-fixture` no sigue redirecciones.
- Ofertas con el tema equivocado: agregar el término a `channel_rules` y la oferta a `tests/fixtures/topics/ofertas-etiquetadas.jsonl` (`tests/unit/test_topic_precision.py` exige ≥ 95 %). Con el perfil, un tema equivocado esconde la oferta: 34 de las 946 etiquetadas (`test_profile_hides_few_card_offers` fija ese techo); se baja con términos en `channel_rules` o con intereses.
- Homecenter sin foto: no se pudo ver el porqué en los logs (ni `photo_missing` ni `photo_deferred`); si sigue el cuadro gris, revisar `crawl_done` y la foto de la ficha. `media.falabella.com` no se alcanza desde la nube.
- Falta: `pg_terminate_backend` tras 3 `skipped_locked` (separar antes el `application_name` del ciclo y del rastreo); reloj fijo por corrida; orden del barrido VTEX sin medir; parámetros de eBay leídos al importar. Hot Sale 2027 sin fecha publicada.
