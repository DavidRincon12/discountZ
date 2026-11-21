-- 0020_indices_y_ritmo (revertir): quita el ritmo; los índices reconstruidos se quedan.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

ALTER TABLE bot_state DROP COLUMN crawl_pace_bp;
