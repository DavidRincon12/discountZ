-- Revierte 0001_init.
SET LOCAL search_path = discountz;
DROP TABLE alert, outbox, search, watch, price_segment, listing, source_hourly_stat,
           run, run_lease, bot_state, source;
DROP FUNCTION set_updated_at();
DROP DOMAIN money_minor, currency_code, category_slug;
-- schema_migrations y el esquema los administra el ejecutor; la fila 0001 se borra al revertir.
