# Estado de discountZ

## Estado actual
- Repo con autoría protegida (`.claude/hooks/git-guard.sh`) y configuración de Claude Code.
- Spec aprobado: `docs/superpowers/specs/2026-10-13-discountz-v1-design.md` (manda su §0.2, decisiones de David).
- Plan aprobado: `docs/superpowers/plans/2026-10-13-discountz-v1.md` (26 tareas, TDD).
- Aún no hay código de producto.

## Último cambio
- PR #1 (`claude/magical-gauss-atp26y`): autoría, spike de fuentes desde Actions, spec y plan.

## Siguiente paso
- Ejecutar el plan desde la **Task 1** en modo **Native** (superpowers:executing-plans), en una sesión nueva.
- Criterio de cada tarea: su prueba en verde y el comando de verificación completo de «Global Constraints» del plan.

## Decisiones vigentes
- Python 3.13 síncrono en GitHub Actions (repo público), Supabase Postgres, Telegram con comandos vía getUpdates.
- Disparo externo cada 5 min (cron-job.org → workflow_dispatch) y schedule de respaldo cada 30 min.
- Sin costo puesto/TRM: las alertas USD dicen «🇺🇸 Compra por casillero».
- Lease con TTL sin fencing; sin respaldo en v1; healthchecks opcional.
- Canales comunitarios (SP-6, después de la v1): DescuentosTech, Clubgratis, cajitatech.

## Pendientes y gotchas
- El entorno de la sesión ya permite los dominios de las tiendas (Network access → Custom); se pueden grabar fixtures desde la sesión.
- Mercado Libre responde 403 sin OAuth (SP-1). Alkosto/Ktronix/Falabella necesitan su JSON interno (SP-2/SP-3).
- En el spike, Walmart y Best Buy dieron precios dudosos (24,00 y 49,99): quedan detrás de una puerta de activación.
- Confirmar las fechas del calendario de eventos en la Task 18.
