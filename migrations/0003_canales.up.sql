-- 0003_canales: canales públicos de ofertas de Telegram como fuente (spec de canales §6, ADR 0005).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

ALTER TABLE source DROP CONSTRAINT source_kind_check;
ALTER TABLE source ADD CONSTRAINT source_kind_check CHECK (kind IN ('store','feed','channel'));
INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('tg_descuentostech','channel','USD',true,NULL),
  ('tg_clubgratis','channel','USD',true,NULL),
  ('tg_cajitatech','channel','USD',true,NULL);

-- Último id leído de cada canal (el código fija `updated_at`).
CREATE TABLE channel_cursor (
  channel     text PRIMARY KEY REFERENCES source(code) ON DELETE CASCADE,
  last_id     bigint NOT NULL CHECK (last_id >= 0),
  updated_at  timestamptz NOT NULL
);

-- Ofertas leídas (7 días): deduplicación entre canales y resumen diario.
CREATE TABLE channel_post (
  channel       text NOT NULL REFERENCES source(code) ON DELETE CASCADE,
  post_id       bigint NOT NULL CHECK (post_id > 0),
  published_at  timestamptz NOT NULL,
  title_norm    text NOT NULL,
  price_minor   bigint NOT NULL CHECK (price_minor > 0),
  currency      currency_code NOT NULL,
  coupon        text,
  topic         text NOT NULL,
  duplicate_of  text,                                  -- «<canal>:<id>» de la primera
  seen_at       timestamptz NOT NULL,
  PRIMARY KEY (channel, post_id)                       -- cubre la FK
);
CREATE INDEX channel_post_recent ON channel_post (seen_at);   -- deduplicación y retención

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON channel_cursor, channel_post TO discountz_app;
  END IF;
END $$;
