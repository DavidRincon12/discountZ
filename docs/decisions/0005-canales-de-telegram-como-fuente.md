# 0005. Leer canales públicos de ofertas de Telegram como fuente «sin verificar»

- Fecha: 2026-10-17
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-17-canales-telegram-design.md` (§0.2 F1-F4, §1, §2 y §6)

## Contexto
Amazon no se puede leer desde Actions (CAPTCHA) y su API oficial exige ventas de afiliado. Los canales públicos DescuentosTech, Clubgratis y cajitatech publican ~180 ofertas al día de Amazon y de otras tiendas que discountZ no cubre. El presupuesto sigue en $0 y no se sortea ninguna detección de bots (E6).

## Decisión
- Una fuente nueva de tipo `channel` con un código por canal (`tg_descuentostech`, `tg_clubgratis`, `tg_cajitatech`) lee la página pública `https://t.me/s/<canal>?after=<id>`, sin cuenta de Telegram, como máximo cada 10 minutos y dentro del ciclo de 5 minutos.
- Llegan todas las ofertas de los tres canales (F1), cada una en el tema de su categoría, una sola vez aunque salga en varios canales (F3), como tarjeta marcada «sin verificar» con el precio que publica el canal (F4). No pasan por `listing` ni por el histórico.
- Un cursor por canal en la BD se avanza en la misma transacción en que se encolan las tarjetas; la primera lectura no envía nada.

## Alternativas consideradas
- Leer con la cuenta de Telegram de David (cliente MTProto): ve canales privados · riesgo de bloqueo de su cuenta · descartada (F2).
- Agregar el bot a los canales: los canales no admiten bots ajenos que lean · imposible.
- Amazon Creators API: datos oficiales · exige 10 ventas en 30 días · descartada.
- Keepa API: historial completo de Amazon · desde ~€49 al mes · descartada por costo.
- Resolver los acortadores (`pccajita.link`, `joylink.io`) y confirmar el precio en la tienda: verificaría la oferta · responden 403 desde la nube y Amazon da CAPTCHA · fuera de alcance.

## Consecuencias
- Cobertura de Amazon y de tiendas nuevas sin leer sus páginas, a cambio de alertas que no se verifican: la etiqueta lo dice en cada tarjeta.
- ~430 peticiones diarias a `t.me`, con breaker y presupuesto por canal.
- Un cambio de formato de un canal deja de producir ofertas: cuenta como `PARSE_ERROR` y el breaker lo pausa con aviso.
- Dos tablas pequeñas (`channel_cursor`, `channel_post`, ~1.300 filas con retención de 7 días).

## Cuándo revisar
- Se sostiene con (hoy): ~180 publicaciones al día, ~1.300 filas en `channel_post`, 0 bloqueos de `t.me`.
- Revisar si: un canal da `PARSE_ERROR` en más de 2 ciclos seguidos; las tarjetas de canales pasan de 100 al día durante una semana; o `t.me/s` empieza a pedir inicio de sesión.
