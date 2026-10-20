-- 0008_jumbo_pepeganga: Jumbo y Pepe Ganga como tiendas colombianas, leídas por VTEX como Éxito.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('jumbo','store','COP',true,NULL), ('pepeganga','store','COP',true,NULL);
