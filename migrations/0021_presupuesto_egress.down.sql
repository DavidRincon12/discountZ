-- 0021_presupuesto_egress (revertir): quita el acumulado del ciclo; el ritmo vuelve a 10-100 %.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

UPDATE bot_state SET crawl_pace_bp = greatest(crawl_pace_bp, 1000);

ALTER TABLE bot_state
  DROP COLUMN egress_cycle_on,
  DROP COLUMN egress_cycle_rows,
  DROP CONSTRAINT bot_state_crawl_pace_bp_check,
  ADD CONSTRAINT bot_state_crawl_pace_bp_check CHECK (crawl_pace_bp BETWEEN 1000 AND 10000);
