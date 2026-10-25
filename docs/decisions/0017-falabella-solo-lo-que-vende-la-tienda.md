# 0017. Rastrear Falabella solo en lo que vende la propia tienda

- Fecha: 2026-10-24
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-24-falabella-design.md` (§0.2 F1-F4, §1 y §2)

## Contexto
Falabella (`falabella.com.co`, COP) mezcla su catálogo con el de cientos de vendedores del marketplace (en Computadores, 232 de 39.723 productos son de la tienda). David pidió solo «Vendido por FALABELLA». Su web es Next.js: cada listado trae 48 productos (más hasta 8 patrocinados) en `__NEXT_DATA__`. Medido el 2026-10-24 desde la nube y desde Actions: 200 sin desafío; `robots.txt` solo cierra la cesta y la cuenta; sus términos no mencionan lectores automáticos.

## Decisión
- Fuente `falabella` (`CrawlSource`, COP) en `sources/falabella.py`. Se lee `/falabella-co/category/<clave>/<slug>?f.derived.variant.sellerId=FALABELLA&page=N`; el filtro de vendedor del propio sitio deja solo lo que vende la tienda, así que el catálogo cabe en ~9.800 productos (~215 páginas) y no se acerca al corte de 200 páginas del listado.
- Se vuelve a comprobar por producto (`sellerName` del listado y `offerings[].sellerId = FALABELLA_COLOMBIA` en la ficha): lo que no sea de la tienda se descarta (`Homecenter` ya tiene su propia fuente).
- Precio = el menor de `internetPrice` y `eventPrice` (sin tachar); `cmrPrice` (exige la tarjeta) y el «antes» tachado nunca se leen. Con dos valores (variantes de talla o color) se toma el menor.
- 14 particiones fijas de categorías del perfil (tecnología, electrohogar, moda, relojes y papelería); sin capa caliente ni `/buscar`.
- Si el filtro deja un solo producto, Falabella responde con la ficha en lugar del listado: es una página vacía, no un error.

## Alternativas consideradas
- Listar cada categoría completa y descartar el marketplace en la confirmación: 40 mil productos solo en Computadores, ~800 páginas por categoría y el corte de 200 páginas · descartada.
- API interna del listado (`falabella-listing-bff`): no es pública · descartada.
- Spike: ¿responde Falabella con el filtro de vendedor desde Actions y la nube? · criterio: 200 y `results` en todas las particiones · medido 200 en 13 de 14 (la 14.ª devuelve la ficha cuando solo hay 1 producto) · sí.

## Consecuencias
- Cada vuelta pesa ≈ 480 MB de HTML; con 700 peticiones al día son ≈ 3 vueltas, gratis en Actions.
- El riesgo de términos es de David (como Alkosto y Homecenter): si Falabella bloquea o reclama, `/fuente off falabella`.
- Sin EAN: no cruza con otras tiendas.

## Cuándo revisar
- Se sostiene con (hoy): ~9.800 productos propios, ~215 páginas por vuelta, 700 peticiones al día.
- Revisar si: `__NEXT_DATA__` cambia de forma, Falabella pone un desafío, el filtro de vendedor deja de funcionar o la BD pasa de 450 MB.
## Ajuste (2026-10-24)

La primera vuelta real (3 s entre peticiones) pasó con 20 peticiones, pero la siguiente recibió `403` desde Actions tras 6 páginas y abrió el interruptor. Se baja el ritmo a 10 s (±5) y 60 peticiones por corrida; el cursor reanuda donde quedó. No se evade ninguna protección: si vuelve el `403`, `/fuente off falabella`.
