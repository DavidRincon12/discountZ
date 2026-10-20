-- 0009_min_seen_at: el mínimo de precios estables del catálogo vence a los 90 días (spec del
-- rastreo §3.1). Sin relleno: lo guardado empieza a contar en su próxima lectura estable.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

ALTER TABLE catalog_item ADD COLUMN min_seen_at timestamptz;
