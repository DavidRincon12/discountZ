# Estado de discountZ

## Estado actual
- v1 en producción desde el 2026-10-14 (Supabase, `DISCOUNTZ_ACTIVO=true`); cron-job.org dispara el ciclo cada ~2 min y el rastreo cada hora.
- Rastreo de catálogo (ADR 0004): Éxito, Panamericana, Jumbo y Pepe Ganga (VTEX), Alkosto (ADR 0009) y eBay (ADR 0008, `MODEL_LOW`). Carulla y Olímpica apagadas y sin catálogo (migración 0011).
- Canales de Telegram (ADR 0005: DescuentosTech, DescuentosHome, Clubgratis, cajitatech, OFERTU) y Amazon de Slickdeals «sin verificar» (ADR 0006). DealNews apagado (406 desde Actions). Best Buy y Walmart fuera; Amazon da CAPTCHA.
- Perfil de temas (ADR 0013): en tarjeta Tecnología, Electrodomésticos, Ferretería, Moda y Libros; Hogar, Deportes y Otros solo por interés (sin intereses aún, no llegan); Belleza, Mercado, Bebés y Mascotas no se rastrean y sus ofertas de canales quedan vistas.
- Dos chats: el grupo (todo, temas apagados) y el canal general (`TELEGRAM_CHANNEL_ID`, solo ofertas y resúmenes de ofertas).
- Suite: 919 unitarias y 219 de integración en verde el 2026-10-22.

## Último cambio
- Rama `claude/project-thread-rmnaug` (PR 1 del perfil, sin migración): `mode` por tema en `config/topics.toml`; el rastreo salta las categorías apagadas (`CatalogPartition` + `crawl_wanted`) y no confirma temas fuera de tarjeta; los canales solo envían temas en tarjeta.

## Siguiente paso
- PR 2 del perfil, en Native: intereses (`/interes`, ~55 semillas en español e inglés en `config/interests.toml`, migración 0012). Empieza en la Task 6 del plan `docs/superpowers/plans/2026-10-22-perfil-e-intereses.md` (spec `docs/superpowers/specs/2026-10-22-perfil-e-intereses-design.md`). Al fusionarlo: *Actions → migrate* con `to = head` y luego `setup-bot`.
- Después, cada una con su spec: 9to5Toys (RSS), Woot (API oficial; el secreto `WOOT_API_KEY` ya está en GitHub) y Mercado Libre (falta la app de desarrollador de David).
- Medir la calidad 2-3 días con `/mnt/project-files/calidad-tiendas/consulta-todas.sql` (contar también `alert.delivery = 'held'` y `offer_log`) y ajustar `[pricing]`, `[cards]` o los modos si David lo pide.
- Ideas sin hacer: comparar por EAN con otras tiendas y botón 👎 que silencia marca o categoría. G10 en `/buscar`: `check_batch` (`domain/guards.py`) no se llama.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase y Telegram por getUpdates (ADR 0001); histórico por segmentos (0002); disparo externo y modo caliente (0003); rastreo completo (0004); canales y feeds «sin verificar» (0005, 0006, 0007); eBay por la Browse API (0008); Alkosto por Algolia asumiendo sus términos (0009); sin carril por descuento en VTEX (0010); enlace sin referido vía `joylink.io` (0011); tope diario y resumen de la noche (0012); perfil de temas e intereses (0013).
- Sin costo puesto ni TRM. Descuento solo contra precios observados (nunca `ListPrice`); la alerta exige «antes» estable 24 h y 2 lecturas, confirmación en la ficha y que otra tienda no haya alertado el mismo EAN en 6 h.
- Con la BD en ≥ 450 MB se pausan las búsquedas y el rastreo de Éxito, Carulla, eBay, Alkosto y Jumbo. Sin temas guardados todo va a *General*.
- No se sortea ningún CAPTCHA ni detección de bots. eBay se deja pese a sus términos (§9.5 y §3.1(b), revisados 2026-10-20) y Slickdeals sigue encendido pese a los suyos y a su robots.txt (spec del perfil §7): decisiones de David.

## Pendientes y gotchas
- El mantenimiento no borra el catálogo de una tienda apagada (cuenta desde su última corrida); borrar filas no baja `pg_database_size` sin `VACUUM FULL`, que no corre dentro de una transacción. Las categorías que el perfil deja de rastrear vencen solas con la retención.
- Una tienda que rastrea de noche puede gastar sus 8 tarjetas en la madrugada (sin sonido); el resto del día va al resumen. Lo retenido después de las 21:00 sale al día siguiente.
- BD: el rastreo reconecta hasta 2 veces (`db_reconnect`); el ciclo no. Supabase: el migrador necesita `GRANT CREATE ON DATABASE postgres TO discountz_owner`. Postgres en la nube: `service postgresql start` y `ALTER USER postgres PASSWORD 'postgres'`.
- Resumen diario (13:00Z): `catalog_daily_stats` a veces pasa el `statement_timeout` de 15 s y se reintenta; el peso sale en el log `maintenance db_mb=`.
- Desde la nube de la sesión: Newegg, Best Buy, Amazon, `*.vtexassets.com`, Jumbo y Pepe Ganga bloqueados (no desde Actions); sus fixtures se graban con un workflow temporal. `record-fixture` no sigue redirecciones.
- Ofertas con el tema equivocado: agregar el término a `channel_rules` y la oferta a `tests/fixtures/topics/ofertas-etiquetadas.jsonl` (`tests/unit/test_topic_precision.py` exige ≥ 95 %). Con el perfil, un tema equivocado puede esconder una oferta (p. ej. un filtro de polvo para PC cae en Hogar).
- Falta: `pg_terminate_backend` tras 3 `skipped_locked` (separar antes el `application_name` del ciclo y del rastreo); reloj fijo por corrida; orden del barrido VTEX sin medir; parámetros de eBay leídos al importar. Hot Sale 2027 sin fecha publicada.
