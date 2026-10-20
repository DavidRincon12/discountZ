# 0011. Publicar el enlace de la tienda sin el referido del canal

- Fecha: 2026-10-20
- Estado: Aceptada

## Contexto
Las ofertas de canales de Telegram (ADR 0005) salen con el enlace que puso el canal. David quiere ver el enlace real de la tienda y, más adelante, poner su propio código de afiliado. Revisado el 2026-10-20 con ofertas reales, canal por canal:
- Clubgratis y OFERTU: `joylink.io/…` redirige por HTTP (30x, sin JavaScript) a `amazon.com/dp/<ASIN>` con el código del canal (`tag=clubgratis203-20`; OFERTU, Amazon Attribution `maas`).
- cajitatech: `pccajita.link` lleva a promocajita.com, que arma el enlace a la tienda con JavaScript y una API privada al pulsar «Ir a la oferta».
- DescuentosTech y DescuentosHome: el post solo enlaza a Facebook, que pide sesión y cuyos términos prohíben la recolección automática.

## Decisión
`app/links.py` sigue solo los saltos de `joylink.io` (hasta 3), leyendo la cabecera `Location` sin descargar nunca la tienda. Una ficha de Amazon queda como `https://<host>/dp/<ASIN>` sin parámetros (`domain/urls.amazon_product_url`); otra tienda permitida, sin los parámetros de rastreo de `SafeUrl`. Si algo falla (red, estado, host fuera de la lista blanca, bucle), la oferta sale con el enlace del canal y se registra `link_unresolved`. Se resuelve solo lo nuevo de un canal con orden de ids, fuera de la transacción de la BD, con el transporte sin redirecciones de las fotos.

## Alternativas consideradas
- Dejar el enlace del canal: sin trabajo, pero no muestra la tienda ni deja poner un código propio.
- Conservar el código del canal: respeta su ingreso; David prefirió quitarlo para poner luego el suyo.
- cajitatech con navegador sin cabeza o su API privada: frágil y en zona gris. Descartado.
- DescuentosTech y DescuentosHome vía Facebook: exige sesión y lo prohíben sus términos. Descartado.

## Consecuencias
Una petición a joylink.io por oferta nueva de Clubgratis u OFERTU (decenas al día). Los canales pierden su comisión en las compras de David y pueden ver menos clics atribuidos; sus condiciones no se revisaron. Pendiente: el código de afiliado propio cuando David lo tenga.

## Cuándo revisar
- Si `link_unresolved` aparece en la mayoría de las ofertas de Clubgratis u OFERTU (joylink cambió o bloquea).
- Cuando David tenga su código de afiliado de Amazon.
