-- 0019_egress: el mantenimiento ya no choca con la purga de la outbox y queda la marca del
-- medidor de egress (ADR 0020).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

-- La outbox se purga a los 30 días y `ON DELETE SET NULL` deja la alerta en `queued` sin
-- mensaje: sigue enviada (con `new` se reenviaría). Solo `queued` puede apuntar a la outbox.
ALTER TABLE alert DROP CONSTRAINT alert_queued_ck;
ALTER TABLE alert ADD CONSTRAINT alert_queued_ck CHECK (outbox_id IS NULL OR delivery = 'queued');

-- Última lectura de `pg_stat_statements` (filas de los SELECT del rol) para estimar el egress.
ALTER TABLE bot_state
  ADD COLUMN select_rows_total bigint CHECK (select_rows_total >= 0),
  ADD COLUMN select_rows_at    timestamptz;
