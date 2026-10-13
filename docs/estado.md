# Estado de discountZ

## Estado actual
- Dominio completo y puro (Tasks 4-11): `Money`, `SafeUrl`, segmentos, estadísticos, guardas G1-G14, `evaluate()` (U01-U37), `decide()`, comandos, breaker/salud y planificador.
- BD (Tasks 12-13): migración `0001_init`, ejecutor con sha256 y lease, repositorio SQL (`infra/db.py`); CLI `migrate --to N|head --wait-lease 20m`.
- HTTP (Task 14): `HttpxClient`, `CurlClient` y `PacedClient` (ritmo, presupuesto, reintentos, bloqueos); `FakeHttp` para pruebas.
- Fuentes (Tasks 15-16): contrato `Source`, registro, `detect_block_common`, fixtures + `record-fixture`; adaptador VTEX para Éxito, Carulla, Olímpica y Panamericana con fixtures reales.
- Aún no hay Telegram, configuración, ciclo ni workflows de despliegue.

## Último cambio
- Rama `claude/discountz-v1-plan-execution-ur365u`: Tasks 1-16 del plan y revisión completa de esas tareas (5 hallazgos importantes y 3 menores corregidos con pruebas).

## Siguiente paso
- Plan `docs/superpowers/plans/2026-10-13-discountz-v1.md` en modo **Native** (superpowers:executing-plans): continuar en la **Task 17** (Telegram: cliente, render y bandeja de entrada).
- Criterio de cada tarea: su prueba en verde y el comando de verificación completo de `CLAUDE.md` (más `-m integration` con Postgres si toca la BD).
- Ledger local de ejecución: `.superpowers/sdd/2026-10-13-discountz-v1/progress.md` (no se versiona; si se pierde, reconstruir desde `git log`). Las decisiones tomadas en cada tarea están en los cuerpos de los commits y en ese ledger.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions, Supabase Postgres, Telegram vía getUpdates (ADR 0001).
- Histórico por segmentos y estadísticos ponderados por tiempo (ADR 0002); disparo externo y modo caliente (ADR 0003).
- Sin costo puesto/TRM: las alertas USD dicen «🇺🇸 Compra por casillero».
- Si spec y plan difieren, manda spec §0.2 y luego el plan.
- M2 (2026-10-13): `fq=skuId:A&fq=skuId:B&fq=skuId:C` devuelve los 3 ítems en < 0,3 s en las 4 tiendas VTEX ⇒ `max_batch = 50` (no se midió con 50 SKUs).
- M3 (2026-10-13): la simulación anónima (`sc=1`, sin código postal) responde con `sellingPrice` en centavos coherente en las 4 tiendas ⇒ todas tienen ruta independiente.

## Pendientes y gotchas
- Carulla ya no responde en `/api`: redirige (308) a `/io/api`, igual que Éxito. Olímpica y Panamericana siguen en `/api`.
- Éxito y Olímpica venden mucho por marketplace (vendedor por defecto ≠ "1" ⇒ `m`); la simulación necesita ese vendedor.
- El `now` del ciclo viaja en `SourceContext.now`: los adaptadores no leen el reloj.
- El ciclo (Task 19) debe pasar `deadline=` (plazo blando) a cada `PacedClient`; sin él, los reintentos no miran el plazo.
- ruff 0.17 formatea Markdown: `docs/` está excluido en `pyproject.toml`.
- La imagen `postgres:17` del CI no tiene digest fijado. En la sesión en la nube se usa el Postgres 16 del sistema (`service postgresql start`).
- El contrato «Adaptadores independientes» de import-linter crece con cada adaptador (falla si un módulo listado no existe).
- Walmart y Best Buy siguen detrás de la puerta de activación. Confirmar las fechas del calendario de eventos en la Task 18.
