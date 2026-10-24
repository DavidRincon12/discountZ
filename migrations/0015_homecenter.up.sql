-- 0015_homecenter: Homecenter como tienda colombiana, leída por el JSON de sus categorías (ADR 0016).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('homecenter','store','COP',true,NULL);
