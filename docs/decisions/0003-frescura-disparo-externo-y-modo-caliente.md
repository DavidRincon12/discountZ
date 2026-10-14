# 0003. Disparar el ciclo cada 5 min desde fuera y priorizar productos calientes

- Fecha: 2026-10-13
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-13-discountz-v1-design.md` (§6; ADR 024, 025 y 026 de §9; decisiones D8 y D9 de §0.2)

## Contexto
David quiere que las ofertas relámpago no se pierdan (D8). El `schedule` de GitHub Actions se retrasa entre 5 y 30 min y no garantiza la frecuencia; un job en bucle de horas choca con los términos de uso de Actions.

## Decisión
- **Disparo externo** (ADR 024): cron-job.org hace `POST .../actions/workflows/cycle.yml/dispatches` cada 5 min con un token fine-grained solo para este repo y solo `Actions: read and write`, que caduca a los 12 meses (recordatorio 14 días antes en el resumen diario, fecha fijada con `/token_vence`). El `schedule` `7,37 * * * *` queda de respaldo; `concurrency` y el lease descartan solapes; `run.trigger_kind` distingue las dos vías.
- **Prioridad y modo caliente** (ADR 025): un listing que baja de precio en stock, o con una alerta pendiente de confirmar, queda caliente 2 h (`listing.hot_until`) y se lee cada ciclo. Orden del plan: confirmaciones > sonda/canario > watches pendientes > calientes > watches con objetivo > demás watches > búsquedas y feeds. Los watches con objetivo usan la mitad del intervalo; los seguimientos VTEX se leen en lote cada ciclo; durante un evento del calendario el intervalo se divide entre 3 y el presupuesto por ejecución sube 50 % sin pasar el de 24 h. Todo dentro del presupuesto y del circuit breaker: nunca hay ráfagas.
- **Canales públicos de Telegram como feed** (ADR 026, sub-proyecto SP-6, después de la v1): `t.me/s/<canal>` de DescuentosTech, Clubgratis y cajitatech; confirmación propia cuando la tienda es legible.

## Alternativas consideradas
- Solo `schedule`: sin dependencias externas · retrasos de 5-30 min · revertir: bajo.
- Job en bucle de horas: latencia de 1 min · choca con los términos de Actions.
- Cadencia fija por fuente: simple · las ofertas cortas se escapan entre lecturas.
- Páginas de ofertas por tienda y alertas «sin confirmar»: excluidas por David (D8).

## Consecuencias
- ~288 ciclos al día de ~3 min en un repo público, sin costo.
- Dependencia de un servicio externo gratuito (cron-job.org) y de un token que hay que rotar; si falla, el respaldo de 30 min sigue y el resumen diario lo muestra.
- Las ofertas de menos de ~10 min se siguen perdiendo (riesgo aceptado).

## Cuándo revisar
- Se sostiene con (hoy): disparo cada 5 min, ciclos reales cada 5-25 min, ≤ 110 peticiones por ciclo.
- Revisar si: la latencia de una alerta pasa de 15 min de forma sostenida; el p95 del ciclo pasa de 5 min; o Amazon da más de 10 % de captchas en 48 h por la frecuencia mayor.
