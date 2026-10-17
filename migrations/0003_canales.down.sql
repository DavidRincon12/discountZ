-- Revierte 0003_canales.
SET LOCAL search_path = discountz;
DROP TABLE channel_post, channel_cursor;
DELETE FROM source WHERE kind = 'channel';
ALTER TABLE source DROP CONSTRAINT source_kind_check;
ALTER TABLE source ADD CONSTRAINT source_kind_check CHECK (kind IN ('store','feed'));
