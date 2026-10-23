# 9to5Toys como fuente «sin verificar» (diseño)

- Fecha: 2026-10-23
- Estado: aprobado. David aprobó el diseño corto del hilo «Fuente 9to5Toys» y pidió avanzar sin esperarlo (opciones recomendadas, anotadas aquí).
- Sub-proyecto 2 de «Mejoras mayores del bot» (después del perfil y los intereses, ADR 0013).

## 1. Objetivo
Sumar ofertas de tecnología de EE. UU. compradas por casillero desde el RSS de 9to5Toys, sin leer las tiendas y sin cuentas ni claves. Éxito: tarjetas de 9to5Toys con precio correcto (nunca el ahorro tomado como precio), filtradas por el perfil, el mínimo de US$25 y el tope de 8 por fuente al día.

## 2. Lo medido (2026-10-23, desde la nube)
- `https://9to5toys.com/feed/` responde 200 (`application/rss+xml`, ~120 KB) con 50 ítems: algo más de un día de publicaciones (unas 45 al día).
- `robots.txt` no cierra `/feed/` ni `/wp-content/uploads/` (solo `wpforms` y la búsqueda interna). Sus términos viven en 9to5mac.com, bloqueado por el proxy de la sesión: no se pudieron leer (queda en el ADR como riesgo).
- No hay campo de precio: el precio va en el título («… now $16», «… at $600 (Save $350)»), y el título también trae el ahorro («AirPods Pro 3 at $50 off») o el precio de antes («(Reg. $30)»).
- `<category>` solo dice `Deals`, `News` o `Steals`: no sirve para el tema.
- El id del post va en el `<guid>` (`https://9to5toys.com/?p=1238275`); el `<link>` es el artículo; la foto, un `<img>` de `9to5toys.com/wp-content/uploads/…` al inicio de la `<description>`.
- Los enlaces a las tiendas son acortadores de afiliado (`link.amazon`, `howl.link`, `sovrn.co`, `*.sjv.io`, `bestbuycreators.7tiv.net`…).

## 3. Diseño
Molde de DealNews (ADR 0006): una fuente `channel` más, `ninetofivetoys` (el código debe empezar por letra), leída por el paso de canales.

- **Lectura:** una petición al feed por lectura, cada 10 min (`[sources.ninetofivetoys]`, tope de 200 al día). `ordered_ids = False`: el feed se relee entero y lo ya guardado se salta por id; la primera lectura con ofertas queda como `baseline` sin enviar. `refeatures = False`: vale el tope de 6 h de los canales.
- **Precio (`_price(title)`, puro):** de los montos `$N` del título se descartan los que siguen de «off», «savings», «discount», «less», «credit», «gift» o «rebate», y los que van después de «Reg.», «regularly», «was», «save», «saving», «list», «MSRP», «worth», «value» o «up to». Si algún monto restante va después de «from», «starting», «under» o «as low as», el post es un resumen y no tiene precio. Si no, el precio es el primer monto restante. Sin precio, el ítem se salta en silencio (las noticias y los resúmenes son normales, no un formato roto).
- **Post:** `title` (máx. 200), `link` y `post_url` = el artículo (le da el crédito a 9to5Toys, no se toca el enlace de afiliado), `image_url` = el primer `<img>` de `9to5toys.com`, `store` = «Amazon» si el primer enlace de tienda de la descripción es de Amazon (`link.amazon`, `amzn.to`, `www.amazon.com`), si no `None`; `hashtags = ()` (el tema sale del título con `channel_rules`); `coupon` del texto («code XYZ»), igual que DealNews; `casillero = True`.
- **Errores:** XML ilegible, no RSS o sin ítems ⇒ `ParseError` (el breaker pausa la fuente). Un ítem sin id, enlace, título o fecha cuenta en `unparsed`.
- **Tarjeta:** nombre «9to5Toys», color propio (`#0F766E`), «Visto en 9to5Toys · sin verificar», como las demás fuentes sin verificar.
- **Migración `0013`:** inserta `ninetofivetoys` (`channel`, USD, encendida); el `down` la borra (cursor y posts en cascada). `SCHEMA_VERSION = 13`.
- **Apagado:** `/fuente off ninetofivetoys`.

## 4. Alternativas
- Seguir el enlace a la tienda y verificar el precio: los acortadores de afiliado redirigen a Amazon (CAPTCHA) o a tiendas con WAF · descartada.
- Leer cada artículo para un precio estructurado: 45 peticiones más al día y el artículo tampoco trae un campo de precio · descartada.
- Usar la categoría de WordPress para el tema: solo dice Deals/News/Steals · descartada.

## 5. Pruebas
- Fixture real `tests/fixtures/ninetofivetoys/feed.xml.gz` (grabada el 2026-10-23).
- Unitarias de `_price` con títulos reales (ahorro, «Reg.», «from», «or get two for», `$1,379`), del parseo del feed real (ids, enlaces, fotos, casillero, sin noticias), de errores y de hypothesis (nunca revienta).
- Registro, presupuesto en `config`, nombre y color, migración (conteo de fuentes y up/down) y un e2e del ciclo: primera lectura como `baseline`, la siguiente envía la oferta nueva.

## 6. Fuera de alcance
Woot y Mercado Libre (cada uno en su hilo).
