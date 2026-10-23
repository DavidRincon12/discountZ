-- 0013_9to5toys: el RSS de 9to5Toys como canal «sin verificar» (ADR 0014).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

-- El código empieza por letra (CHECK de `source.code`): `ninetofivetoys`.
INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('ninetofivetoys','channel','USD',true,NULL);
