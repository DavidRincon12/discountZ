# 0022. Repartir un presupuesto mensual de egress y apagar el rastreo al agotarlo

- Fecha: 2026-11-25
- Estado: Aceptada (extiende la 0021)
- Spec: `docs/superpowers/specs/2026-11-24-presupuesto-mensual-egress-design.md`

## Contexto
El 2026-11-24 Supabase restringió la organización: el ciclo de facturación 28-oct/28-nov iba en 11,97 GB de egress de los 5 del plan gratis. El exceso vino de antes de los arreglos del 21-nov (~700 MB al día); después bajó a 194, 157 y ~110 MB al día. El freno de la 0021 solo mira el día anterior y apunta a 132 MB/día: no sabe cuánto va del mes, así que una semana mala se acumula. David pidió que «nunca se termine llenando».

## Decisión
- `bot_state.egress_cycle_on` y `egress_cycle_rows` (migración `0021`) acumulan las filas que devolvió el rol desde el día 28 (fecha de Bogotá) en que Supabase reinicia el ciclo.
- Presupuesto del rastreo: 75 % del plan (3.840 MB). Meta del día = mín(3.840 ÷ 31, lo que queda ÷ días hasta el próximo 28): nunca más rápido que un reparto parejo, y más lento si ya se gastó de más.
- Presupuesto agotado ⇒ `crawl_pace_bp = 0` y el rastreo sale sin pedir nada hasta el ciclo nuevo, que lo vuelve a arrancar desde el 10 %. El 25 % restante (1,28 GB) cubre el ciclo de cada 2 min (~50 MB/día) durante 25 días.
- Avisos al grupo una vez por ciclo: al apagar el rastreo y al pasar el 90 % del plan. El ciclo nunca se detiene: sin él no corre el mantenimiento que reinicia el mes.
- La migración siembra el ciclo 2026-10-28 con lo ya gastado, así el rastreo queda apagado hasta el 28-nov.

## Alternativas consideradas
- **Bajar la meta diaria al 65 %:** una línea, pero los picos se siguen acumulando y el rastreo queda más lento todo el mes.
- **Leer el uso real con la API de facturación de Supabase:** exacto, pero exige un token de la cuenta en Actions y depende de una API más.
- **Detener también el ciclo al 90 %:** dejaría de correr el mantenimiento que reinicia el mes; descartada.

## Consecuencias
- El estimador (filas × 170 B) sobrestima un poco (164 contra 157 MB reales el 23-nov), lo que da margen.
- Se reacciona una vez al día: un pico de un solo día no se frena hasta el mantenimiento siguiente.
- Con el rastreo apagado no llegan ofertas del catálogo; los canales, seguimientos y búsquedas siguen.

## Cuándo revisar
- Se sostiene con (hoy): ~98 MB/día estimados, mitad rastreo (al 37 %) y mitad ciclo.
- Revisar si el ciclo solo pasa de 100 MB/día, si el aviso del 90 % sale o si Supabase vuelve a restringir.
