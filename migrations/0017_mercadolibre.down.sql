-- Revierte 0017_mercadolibre: borra antes lo que referencia a la fuente (las alertas caen con
-- `listing`).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;
DROP TABLE ean_lookup;
ALTER TABLE catalog_item DROP COLUMN checked_at;
DELETE FROM watch WHERE source = 'mercadolibre';
DELETE FROM search WHERE source = 'mercadolibre';
DELETE FROM listing WHERE source = 'mercadolibre';
DELETE FROM catalog_item WHERE source = 'mercadolibre';
DELETE FROM source WHERE code = 'mercadolibre';
