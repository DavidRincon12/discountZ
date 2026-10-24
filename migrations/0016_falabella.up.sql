-- 0016_falabella: Falabella como tienda colombiana, solo lo que vende la tienda (ADR 0017).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('falabella','store','COP',true,NULL);
