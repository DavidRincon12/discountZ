# Estado de discountZ

## Estado actual
- Plan v1 completo (Tasks 1-26): dominio puro, BD con `0001_init`, lease, HTTP con `PacedClient`, Telegram con comandos y ciclo completo con confirmación, mantenimiento y resumen diario.
- Fuentes: VTEX ×4 y Amazon US con fixtures reales; Newegg (fichas reales; búsqueda desactivada, `/buscar newegg` se rechaza); Slickdeals como feed (`COMMUNITY_DEAL`, U34/U34b de punta a punta); Walmart (fixtures reales) y Best Buy (fixtures reales) detrás de `/fuente on`.
- Despliegue: workflows `cycle`, `migrate` y `keepalive`; guía de puesta en marcha en `README.md`. El job de `cycle` se omite hasta que la variable de Actions `DISCOUNTZ_ACTIVO` valga `true`.
- Suite: 410 pruebas unitarias y 51 de integración (Postgres) en verde el 2026-10-14.

## Último cambio
- Rama `claude/discountz-puesta-en-marcha-mlcmm6`: preparación de la puesta en marcha. `/buscar` rechaza las tiendas sin `Capability.SEARCH` y Newegg la pierde: su búsqueda da CAPTCHA desde la nube y desde la red de David, y su parser solo conoce HTML sintético. `cycle` queda detrás del interruptor `DISCOUNTZ_ACTIVO` para que el cron de respaldo no falle cada 30 min mientras faltan los secretos. README: pasos de `check-config` y de encendido.

## Siguiente paso
- David hace la puesta en marcha según `README.md`: bot, Supabase, secretos y `production-db`, `check-config`, migración, `DISCOUNTZ_ACTIVO=true`, primer `cycle` manual y cron-job.org.
- Con el ciclo en marcha: revisar los primeros runs (Amazon con `curl_cffi`, fichas de Newegg y Best Buy desde Actions) y agregar canarios de Newegg.
- Reactivar la búsqueda de Newegg solo con una fixture real (el spike desde Actions del 2026-10-13 sí leyó precio).
- Después: SP-6 (canales comunitarios de Telegram: DescuentosTech, Clubgratis, cajitatech).

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001); histórico por segmentos (ADR 0002); disparo externo y modo caliente (ADR 0003).
- Sin costo puesto ni TRM: las alertas USD dicen «🇺🇸 Compra por casillero».
- Si spec y plan difieren, manda spec §0.2 y luego el plan.
- La confirmación exige ±1 %, stock, misma variante y mismo vendedor; en búsquedas sin vendedor visible (Amazon, Newegg) el vendedor es provisional y la ficha decide.
- Slickdeals: un solo task `feed` por ciclo para todas sus búsquedas, sin reserva de confirmación; el lead se lee en el SourceRun de la tienda (se crea si no tenía tareas) y se omite si el listing se leyó hace < 6 h.

## Pendientes y gotchas
- Postgres en la nube: `service postgresql start` y, si pide clave, `ALTER USER postgres PASSWORD 'postgres'`.
- Desde la nube de la sesión: Newegg da 403 CAPTCHA (httpx y curl_cffi) y las fichas de Best Buy 502/timeout; Amazon con `curl_cffi` recibe el intersticial de Akamai (fixtures con httpx).
- La búsqueda de Newegg (`/p/pl?d=…`) da 403 CAPTCHA también desde la red de David, con ambos clientes: está desactivada y su fixture sigue sintética. Las fichas sí responden. No intentar sortear el CAPTCHA.
- Las dos fichas reales de Newegg (`N82E…`) tienen el buy box de un tercero (`m`): el vendedor de un Item# no es fijo. `record-fixture` no sigue redirecciones y en Windows escribe el `.meta.json` con CRLF.
- Slickdeals ya no publica `u2=` (usa `u3=` cifrado): hoy solo los leads de Amazon (`data-aps-asin`) llegan a una tienda.
- Best Buy: el HTML de la búsqueda no trae precios (GraphQL) y el «$49.99» es la membresía Plus. Walmart ya no trae JSON-LD Product: su ruta independiente es el precio héroe.
- Fechas del calendario de eventos 2026-2027 estimadas: David debe confirmarlas (Hot Sale y Cyber Lunes de Colombia, sobre todo).
- Falta: `setup-bot`, `pg_terminate_backend` tras 3 `skipped_locked`, descarte G10 contra `R_listing`, mensaje agrupado de tormenta y `config/supabase-ca.crt` para `sslmode=verify-full`.
- Una alerta de un ciclo que murió antes de componer sale con vista reducida (sin línea de histórico).
- ruff 0.17 formatea Markdown: `docs/` está excluido. La imagen `postgres:17` del CI no tiene digest fijado.
