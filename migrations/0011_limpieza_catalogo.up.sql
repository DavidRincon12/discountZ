-- 0011_limpieza_catalogo: quita `catalog_price_change`, que no se escribe desde el PR #31, y el
-- catálogo de Carulla y Olímpica, apagadas el 2026-10-22 (`/fuente off`): la retención no lo
-- borra porque cuenta desde la última corrida de la tienda. Si una ya se prendió, se queda.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '10min';  -- cientos de miles de filas; el job tiene 25 min
SET LOCAL search_path = discountz;

DROP TABLE catalog_price_change;

DELETE FROM catalog_item
 WHERE source IN (SELECT code FROM source WHERE code IN ('carulla', 'olimpica') AND NOT enabled);
