-- Revierte 0018_semillas_por_ventanas: la marca de las semillas y su índice.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;
ALTER TABLE crawl_cursor
  DROP CONSTRAINT crawl_cursor_seed_ck,
  DROP COLUMN seed_item_id,
  DROP COLUMN seed_price_minor;
DROP INDEX catalog_item_seed_ix;
