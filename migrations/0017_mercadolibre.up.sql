-- 0017_mercadolibre: Mercado Libre como tienda colombiana, seguida por su catálogo y sembrada con
-- los EAN de las demás tiendas (ADR 0018).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('mercadolibre','store','COP',true,NULL);

-- Cada EAN buscado en una fuente sembrada: no se repite en 30 días (mantenimiento diario).
CREATE TABLE ean_lookup (
  source      text NOT NULL REFERENCES source(code) ON DELETE CASCADE,
  ean         text NOT NULL CHECK (ean ~ '^[0-9]{6,14}$'),
  checked_at  timestamptz NOT NULL,
  external_id text CHECK (external_id ~ '^[A-Za-z0-9]{1,40}$'),  -- NULL: no está en la fuente
  PRIMARY KEY (source, ean)                                         -- cubre la FK
);
CREATE INDEX ean_lookup_checked_ix ON ean_lookup (checked_at);       -- retención

-- Última lectura por id de lo que sigue una fuente sembrada, haya o no precio: lo no pedido en
-- 6 h se relee primero. Sin relleno: NULL cuenta como `last_seen_at`.
ALTER TABLE catalog_item ADD COLUMN checked_at timestamptz;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON ean_lookup TO discountz_app;
  END IF;
END $$;
