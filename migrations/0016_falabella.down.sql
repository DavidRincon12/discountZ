-- Revierte 0016_falabella: borra antes lo que referencia a la fuente (las alertas caen con `listing`).
SET LOCAL search_path = discountz;
DELETE FROM watch WHERE source = 'falabella';
DELETE FROM search WHERE source = 'falabella';
DELETE FROM listing WHERE source = 'falabella';
DELETE FROM catalog_item WHERE source = 'falabella';
DELETE FROM source WHERE code = 'falabella';
