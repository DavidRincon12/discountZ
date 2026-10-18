# 0008. Rastrear eBay por la Browse API y compararlo contra el mismo modelo

- Fecha: 2026-10-18
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-18-ebay-design.md` (§0.2 B1-B4, §3 y §4)

## Contexto
David quiere eBay con `/seguir`, `/buscar` y un barrido que descubra ofertas en todo lo posible, solo con envío a su casillero en EE. UU. (B1-B3). La Browse API responde desde Actions con sus claves de producción: 5.000 búsquedas al día más 5.000 lecturas en lote aparte, hasta 200 publicaciones por búsqueda y `offset` ≤ 10.000. Las 19 raíces de producto suman 2.054 categorías de nivel 3 y millones de publicaciones nuevas (solo tenis: 2 millones). El `epid` (modelo) viene en ~35 % de las publicaciones; el precio tachado lo pone el vendedor. La BD va en 290 MB de 500.

## Decisión
- Fuente `ebay_us` (`CrawlSource`, `auth="oauth_client"`) sobre la Browse API. El token de aplicación lo pide el adaptador por `ctx.http` y vive en memoria del proceso.
- Barrido de las categorías de nivel 3 de 19 raíces, las 75 publicaciones mejor posicionadas de cada una (~154 mil, tope de 150 mil en la BD), nuevas, a precio fijo, con envío al 33166, desde USD 15 y de vendedores con ≥ 98 % y ≥ 100 calificaciones (que cuentan como `FIRST_PARTY`).
- El precio observado es precio + envío a Miami; el envío calculado queda fuera y el `marketingPrice` nunca es «antes».
- Dos detecciones: la bajada de la misma publicación (rastreo de la ADR 0004, sin cambios) y `MODEL_LOW`, precio bajo frente a la mediana de ≥ 5 publicaciones del mismo `epid` vistas en 7 días.
- Migración `0005_ebay`: fuente, `catalog_item.model_key` con índice parcial y `alert.kind = 'model_low'`. El `external_id` es el id legado (`<legacy>` o `<legacy>V<variación>`) porque la BD no admite `|`.

## Alternativas consideradas
- Búsquedas vigiladas con precio máximo: precisas · exigen mantener una lista y no descubren · descartada por David.
- Barrer las 9.234 hojas: más cobertura · una vuelta pasaría de 3 días con la cuota · descartada.
- Leer el HTML de eBay: sin cuota · contra sus términos y con detección de bots · descartada.
- Deal API de eBay: ofertas curadas · exige acceso especial · fuera de alcance.
- Solo la mediana por modelo: lo que eligió David · cubre ~35 % · se suma la bajada de la misma publicación.

## Consecuencias
- Vuelta completa de eBay cada ~17 h; lo que cambió de precio se relee cada hora (~1.600 publicaciones).
- ~70 MB más en la BD; con ≥ 450 MB eBay se pausa con Éxito y Carulla.
- Una publicación nueva y barata fuera de las 75 mejor posicionadas no se ve salvo por `/buscar` o `/seguir`.
- Toda alerta relee la ficha antes de enviarse (lo mostrado nunca tiene más de 6 h de atraso).

## Cuándo revisar
- Se sostiene con (hoy): 4.800 peticiones al día de rastreo, 150 mil publicaciones, BD ≤ 360 MB.
- Revisar si: la BD pasa de 400 MB; la cuota se agota 2 días seguidos; o eBay da más de 50 alertas al día durante una semana.
