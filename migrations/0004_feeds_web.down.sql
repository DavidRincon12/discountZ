-- Revierte 0004_feeds_web; su cursor y sus publicaciones se borran en cascada.
SET LOCAL search_path = discountz;
DELETE FROM source WHERE code IN ('dealnews','slickdeals_amazon');
ALTER TABLE channel_post DROP COLUMN baseline;
