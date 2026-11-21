-- 0020_indices_y_ritmo: reconstruye los índices de `catalog_item`, hinchados porque cada lectura
-- del rastreo movía `last_seen_at` (el 2026-11-21 `catalog_item_seen_ix` pesaba 125 MB para
-- 708 mil filas), y agrega el ritmo del rastreo que ajusta el mantenimiento según el egress
-- (ADR 0021). Sin CONCURRENTLY: no corre dentro de la transacción del migrador; bloquea las
-- escrituras del catálogo mientras reconstruye (uno o dos minutos).
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '10min';
SET LOCAL search_path = discountz;

REINDEX INDEX catalog_item_seen_ix;
REINDEX INDEX catalog_item_uq;
REINDEX INDEX catalog_item_pkey;
REINDEX INDEX catalog_item_changed_ix;
REINDEX INDEX catalog_item_ean_ix;
REINDEX INDEX catalog_item_seed_ix;
REINDEX INDEX catalog_item_model_ix;

ALTER TABLE bot_state
  ADD COLUMN crawl_pace_bp smallint NOT NULL DEFAULT 10000
    CHECK (crawl_pace_bp BETWEEN 1000 AND 10000);
