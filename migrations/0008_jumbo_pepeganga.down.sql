-- Revierte 0008_jumbo_pepeganga: borra antes lo que referencia a las fuentes (las alertas caen con `listing`).
SET LOCAL search_path = discountz;
DELETE FROM watch WHERE source IN ('jumbo', 'pepeganga');
DELETE FROM search WHERE source IN ('jumbo', 'pepeganga');
DELETE FROM listing WHERE source IN ('jumbo', 'pepeganga');
DELETE FROM catalog_item WHERE source IN ('jumbo', 'pepeganga');
DELETE FROM source WHERE code IN ('jumbo', 'pepeganga');
