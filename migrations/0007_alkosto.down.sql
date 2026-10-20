-- Revierte 0007_alkosto: borra antes lo que referencia a la fuente (las alertas caen con `listing`).
SET LOCAL search_path = discountz;
DELETE FROM watch WHERE source = 'alkosto';
DELETE FROM search WHERE source = 'alkosto';
DELETE FROM listing WHERE source = 'alkosto';
DELETE FROM catalog_item WHERE source = 'alkosto';
DELETE FROM source WHERE code = 'alkosto';
