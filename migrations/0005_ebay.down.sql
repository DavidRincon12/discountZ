-- Revierte 0005_ebay: borra antes lo que referencia a la fuente y las alertas `model_low`.
SET LOCAL search_path = discountz;
DELETE FROM alert WHERE kind = 'model_low'
   OR listing_id IN (SELECT id FROM listing WHERE source = 'ebay_us');
DELETE FROM watch WHERE source = 'ebay_us';
DELETE FROM search WHERE source = 'ebay_us';
DELETE FROM listing WHERE source = 'ebay_us';
DELETE FROM catalog_item WHERE source = 'ebay_us';
DELETE FROM crawl_cursor WHERE source = 'ebay_us';
DELETE FROM source WHERE code = 'ebay_us';
DROP INDEX catalog_item_model_ix;
ALTER TABLE catalog_item DROP COLUMN model_key;
ALTER TABLE alert DROP CONSTRAINT alert_kind_check;
ALTER TABLE alert ADD CONSTRAINT alert_kind_check CHECK (kind IN
  ('price_error','historic_low','target_reached','good_deal','community_deal','catalog_drop'));
