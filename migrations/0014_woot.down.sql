-- Revierte 0014_woot; su cursor y sus publicaciones se borran en cascada.
SET LOCAL search_path = discountz;
DELETE FROM source WHERE code = 'woot';
