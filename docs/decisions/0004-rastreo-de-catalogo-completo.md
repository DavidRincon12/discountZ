# 0004. Rastrear el catálogo completo en corridas horarias por tienda

- Fecha: 2026-10-14
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-14-rastreo-catalogo-design.md` (§0.2 E1-E6, §2 y §7)

## Contexto
David quiere enterarse de cualquier bajada de precio de las tiendas sin elegir productos (E1), a costo $0 (E2) y sin sortear detección de bots (E6). Las 4 tiendas VTEX suman ~515.000 productos con stock; Supabase gratis da 500 MB; GitHub Actions es ilimitado en el repo público. El «antes» que publican las tiendas está inflado en el marketplace.

## Decisión
- Un caso de uso nuevo `crawl` (CLI y workflow `crawl.yml`) separado del ciclo de 5 min: un job por tienda en matriz, disparado cada hora desde cron-job.org, de hasta ~45 min, que reanuda una vuelta completa desde un cursor en la BD y antes relee una capa caliente (productos que cambiaron en 7 días).
- Por producto solo se guardan números (precio, precio habitual, mínimo, fechas, EAN y tema); título y foto se leen al alertar. Del historial, solo los cambios de precio por 90 días.
- El descuento se mide contra el precio habitual observado por discountZ; nunca contra el `ListPrice` de la tienda.
- EE. UU. entra solo por APIs oficiales: Best Buy (clave gratuita) ahora; Walmart si aprueban la API de afiliados; Amazon queda en Slickdeals y `/seguir`.
- Las alertas del catálogo se confirman releyendo la ficha y se publican como imagen generada con Pillow en el tema de su categoría de un supergrupo de Telegram; la entrega la hace el ciclo de 5 min para respetar el ritmo de Telegram.

## Alternativas consideradas
- Ordenar por «mayor descuento» de la tienda: pocas peticiones · devuelve ofertas falsas por el `ListPrice` inflado · descartada.
- Solo categorías elegidas: más rápido · deja productos por fuera (contra E1) · descartada.
- Guardar todo el historial en Supabase Pro o en archivos del repo: más datos · cuesta dinero o expone datos y crece sin control · descartada.
- Job único que recorre todas las tiendas: más simple · Éxito bloquea a las demás · descartada por la matriz.
- Rastrear Amazon, Walmart o Newegg por HTML desde Actions: bloquean robots y sortearlo está prohibido (E6).

## Consecuencias
- Latencia de detección: Éxito ~6 h por vuelta completa; Carulla ~2 h; Olímpica y Panamericana ~1 h; productos volátiles cada hora; Best Buy por cambios desde la última hora.
- ~300 MB estimados en Supabase; el mantenimiento poda cambios a 90 d y productos sin verse en 30 d.
- ~36.000 peticiones diarias a Éxito y Carulla: riesgo de bloqueo controlado por el circuit breaker y topes en la configuración.
- Dependencia nueva: Pillow y una fuente OFL versionada.

## Cuándo revisar
- Se sostiene con (hoy): vuelta de Éxito ≤ 8 h, BD ≤ 350 MB, 0 bloqueos sostenidos.
- Revisar si: la BD pasa de 400 MB; una tienda bloquea más de 2 corridas seguidas; o las alertas por tema pasan de 50 al día durante una semana.
