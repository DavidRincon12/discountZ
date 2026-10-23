# 0015. Leer Woot por su API como canal «sin verificar»

- Fecha: 2026-10-23
- Estado: Aceptada

## Contexto
David quiere más ofertas de tecnología de EE. UU. que compensen por casillero. Woot (de Amazon) tiene API oficial (`developer.woot.com`, clave `x-api-key`, 1 petición por segundo y 1.000 al día) con las ofertas vivas por categoría: precio de venta y de lista, fechas, foto y si están agotadas. Sus ofertas duran pocos días. Diseño en `docs/superpowers/specs/2026-10-23-woot-design.md`.

## Decisión
Woot entra como canal sin orden de ids (ADR 0006): cada 15 min se leen los feeds Computers, Electronics y Tools (3 peticiones, ≈ 288 al día), lo ya guardado se salta por id (63 bits del `OfferId`) y la primera lectura deja todo visto. Las ofertas salen «sin verificar» con «🇺🇸 Compra por casillero» y pasan por el perfil, los intereses, el mínimo de US$25 y el tope diario. Solo nuevas (como eBay); agotadas, solo en la app y vencidas se saltan. Los canales reciben ahora las credenciales del ciclo.

## Alternativas consideradas
- Rastreo de catálogo con histórico (como eBay): las ofertas viven menos que las 24 h de «antes» estable que pide la alerta y releer gastaría la cuota · revertir: medio.
- Feed `All`: trae Home, Shirts, Gourmet y Sports, que el perfil esconde, con más peticiones · revertir: bajo.
- Usar el precio de lista como «antes»: viene de Amazon y puede estar inflado (como el `ListPrice` de VTEX) · revertir: bajo.

## Consecuencias
Se gana una fuente oficial de EE. UU. con poco código (reutiliza canales, dedupe, temas y tope). Se sacrifica el histórico propio: la tarjeta no dice cuánto bajó. Pendiente: grabar la fixture real con la clave desde Actions (la nube de la sesión no la tiene).

## Cuándo revisar
- Se sostiene con (hoy): 3 feeds, ≈ 288 peticiones al día de 1.000, un puñado de ofertas nuevas al día.
- Revisar si: Woot cambia la cuota o el formato, o si sus tarjetas pasan seguido el tope de 8 al día (subir el umbral o cambiar los feeds).
