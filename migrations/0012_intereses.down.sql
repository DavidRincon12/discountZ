-- Revierte 0012_intereses.
SET LOCAL search_path = discountz;
ALTER TABLE offer_log DROP COLUMN interest;
DROP TABLE interest;
