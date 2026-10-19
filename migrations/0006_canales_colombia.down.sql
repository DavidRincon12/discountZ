-- Revierte 0006_canales_colombia: `channel_cursor` y `channel_post` se borran en cascada.
SET LOCAL search_path = discountz;
DELETE FROM source WHERE code IN ('tg_descuentoshome','tg_ofertu');
