# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`); cron-job.org dispara el ciclo cada ~2 min y el rastreo cada hora.
- Rastreo de catálogo (ADR 0004): Éxito, Panamericana, Jumbo y Pepe Ganga (VTEX), Alkosto (ADR 0009) y eBay (ADR 0008, `MODEL_LOW`). David decidió apagar Carulla y Olímpica el 2026-10-22 (`/fuente off`): casi todo mercado y falsos errores de precio.
- Canales de Telegram (ADR 0005: DescuentosTech, DescuentosHome, Clubgratis, cajitatech, OFERTU) y Amazon de Slickdeals «sin verificar» (ADR 0006). DealNews apagado (406 desde Actions). Best Buy y Walmart fuera; Amazon da CAPTCHA.
- Dos chats: el grupo (todo, temas apagados) y el canal general (`TELEGRAM_CHANNEL_ID`, solo ofertas y resúmenes de ofertas).
- Suite: 912 unitarias y 209 de integración en verde el 2026-10-22.

## Último cambio
- Rama `claude/project-thread-xs8ugi` (calidad de las ofertas, informe «Calidad de las ofertas de discountZ» del 2026-10-22: ~450 ofertas al día): «casillero» solo si el post lo dice; rastreo con «antes» ≥ 100.000 COP/US$25, ahorro ≥ 30.000/US$5, sin mercado y error de precio solo de la tienda con 3 días; lo de casillero < US$25 o de mercado no sale; 8 tarjetas por fuente al día y una por promoción, el resto en el resumen de las 21:00 (ADR 0012, migración 0010, esquema 10). Meta: ~60 tarjetas al día.

## Siguiente paso
- Al fusionar: *Actions → migrate* con `to = head` (sin eso el ciclo y el rastreo se saltan). Luego medir 2-3 días con `/mnt/project-files/calidad-tiendas/consulta-todas.sql` (contar también `alert.delivery = 'held'` y `offer_log`) y ajustar `[pricing]` o `[cards]` si David lo pide.
- Ideas del informe aún sin hacer: `/interes <palabras>` (salta el tope), comparar por EAN con otras tiendas, botón 👎 que silencia marca o categoría.
- G10 en `/buscar`: `check_batch` (`domain/guards.py`) no se llama; hoy el lote constante no alerta pero entra al histórico.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase y Telegram por getUpdates (ADR 0001); histórico por segmentos (0002); disparo externo y modo caliente (0003); rastreo completo (0004); canales y feeds «sin verificar» (0005, 0006, 0007); eBay por la Browse API (0008); Alkosto por Algolia asumiendo sus términos (0009); sin carril por descuento en VTEX (0010); enlace sin referido vía `joylink.io` (0011); tope diario y resumen de la noche (0012).
- Sin costo puesto ni TRM. Descuento solo contra precios observados (nunca `ListPrice`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito, Carulla, eBay, Alkosto y Jumbo. Sin temas guardados todo va a *General*.
- No se sortea ningún CAPTCHA ni detección de bots. eBay se deja pese a sus términos (§9.5 y §3.1(b), revisados 2026-10-20; decisión de David).

## Pendientes y gotchas
- El mantenimiento no borra el catálogo de una tienda apagada (cuenta desde su última corrida): Carulla y Olímpica siguen ocupando espacio hasta que David las borre.
- Una tienda que rastrea de noche puede gastar sus 8 tarjetas en la madrugada (sin sonido); el resto del día va al resumen. Lo retenido después de las 21:00 sale al día siguiente.
- BD: el rastreo reconecta hasta 2 veces (`db_reconnect`); el ciclo no. Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner`. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`. Pendiente menor: borrar `catalog_price_change` y `CatalogRow.price_changed`.
- Resumen diario (13:00Z): `catalog_daily_stats` a veces pasa el `statement_timeout` de 15 s y se reintenta; el peso sale en el log `maintenance db_mb=`.
- Desde la nube de la sesión: Newegg, Best Buy, Amazon, `*.vtexassets.com`, Jumbo y Pepe Ganga bloqueados (no desde Actions); sus fixtures se graban con un workflow temporal. `record-fixture` no sigue redirecciones.
- Ofertas con el tema equivocado: agregar el término a `channel_rules` y la oferta a `tests/fixtures/topics/ofertas-etiquetadas.jsonl` (`tests/unit/test_topic_precision.py` exige ≥ 95 %).
- Falta: `pg_terminate_backend` tras 3 `skipped_locked` (separar antes el `application_name` del ciclo y del rastreo); reloj fijo por corrida; orden del barrido VTEX sin medir; parámetros de eBay leídos al importar. Hot Sale 2027 sin fecha publicada.
