-- 0002_catalogo: rastreo del catálogo completo, temas de Telegram y seguimientos automáticos
-- (spec del rastreo §7.1 y §4.4).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

-- ───────────── Producto del catálogo: solo números (título y foto se leen al alertar)
CREATE TABLE catalog_item (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source           text NOT NULL REFERENCES source(code) ON DELETE RESTRICT,
  external_id      text NOT NULL CHECK (external_id ~ '^[A-Za-z0-9]{1,40}$'),
  ean              text CHECK (ean ~ '^[0-9]{6,14}$'),
  topic            text NOT NULL CHECK (topic ~ '^[a-z_]{2,20}$'),
  currency         currency_code NOT NULL,
  price_minor      money_minor NOT NULL,
  usual_minor      money_minor,                    -- precio estable 24 h y 2 lecturas («antes»)
  min_seen_minor   money_minor,                    -- mínimo de precios estables
  reads_at_price   integer NOT NULL DEFAULT 1 CHECK (reads_at_price >= 1),
  first_seen_at    timestamptz NOT NULL,
  stable_since     timestamptz NOT NULL,
  last_changed_at  timestamptz,
  last_seen_at     timestamptz NOT NULL,
  last_alert_at    timestamptz,
  last_alert_minor money_minor,
  CONSTRAINT catalog_item_uq       UNIQUE (source, external_id),          -- cubre la FK
  CONSTRAINT catalog_item_alert_ck CHECK ((last_alert_at IS NULL) = (last_alert_minor IS NULL))
) WITH (fillfactor = 80);
CREATE INDEX catalog_item_changed_ix ON catalog_item (source, last_changed_at DESC)
  WHERE last_changed_at IS NOT NULL;
CREATE INDEX catalog_item_ean_ix  ON catalog_item (ean) WHERE ean IS NOT NULL;
CREATE INDEX catalog_item_seen_ix ON catalog_item (last_seen_at);                  -- retención

CREATE TABLE catalog_price_change (
  item_id     bigint NOT NULL REFERENCES catalog_item(id) ON DELETE CASCADE,
  at          timestamptz NOT NULL,
  price_minor money_minor NOT NULL,
  PRIMARY KEY (item_id, at)                                                          -- cubre la FK
);
CREATE INDEX catalog_price_change_at_ix ON catalog_price_change (at);                -- retención

-- ───────────── Cursor y lease del rastreo, uno por fuente
CREATE TABLE crawl_cursor (
  source           text PRIMARY KEY REFERENCES source(code) ON DELETE CASCADE,
  sweep_no         integer NOT NULL DEFAULT 0 CHECK (sweep_no >= 0),
  partition_idx    integer NOT NULL DEFAULT 0 CHECK (partition_idx >= 0),
  page             integer NOT NULL DEFAULT 0 CHECK (page >= 0),
  sweep_started_at timestamptz,
  last_sweep_s     integer CHECK (last_sweep_s >= 0),
  last_run_at      timestamptz,
  changed_since    timestamptz,
  partitions       jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(partitions) = 'array'),
  day_start        date,
  day_requests     integer NOT NULL DEFAULT 0 CHECK (day_requests >= 0),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE crawl_lease (
  source     text PRIMARY KEY REFERENCES source(code) ON DELETE CASCADE,
  holder     text NOT NULL CHECK (length(holder) <= 80),
  expires_at timestamptz NOT NULL
);

-- ───────────── Temas del grupo de Telegram
CREATE TABLE topic (
  code       text PRIMARY KEY CHECK (code ~ '^[a-z_]{2,20}$'),
  title      text NOT NULL CHECK (length(title) BETWEEN 1 AND 60),
  thread_id  bigint,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────── Cambios a tablas de la v1
ALTER TABLE outbox ADD COLUMN topic text CHECK (topic ~ '^[a-z_]{2,20}$'),
                   ADD COLUMN card  jsonb CHECK (card IS NULL OR jsonb_typeof(card) = 'object');
ALTER TABLE watch ADD COLUMN origin text NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual','auto')),
                  ADD COLUMN expires_at timestamptz;
ALTER TABLE alert DROP CONSTRAINT alert_kind_check;
ALTER TABLE alert ADD CONSTRAINT alert_kind_check CHECK (kind IN
  ('price_error','historic_low','target_reached','good_deal','community_deal','catalog_drop'));

CREATE TRIGGER crawl_cursor_touch BEFORE UPDATE ON crawl_cursor
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER topic_touch BEFORE UPDATE ON topic
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON catalog_item, catalog_price_change, crawl_cursor,
                                            crawl_lease, topic TO discountz_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA discountz TO discountz_app;
  END IF;
END $$;
