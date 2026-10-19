-- 0006_canales_colombia: dos canales públicos más de Telegram (DescuentosHome y OFERTU).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('tg_descuentoshome','channel','USD',true,NULL),
  ('tg_ofertu','channel','USD',true,NULL);
