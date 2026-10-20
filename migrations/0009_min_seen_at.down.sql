-- Revierte 0009_min_seen_at.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

ALTER TABLE catalog_item DROP COLUMN min_seen_at;
