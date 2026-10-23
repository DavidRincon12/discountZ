-- 0012_intereses: intereses personales de /interes y el interés que sacó cada tarjeta, que no
-- cuenta para el tope de la fuente sino para el del interés (spec del perfil §3, ADR 0013).
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = discountz;

-- Los términos se guardan normalizados; el nombre no se repite sin importar mayúsculas ni
-- tildes (lo comprueba /interes: un UNIQUE no ve las tildes).
CREATE TABLE interest (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  topic       text NOT NULL,
  terms       text[] NOT NULL CHECK (cardinality(terms) BETWEEN 1 AND 10),
  created_at  timestamptz NOT NULL
);

-- Nombre del interés que la sacó en tarjeta. offer_log guarda 7 días: sin índice nuevo.
ALTER TABLE offer_log ADD COLUMN interest text;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'discountz_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON interest TO discountz_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA discountz TO discountz_app;
  END IF;
END $$;
