# Presupuesto mensual de egress (diseño corto)

- Fecha: 2026-11-24
- Pedido de David: «haz todo lo que esté a tu alcance para que no se repita» (restricción de Supabase por egress, 11,97 de 5 GB en el ciclo 28-oct/28-nov).

## Por qué
El freno del ADR 0021 (`next_pace`) solo mira el gasto del día anterior y apunta a 132 MB/día (80 % de 5 GB en 31 días). No sabe cuánto va del ciclo de facturación: una semana mala (el rastreo redescubriendo el catálogo, un bug como el del resumen de la noche) se acumula y nada la compensa. Medido el 2026-11-25: ~98 MB/día (mitad rastreo al 37 %, mitad ciclo), así que hoy el freno alcanza; falta la protección contra los picos.

## Metas y no metas
- Meta: que el mes de Supabase (del 28 al 28) no pase del 80 % del plan aunque haya días malos; si un día se gasta de más, los siguientes frenan en proporción.
- No meta: bajar el gasto base del ciclo (cada 2 min) ni cambiar el estimador (filas × 170 B, que sobrestima un poco: 164 estimado contra 157 real el 23-nov).
- Anti-meta: apagar el rastreo del todo (el piso del 10 % se mantiene) o depender de la API de facturación de Supabase.

## Enfoques
1. **Presupuesto mensual (elegido, recomendado):** el mantenimiento acumula las filas devueltas desde el día 28 y la meta del día pasa a ser (80 % del plan − lo gastado) ÷ días que faltan para el próximo 28. Corrige solo cualquier pico.
2. Bajar la meta diaria al 65 %: una línea, pero los picos igual se acumulan y el rastreo queda más lento todo el mes.
3. Dejarlo como está: ~3,4 GB al mes hoy, sin protección contra un pico.

## Diseño
- Migración `0021`: `bot_state.egress_cycle_on date` (inicio del ciclo al que pertenece el acumulado) y `bot_state.egress_cycle_rows bigint NOT NULL DEFAULT 0 CHECK (≥ 0)`.
- `app/maintenance.py`, funciones puras: `billing_cycle_start(día)` = el último día 28 ≤ día (constante `EGRESS_CYCLE_DAY = 28`); `cycle_target_mb(usado_mb, día)` = max(0, 80 % del plan − usado) ÷ días hasta el próximo 28 (mínimo 1 día); `next_pace` recibe la meta en vez de usar la constante.
- `_measure_egress`: suma `total − anterior` al acumulado (0 si no hay lectura anterior o se reiniciaron las estadísticas); si el ciclo cambió, el acumulado vuelve a empezar con esa suma. Gastado todo el presupuesto, la meta es 0 y el ritmo cae al piso (10 %).
- Aviso al grupo: el de siempre (≥ 80 % al día) suma «~X GB del ciclo»; y otro, una vez por ciclo, al pasar el 80 % del plan.
- Log del mantenimiento: `egress_cycle_mb=` (lista blanca de `infra/log.py`).
- ADR 0022 corto que extiende el 0021.

## Decisiones mías (corregibles)
- El ciclo empieza el día 28 en la fecha de Bogotá: el mantenimiento corre a las 3 a. m. de Bogotá (08:00Z), ya dentro del día 28 UTC en que Supabase reinicia.
- Para lo que queda de este ciclo (hasta el 28-nov) el acumulado arranca en 0 al desplegar: el ciclo actual ya está perdido y se reinicia en 3 días.
