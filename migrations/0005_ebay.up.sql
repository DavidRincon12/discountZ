-- 0005_ebay: eBay por la Browse API (ADR 0008).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('ebay_us','store','USD',true,NULL);

-- Modelo de eBay (`epid`) para comparar una publicación con las demás del mismo modelo.
ALTER TABLE catalog_item ADD COLUMN model_key text CHECK (model_key ~ '^[0-9]{4,15}$');
CREATE INDEX catalog_item_model_ix ON catalog_item (source, model_key, last_seen_at DESC)
  WHERE model_key IS NOT NULL;

ALTER TABLE alert DROP CONSTRAINT alert_kind_check;
ALTER TABLE alert ADD CONSTRAINT alert_kind_check CHECK (kind IN
  ('price_error','historic_low','target_reached','good_deal','community_deal','catalog_drop',
   'model_low'));
