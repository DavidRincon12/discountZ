-- 0001_init: esquema inicial de discountZ (spec §2.2 y §6.3).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

CREATE DOMAIN category_slug AS text CHECK (VALUE IN ('tecnologia','componentes','hogar','ropa','otros'));
CREATE DOMAIN currency_code AS char(3) CHECK (VALUE IN ('COP','USD'));
CREATE DOMAIN money_minor  AS bigint  CHECK (VALUE > 0);

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ───────────── Fuentes: catálogo + breaker + salud (estado que sobrevive entre procesos)
CREATE TABLE source (
  code                       text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{1,31}$'),
  kind                       text NOT NULL CHECK (kind IN ('store','feed')),
  currency                   currency_code NOT NULL,
  enabled                    boolean NOT NULL DEFAULT true,          -- /fuente on|off
  ramp_until                 timestamptz,                            -- presupuesto al 50 % hasta esta fecha
  breaker                    text NOT NULL DEFAULT 'closed' CHECK (breaker IN ('closed','open')),
  breaker_open_until         timestamptz,                            -- semiabierto = open AND now >= open_until
  breaker_trips              smallint NOT NULL DEFAULT 0 CHECK (breaker_trips BETWEEN 0 AND 20),
  last_trip_at               timestamptz,
  last_trip_kind             text CHECK (last_trip_kind IN ('blocked','unavailable','parse')),
  consecutive_unavailable    smallint NOT NULL DEFAULT 0 CHECK (consecutive_unavailable >= 0),
  consecutive_parse_failures smallint NOT NULL DEFAULT 0 CHECK (consecutive_parse_failures >= 0),
  pace_multiplier            smallint NOT NULL DEFAULT 1 CHECK (pace_multiplier IN (1,2,4)),
  quarantine_cycles          smallint NOT NULL DEFAULT 0 CHECK (quarantine_cycles BETWEEN 0 AND 10),
  health                     text NOT NULL DEFAULT 'healthy'
                             CHECK (health IN ('healthy','degraded','blocked','broken')),
  health_since               timestamptz NOT NULL DEFAULT now(),
  notified_health            text NOT NULL DEFAULT 'healthy'
                             CHECK (notified_health IN ('healthy','degraded','blocked','broken')),
  last_notice_at             timestamptz,
  last_success_at            timestamptz,
  canary_checked_at          timestamptz,
  canary_ok                  boolean,
  created_at                 timestamptz NOT NULL DEFAULT now(),
  updated_at                 timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT source_breaker_ck CHECK ((breaker = 'open') = (breaker_open_until IS NOT NULL)),
  CONSTRAINT source_trip_ck    CHECK ((last_trip_kind IS NULL) = (last_trip_at IS NULL)),
  CONSTRAINT source_canary_ck  CHECK ((canary_ok IS NULL) = (canary_checked_at IS NULL))
);
-- Datos de referencia que exigen las FKs (no son semilla de prueba).
INSERT INTO source (code, kind, currency, enabled, ramp_until) VALUES
  ('exito','store','COP',true,NULL), ('carulla','store','COP',true,NULL),
  ('olimpica','store','COP',true,NULL), ('panamericana','store','COP',true,NULL),
  ('amazon_us','store','USD',true, now() + interval '7 days'),
  ('newegg','store','USD',true, now() + interval '7 days'),
  ('walmart_us','store','USD',false,NULL),      -- puerta de activación §4.1
  ('bestbuy_us','store','USD',false,NULL),      -- puerta de activación §4.1
  ('slickdeals','feed','USD',true,NULL);

-- Contadores por hora: presupuesto de 24 h, tasas de éxito, /estado (retención 90 d)
CREATE TABLE source_hourly_stat (
  source           text NOT NULL REFERENCES source(code) ON DELETE CASCADE,
  hour_start       timestamptz NOT NULL CHECK (extract(epoch FROM hour_start)::bigint % 3600 = 0),
  requests         integer NOT NULL DEFAULT 0 CHECK (requests >= 0),   -- cada intento HTTP, con reintentos
  ok               integer NOT NULL DEFAULT 0 CHECK (ok >= 0),
  blocked          integer NOT NULL DEFAULT 0 CHECK (blocked >= 0),
  unavailable      integer NOT NULL DEFAULT 0 CHECK (unavailable >= 0),
  parse_errors     integer NOT NULL DEFAULT 0 CHECK (parse_errors >= 0),
  not_found        integer NOT NULL DEFAULT 0 CHECK (not_found >= 0),
  confirm_mismatch integer NOT NULL DEFAULT 0 CHECK (confirm_mismatch >= 0),
  soft_empty       integer NOT NULL DEFAULT 0 CHECK (soft_empty >= 0),      -- ciclos 2xx sin ofertas
  offers           integer NOT NULL DEFAULT 0 CHECK (offers >= 0),
  latency_ms_sum   bigint  NOT NULL DEFAULT 0 CHECK (latency_ms_sum >= 0),
  latency_ms_max   integer NOT NULL DEFAULT 0 CHECK (latency_ms_max >= 0),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, hour_start),                                     -- cubre la FK
  CONSTRAINT shs_counts_ck CHECK (ok + blocked + unavailable + parse_errors + not_found <= requests)
);
CREATE INDEX source_hourly_stat_hour_ix ON source_hourly_stat (hour_start);   -- retención

-- ───────────── Listing: una VARIANTE vendible en una fuente (SKU VTEX, ASIN, usItemId, Item#, sku Best Buy)
CREATE TABLE listing (
  id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source                  text NOT NULL REFERENCES source(code) ON DELETE RESTRICT,
  external_id             text NOT NULL CHECK (external_id ~ '^[A-Za-z0-9]{1,40}$'),
  parent_external_id      text CHECK (parent_external_id ~ '^[A-Za-z0-9]{1,40}$'),  -- productId VTEX, ASIN padre
  canonical_url           text NOT NULL CHECK (canonical_url ~ '^https://' AND length(canonical_url) <= 500),
  title                   text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
  category                category_slug NOT NULL DEFAULT 'otros',
  first_seen_at           timestamptz NOT NULL,
  last_seen_at            timestamptz NOT NULL,
  last_price_minor        money_minor,
  last_list_price_minor   money_minor,
  last_in_stock           boolean NOT NULL,
  last_seller_class       char(1) NOT NULL CHECK (last_seller_class IN ('f','m','u')),
  alltime_min_minor       money_minor,                 -- mínimo robusto; sobrevive a la retención
  alltime_min_at          timestamptz,
  history_reset_at        timestamptz,                 -- G6/G7: unidad o identidad cambió
  pending_confirmation_at timestamptz,                 -- candidato sin confirmar: prioridad en el siguiente plan
  muted_until             timestamptz,
  gone_at                 timestamptz,                 -- 404 tres veces
  hot_until               timestamptz,                 -- modo caliente (§6.2)
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT listing_identity_uq UNIQUE (source, external_id),          -- identidad entre ciclos; cubre FK
  CONSTRAINT listing_seen_ck     CHECK (last_seen_at >= first_seen_at),
  CONSTRAINT listing_min_pair_ck CHECK ((alltime_min_minor IS NULL) = (alltime_min_at IS NULL))
) WITH (fillfactor = 80);                                                 -- UPDATE HOT por lectura
CREATE INDEX listing_last_seen_ix ON listing (last_seen_at);              -- retención
CREATE INDEX listing_pending_ix   ON listing (source) WHERE pending_confirmation_at IS NOT NULL;
CREATE INDEX listing_hot_ix       ON listing (source, hot_until) WHERE hot_until IS NOT NULL;

-- ───────────── Histórico: segmentos de estado constante (§3.2)
CREATE TABLE price_segment (
  listing_id       bigint      NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
  started_at       timestamptz NOT NULL,                 -- primera lectura con este estado
  last_seen_at     timestamptz NOT NULL,                 -- SIN índice: UPDATE HOT por lectura
  ended_at         timestamptz,                          -- instante de la lectura siguiente; NULL = abierto
  price_minor      money_minor,                          -- NULL = sin precio comprable
  list_price_minor money_minor,                          -- máximo L visto en el segmento (tras G4)
  in_stock         boolean     NOT NULL,
  seller_class     char(1)     NOT NULL CHECK (seller_class IN ('f','m','u')),   -- tienda / marketplace / desconocido
  unit_key         text        NOT NULL DEFAULT 'un|1' CHECK (unit_key ~ '^[a-z]{1,8}\|[0-9.]{1,10}$'),
  status           char(1)     NOT NULL DEFAULT 'o' CHECK (status IN ('o','s','g')), -- ok / sin confirmar / glitch
  sample_count     integer     NOT NULL DEFAULT 1 CHECK (sample_count >= 1),
  PRIMARY KEY (listing_id, started_at),                                            -- cubre FK
  CONSTRAINT seg_order_ck CHECK (last_seen_at >= started_at),
  CONSTRAINT seg_next_ck  CHECK (ended_at IS NULL OR ended_at > last_seen_at),
  CONSTRAINT seg_stock_ck CHECK (NOT in_stock OR price_minor IS NOT NULL)
) WITH (fillfactor = 85);
CREATE UNIQUE INDEX price_segment_open_uq ON price_segment (listing_id) WHERE ended_at IS NULL;  -- 1 abierto

-- ───────────── Seguimiento (producto puntual que David pidió)
CREATE TABLE watch (
  id                    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,          -- "p12"
  source                text NOT NULL REFERENCES source(code) ON DELETE RESTRICT,
  listing_id            bigint REFERENCES listing(id) ON DELETE CASCADE,          -- NULL hasta resolver
  pending_ref           text CHECK (pending_ref ~ '^(id|slug|short):[A-Za-z0-9/_|-]{1,190}$'),
  status                text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','paused','gone')),
  requested_category    category_slug NOT NULL DEFAULT 'otros',                    -- se copia al listing al resolver
  target_price_minor    money_minor,
  target_set_at         timestamptz,
  next_check_at         timestamptz NOT NULL,
  last_checked_at       timestamptz,
  consecutive_failures  smallint NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  consecutive_not_found smallint NOT NULL DEFAULT 0 CHECK (consecutive_not_found >= 0),
  reply_to_message_id   bigint,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT watch_resolved_ck CHECK ((status = 'pending') = (listing_id IS NULL)),
  CONSTRAINT watch_ref_ck      CHECK ((status = 'pending') = (pending_ref IS NOT NULL)),
  CONSTRAINT watch_target_ck   CHECK ((target_price_minor IS NULL) = (target_set_at IS NULL))
);
CREATE UNIQUE INDEX watch_listing_uq ON watch (listing_id) WHERE listing_id IS NOT NULL;   -- cubre FK
CREATE UNIQUE INDEX watch_pending_uq ON watch (source, pending_ref) WHERE pending_ref IS NOT NULL;
CREATE INDEX watch_source_due_ix     ON watch (source, next_check_at);                    -- cubre FK + plan

-- ───────────── Búsqueda (descubrimiento; en 'slickdeals' es un filtro de leads)
CREATE TABLE search (
  id                   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,           -- "b3"
  source               text NOT NULL REFERENCES source(code) ON DELETE RESTRICT,
  query                text NOT NULL CHECK (length(query) BETWEEN 2 AND 100
                         AND query = lower(regexp_replace(btrim(query), '\s+', ' ', 'g'))),
  category             category_slug NOT NULL,
  exclude_terms        text[] NOT NULL DEFAULT '{}' CHECK (cardinality(exclude_terms) <= 20),
  min_price_minor      money_minor,
  max_price_minor      money_minor,
  enabled              boolean NOT NULL DEFAULT true,
  next_run_at          timestamptz NOT NULL,
  last_run_at          timestamptz,
  last_result_count    smallint CHECK (last_result_count >= 0),
  consecutive_failures smallint NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  consecutive_empty    smallint NOT NULL DEFAULT 0 CHECK (consecutive_empty >= 0),  -- SOFT_EMPTY
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT search_uq       UNIQUE (source, query),                                -- cubre FK
  CONSTRAINT search_range_ck CHECK (min_price_minor IS NULL OR max_price_minor IS NULL
                                    OR min_price_minor < max_price_minor)
);
CREATE INDEX search_due_ix ON search (next_run_at) WHERE enabled;

-- ───────────── Outbox: todo mensaje saliente (alerta, resumen, respuesta, sistema)
CREATE TABLE outbox (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purpose             text NOT NULL CHECK (purpose IN ('alert','digest','reply','system')),
  dedup_key           text NOT NULL UNIQUE CHECK (length(dedup_key) BETWEEN 3 AND 120),
  priority            smallint NOT NULL CHECK (priority BETWEEN 0 AND 9),               -- 0 = más urgente
  body_html           text NOT NULL CHECK (length(body_html) BETWEEN 1 AND 8000),       -- visible ≤ 4096: render
  reply_markup        jsonb CHECK (reply_markup IS NULL OR jsonb_typeof(reply_markup) = 'object'),
  silent              boolean NOT NULL DEFAULT false,
  reply_to_message_id bigint,
  status              text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','sending','sent','failed','expired')),
  attempts            smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
  not_before          timestamptz NOT NULL,
  expires_at          timestamptz NOT NULL,
  sending_since       timestamptz,
  sent_at             timestamptz,
  telegram_message_id bigint,
  last_error_code     text CHECK (length(last_error_code) <= 60),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT outbox_expiry_ck  CHECK (expires_at > not_before),
  CONSTRAINT outbox_sending_ck CHECK ((status = 'sending') = (sending_since IS NOT NULL)),
  CONSTRAINT outbox_sent_ck    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
  CONSTRAINT outbox_msgid_ck   CHECK (telegram_message_id IS NULL OR status = 'sent')
);
CREATE INDEX outbox_ready_ix   ON outbox (priority, not_before) WHERE status IN ('pending','sending');
CREATE INDEX outbox_created_ix ON outbox (created_at);                                 -- retención

-- ───────────── Alerta: hecho de dominio (dedupe, cooldown, rearme, ancla de tendencia)
CREATE TABLE alert (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  listing_id           bigint NOT NULL REFERENCES listing(id) ON DELETE CASCADE,
  segment_started_at   timestamptz,                                  -- NULL tras la retención del segmento
  watch_id             integer REFERENCES watch(id) ON DELETE SET NULL,
  outbox_id            bigint  REFERENCES outbox(id) ON DELETE SET NULL,
  kind                 text NOT NULL CHECK (kind IN
                       ('price_error','historic_low','target_reached','good_deal','community_deal')),
  maturity             text NOT NULL CHECK (maturity IN ('cold','warm','mature')),
  price_minor          money_minor NOT NULL,
  reference_minor      money_minor,
  real_saving_bp       integer CHECK (real_saving_bp BETWEEN -100000 AND 10000),
  advertised_saving_bp integer CHECK (advertised_saving_bp BETWEEN 1 AND 10000),
  flags                text[] NOT NULL DEFAULT '{}' CHECK (flags <@ ARRAY[
                         'inflated_list','advertised_gap','pre_inflated','flash_seen','gap_before',
                         'short_history','marketplace','target_above_typical','ref_antigua','unit_changed',
                         'storm','target_also','persistence_only','hot',
                         'community','unverified']::text[]),
  delivery             text NOT NULL DEFAULT 'new' CHECK (delivery IN ('new','queued','muted','suppressed')),
  observed_at          timestamptz NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT alert_segment_fk FOREIGN KEY (listing_id, segment_started_at)
             REFERENCES price_segment (listing_id, started_at) ON DELETE SET NULL (segment_started_at),
  CONSTRAINT alert_dedup_uq   UNIQUE (listing_id, segment_started_at, kind),   -- cubre ambas FKs por prefijo
  CONSTRAINT alert_queued_ck  CHECK ((delivery = 'queued') = (outbox_id IS NOT NULL))
);
CREATE INDEX alert_listing_created_ix ON alert (listing_id, created_at DESC);  -- cooldown y ancla
CREATE INDEX alert_watch_ix   ON alert (watch_id)  WHERE watch_id IS NOT NULL;
CREATE INDEX alert_outbox_ix  ON alert (outbox_id) WHERE outbox_id IS NOT NULL;
CREATE INDEX alert_new_ix     ON alert (observed_at) WHERE delivery = 'new';
CREATE INDEX alert_created_ix ON alert (created_at);                           -- retención

-- ───────────── Singletons de operación
CREATE TABLE bot_state (
  id                  smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  telegram_offset     bigint NOT NULL DEFAULT 0 CHECK (telegram_offset >= 0),   -- último update_id aplicado
  muted_until         timestamptz,                                              -- /silenciar todo N
  last_maintenance_on date,
  last_summary_on     date,
  last_db_size_bytes  bigint CHECK (last_db_size_bytes >= 0),
  discovery_paused    boolean NOT NULL DEFAULT false,                           -- guarda de 450 MB
  dispatch_token_expires_on date,                                               -- token de cron-job.org (§6.1)
  updated_at          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO bot_state DEFAULT VALUES;

CREATE TABLE run_lease (
  id          smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  holder      text CHECK (length(holder) <= 80),           -- 'gh:<run_id>:<attempt>' | 'migrate:<run_id>' | 'local:<host>'
  acquired_at timestamptz,
  expires_at  timestamptz,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT lease_pair_ck CHECK ((holder IS NULL) = (expires_at IS NULL)
                              AND (holder IS NULL) = (acquired_at IS NULL))
);
INSERT INTO run_lease DEFAULT VALUES;

CREATE TABLE run (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  gh_run_id         bigint,
  gh_run_attempt    smallint CHECK (gh_run_attempt >= 1),
  trigger_kind      text NOT NULL CHECK (trigger_kind IN ('schedule','workflow_dispatch','local')),
  status            text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','ok','partial','failed','skipped_locked','skipped_schema')),
  started_at        timestamptz NOT NULL,
  finished_at       timestamptz,
  gap_seconds       integer CHECK (gap_seconds >= 0),
  requests          integer NOT NULL DEFAULT 0 CHECK (requests >= 0),
  alerts_created    integer NOT NULL DEFAULT 0 CHECK (alerts_created >= 0),
  messages_sent     integer NOT NULL DEFAULT 0 CHECK (messages_sent >= 0),
  updates_processed integer NOT NULL DEFAULT 0 CHECK (updates_processed >= 0),
  error_code        text CHECK (error_code ~ '^[a-z0-9_.]{1,60}$'),       -- código, nunca texto libre
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT run_gh_uq      UNIQUE (gh_run_id, gh_run_attempt),
  CONSTRAINT run_local_ck   CHECK ((trigger_kind = 'local') = (gh_run_id IS NULL)),
  CONSTRAINT run_attempt_ck CHECK ((gh_run_id IS NULL) = (gh_run_attempt IS NULL)),
  CONSTRAINT run_open_ck    CHECK ((status = 'running') = (finished_at IS NULL)),
  CONSTRAINT run_time_ck    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX run_started_ix ON run (started_at);

-- ───────────── Triggers de auditoría
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['source','source_hourly_stat','listing','watch','search','outbox','alert',
                           'bot_state','run_lease','run'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON discountz.%I FOR EACH ROW
                    EXECUTE FUNCTION discountz.set_updated_at()', t || '_touch', t);
  END LOOP;
END $$;

-- ───────────── Permisos (los roles se crean a mano con su secreto: runbook §7.6)
REVOKE ALL ON SCHEMA discountz FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    EXECUTE 'REVOKE ALL ON SCHEMA discountz FROM anon, authenticated';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT USAGE ON SCHEMA discountz TO discountz_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA discountz TO discountz_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA discountz TO discountz_app;
  END IF;
END $$;
