# 0014. Leer el RSS de 9to5Toys como canal «sin verificar» con el precio del título

- Fecha: 2026-10-23
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-23-9to5toys-design.md` (diseño corto aprobado por David en el hilo «Fuente 9to5Toys»)

## Contexto
David quiere más ofertas de tecnología de EE. UU. por casillero. 9to5Toys publica unas 45 al día y su RSS (`https://9to5toys.com/feed/`, 50 ítems) responde desde la nube; `robots.txt` no cierra `/feed/`. Sus términos viven en 9to5mac.com, bloqueado por el proxy de la sesión: no se leyeron. El feed no trae un campo de precio (va en el título, que también trae el ahorro y el precio de antes), su categoría solo dice Deals/News/Steals y los enlaces a tiendas son acortadores de afiliado.

## Decisión
- Una fuente `channel` más, `ninetofivetoys`, por el camino de los feeds de Amazon (ADR 0006): `ordered_ids = False`, primera lectura como `baseline`, duplicados en 24 h, tema por el título (`channel_rules`), perfil, mínimos, tope diario y resumen de la noche (ADR 0012, 0013). Siempre por casillero.
- El precio sale del título con una regla pura (`price_from_title`): se descartan los montos seguidos de «off», «savings», «discount»… o precedidos de «Reg.», «save», «up to»…; un monto restante después de «from», «starting», «under» o «as low as» marca un resumen sin precio; si no, manda el primero. Sin precio, el ítem se salta en silencio: noticias y resúmenes son normales.
- «Ver oferta» lleva al artículo de 9to5Toys (le da el crédito y no toca sus enlaces de afiliado); la tarjeta dice «Visto en 9to5Toys».
- Migración `0013` con la fuente; una lectura cada 10 min (≤ 200 al día).

## Alternativas consideradas
- Seguir el enlace a la tienda para verificar el precio: los acortadores llevan a Amazon (CAPTCHA) o a tiendas con WAF · descartada.
- Leer cada artículo: ~45 peticiones más al día y tampoco hay un precio estructurado · descartada.
- Tema por la categoría de WordPress: no distingue productos · descartada.

## Consecuencias
- Ofertas de tecnología de EE. UU. sin claves ni cuentas, marcadas «sin verificar».
- Un precio mal leído del título es posible en redacciones raras; la regla prefiere saltar el post a inventar un precio.
- Si el título cambia de estilo, los posts se saltan en silencio (no cuentan como formato roto): solo se nota porque la fuente deja de enviar.
- Riesgo sin medir: los términos de 9to5Toys. Se apaga con `/fuente off ninetofivetoys`.

## Cuándo revisar
- Se sostiene con (hoy): ~45 posts al día, 0 bloqueos, una petición cada 10 min.
- Revisar si: menos del 30 % de los posts con «$» da precio durante una semana, el feed empieza a bloquear o David lee unos términos que lo prohíban.
