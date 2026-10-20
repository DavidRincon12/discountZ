-- 0007_alkosto: Alkosto como tienda colombiana, leída por su índice público de Algolia (ADR 0009).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('alkosto','store','COP',true,NULL);
