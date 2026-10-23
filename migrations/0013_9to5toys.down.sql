-- Revierte 0013_9to5toys: `channel_cursor` y `channel_post` se borran en cascada.
SET LOCAL search_path = discountz;
DELETE FROM source WHERE code = 'ninetofivetoys';
