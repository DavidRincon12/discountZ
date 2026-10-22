-- Revierte 0010_tope_diario.
SET LOCAL search_path = discountz;
DROP TABLE offer_log;
UPDATE alert SET delivery = 'suppressed' WHERE delivery = 'held';
ALTER TABLE alert DROP CONSTRAINT alert_delivery_check;
ALTER TABLE alert ADD CONSTRAINT alert_delivery_check
  CHECK (delivery IN ('new','queued','muted','suppressed'));
