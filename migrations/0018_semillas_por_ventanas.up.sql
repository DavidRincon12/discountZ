-- 0018_semillas_por_ventanas: las semillas de Mercado Libre se leen por ventanas de un índice
-- parcial, del más caro al más barato, desde la marca que dejó la corrida anterior (ADR 0019).
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '10min';  -- el índice lee todo el catálogo; las escrituras esperan
SET LOCAL search_path = discountz;

-- Solo lo que puede ser semilla: en pesos, con EAN y desde 100.000 (`db.SEED_FLOOR_MINOR`).
-- `(price_minor, id)` es único: sirve de marca exacta entre ventanas.
CREATE INDEX catalog_item_seed_ix ON catalog_item (price_minor, id)
  WHERE currency = 'COP' AND ean IS NOT NULL AND price_minor >= 100000;

-- Última fila leída por las semillas; NULL: la próxima vuelta empieza por la más cara.
ALTER TABLE crawl_cursor
  ADD COLUMN seed_price_minor money_minor,
  ADD COLUMN seed_item_id     bigint,
  ADD CONSTRAINT crawl_cursor_seed_ck CHECK ((seed_price_minor IS NULL) = (seed_item_id IS NULL));
