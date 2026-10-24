-- Revierte 0015_homecenter: borra antes lo que referencia a la fuente (las alertas caen con `listing`).
SET LOCAL search_path = discountz;
DELETE FROM watch WHERE source = 'homecenter';
DELETE FROM search WHERE source = 'homecenter';
DELETE FROM listing WHERE source = 'homecenter';
DELETE FROM catalog_item WHERE source = 'homecenter';
DELETE FROM source WHERE code = 'homecenter';
