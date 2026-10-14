# 0002. Guardar el histórico por segmentos y validar ofertas con estadísticos ponderados por tiempo

- Fecha: 2026-10-13
- Estado: Aceptada
- Spec: `docs/superpowers/specs/2026-10-13-discountz-v1-design.md` (§2, §3; ADR 002, 003, 004, 007, 011 y 012 de §9)

## Contexto
Las tiendas inflan el precio tachado y los descuentos anunciados; una alerta útil exige comparar contra un histórico propio. El histórico debe caber en 500 MB (plan gratuito de Supabase), distinguir relámpagos de precios reales, resistir errores de parseo (precios ×10 o ÷10) y no duplicar avisos.

## Decisión
- **Histórico en segmentos de estado constante** (`price_segment`: precio, stock, `seller_class`, `unit_key`, `status o/s/g`), PK `(listing_id, started_at)` y un solo segmento abierto garantizado por índice único parcial. Unas 10 veces menos filas que una por lectura y ~115 MB el año 1 (ADR 002).
- **Referencia conservadora** `R = mín(M90, M30, M_prior)` con cuantil inferior ponderado por segundos observados; **rareza** `F(P) ≤ F_max` obligatoria para `GOOD_DEAL`; umbrales que se adaptan a la volatilidad propia solo con ≥ 60 d de cobertura; **ancla de tendencia** contra escaleras descendentes. Todo en enteros (unidad menor, segundos, puntos básicos) (ADR 003).
- **Confirmación independiente** (otro endpoint u otro parser) frente a **persistencia** (releer la misma ruta). El patrón 10^k nunca alerta sin ruta independiente; la tormenta (≥ 2 candidatos por fuente y ciclo) suprime o agrupa (ADR 004).
- **Outbox único** para todo mensaje y `alert` como hecho de dominio con `UNIQUE (listing_id, segment_started_at, kind)`: efecto exactamente una vez, envío al menos una vez (ADR 007).
- **Categorías** como `DOMAIN` con `CHECK` y parámetros en el TOML público (ADR 011).
- **Identidad a nivel de variante**; URL canónica reconstruida desde un id validado por regex; `SafeUrl` con host exacto y redirecciones manuales (ADR 012).

## Alternativas consideradas
- Mínimo diario (`price_daily`): pocas filas · no pondera por tiempo ni distingue relámpagos · revertir: alto (modelo central).
- Una fila por lectura: simple · ~10× más volumen y sesgo por muestreo · revertir: alto.
- Mediana con decaimiento exponencial: suave · cede ante inflación previa (U01-U02) · revertir: medio.
- `P ≤ P10`: simple · avisa promociones recurrentes (U27-U28).
- Releer la misma ficha como confirmación: barato · confirma su propio error de parseo (U12).
- `alerts` como outbox y respuestas directas: menos tablas · la retención borra la memoria del cooldown.
- Tabla `category`: flexible · más sincronización; cambia rara vez.
- Identidad por slug de producto o `"exito.com" in url`: simple · falsos positivos entre SKU (U25), SSRF e inyección de ruta.

## Consecuencias
- Las pruebas U01-U37 y D01-D06 (spec §3.11) fijan el comportamiento del algoritmo; las propiedades con Hypothesis lo blindan.
- Falsos negativos aceptados: promociones que ocupan > 10 % del tiempo (U33) y mercados al alza (`R = mín`).
- Una tienda sin ruta independiente nunca confirma un patrón 10^k (U12).

## Cuándo revisar
- Se sostiene con (hoy): ~900 segmentos al día, ~115 MB el año 1 y ~180 MB el año 2.
- Revisar si: la BD pasa de 300 MB (resumir segmentos > 400 d a mínimos semanales); o se reportan más de 3 falsos negativos en una categoría (modo de referencia `recent`).
