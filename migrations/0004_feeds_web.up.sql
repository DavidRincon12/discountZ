-- 0004_feeds_web: DealNews y los enlaces a Amazon de Slickdeals como canales «sin verificar».
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

-- Son feeds RSS que no numeran en orden: se leen como canales sin cursor por id
-- (`ordered_ids = False` en el descriptor) y reutilizan `channel_cursor` y `channel_post`.
INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('dealnews','channel','USD',true,NULL),
  ('slickdeals_amazon','channel','USD',true,NULL);
