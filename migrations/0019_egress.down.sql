-- Revierte 0019_egress: la marca del medidor y la regla original de las alertas en cola.
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;
ALTER TABLE bot_state DROP COLUMN select_rows_at, DROP COLUMN select_rows_total;
-- Las enviadas cuya outbox ya se purgó no caben en la regla original: quedan suprimidas.
UPDATE alert SET delivery = 'suppressed' WHERE delivery = 'queued' AND outbox_id IS NULL;
ALTER TABLE alert DROP CONSTRAINT alert_queued_ck;
ALTER TABLE alert ADD CONSTRAINT alert_queued_ck CHECK ((delivery = 'queued') = (outbox_id IS NOT NULL));
