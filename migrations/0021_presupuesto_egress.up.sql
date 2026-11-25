-- 0021_presupuesto_egress: acumulado de las filas devueltas en el ciclo de facturación de
-- Supabase (del 28 al 28) para repartir un presupuesto del 75 % del plan, y ritmo 0 para apagar
-- el rastreo al agotarlo (ADR 0022). Se siembra el ciclo 2026-10-28 con lo que el panel marcaba
-- el 2026-11-25 (11,97 GB ≈ 75,6 millones de filas a 170 B): el rastreo queda apagado hasta el 28.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

ALTER TABLE bot_state
  ADD COLUMN egress_cycle_on date,
  ADD COLUMN egress_cycle_rows bigint NOT NULL DEFAULT 0 CHECK (egress_cycle_rows >= 0),
  DROP CONSTRAINT bot_state_crawl_pace_bp_check,
  ADD CONSTRAINT bot_state_crawl_pace_bp_check CHECK (crawl_pace_bp BETWEEN 0 AND 10000);

UPDATE bot_state SET egress_cycle_on = DATE '2026-10-28', egress_cycle_rows = 75600000;
