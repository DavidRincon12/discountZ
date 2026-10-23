-- 0014_woot: Woot como canal «sin verificar» de EE. UU. (ADR 0015).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

-- Su API no numera las ofertas en orden: se lee como canal sin cursor por id
-- (`ordered_ids = False`) y reutiliza `channel_cursor` y `channel_post`.
INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('woot','channel','USD',true,NULL);
