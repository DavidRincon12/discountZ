-- Revierte 0002_catalogo.
SET LOCAL search_path = discountz;
DELETE FROM alert WHERE kind = 'catalog_drop';
ALTER TABLE alert DROP CONSTRAINT alert_kind_check;
ALTER TABLE alert ADD CONSTRAINT alert_kind_check CHECK (kind IN
  ('price_error','historic_low','target_reached','good_deal','community_deal'));
ALTER TABLE watch DROP COLUMN expires_at, DROP COLUMN origin;
ALTER TABLE outbox DROP COLUMN card, DROP COLUMN topic;
DROP TABLE topic, crawl_lease, crawl_cursor, catalog_price_change, catalog_item;
