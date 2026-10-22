-- Revierte 0011_limpieza_catalogo: la tabla vuelve vacía; el catálogo borrado no vuelve (el
-- rastreo lo relee si la tienda se prende).
SET LOCAL search_path = discountz;

CREATE TABLE catalog_price_change (
  item_id     bigint NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
  at          timestamptz NOT NULL,
  price_minor money_minor NOT NULL,
  PRIMARY KEY (item_id, at)
);
CREATE INDEX catalog_price_change_at_ix ON catalog_price_change (at);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON catalog_price_change TO discountz_app;
  END IF;
END $$;
