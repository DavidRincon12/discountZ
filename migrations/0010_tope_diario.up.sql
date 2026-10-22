-- 0010_tope_diario: tope de tarjetas por fuente al día y resumen de la noche (ADR 0012).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

-- Alerta del rastreo que no salió en tarjeta (tope del día o misma promoción): va al resumen.
ALTER TABLE alert DROP CONSTRAINT alert_delivery_check;
ALTER TABLE alert ADD CONSTRAINT alert_delivery_check
  CHECK (delivery IN ('new','queued','muted','suppressed','held'));

-- Ofertas del rastreo y de los canales (7 días): las que salieron en tarjeta cuentan para el
-- tope y la promoción; las demás esperan el resumen de la noche con su línea ya armada.
CREATE TABLE offer_log (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source       text NOT NULL REFERENCES source(code) ON DELETE CASCADE,
  day          date NOT NULL,                       -- día de Bogotá del tope
  topic        text NOT NULL,
  kind         text NOT NULL,
  drop_bp      integer,                             -- ahorro frente al «antes»; NULL en canales
  carded       boolean NOT NULL,
  line         text,
  created_at   timestamptz NOT NULL,
  digested_at  timestamptz,
  CONSTRAINT offer_log_line_ck CHECK (carded OR line IS NOT NULL)
);
CREATE INDEX offer_log_source_ix ON offer_log (source, day);                   -- cubre la FK
CREATE INDEX offer_log_held_ix ON offer_log (created_at) WHERE NOT carded AND digested_at IS NULL;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON offer_log TO discountz_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA discountz TO discountz_app;
  END IF;
END $$;
