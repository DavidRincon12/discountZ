---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
---
# Python en discountZ
- Python 3.13, dependencias fijadas con `==` y `uv.lock` versionado.
- `domain/` no importa `httpx`, `curl_cffi`, `psycopg`, `selectolax`, `pydantic`, `pydantic_settings` ni `defusedxml`.
- Dinero solo como `Money(minor: int, currency)`: COP exponente 0, USD exponente 2; nunca `float`; `json.loads(..., parse_float=Decimal)`; redondeo `ROUND_HALF_UP`.
- Porcentajes en puntos básicos (`_bp`, enteros). Datetimes con tz UTC; prohibidos los naive (ruff `DTZ`).
- `datetime.now`, `utcnow`, `date.today`, `time.time` y `random.random` prohibidos salvo en `__main__.py` e `infra/http.py`: `now`, `monotonic`, `sleep` y `random.Random` se inyectan.
- Categorías: `tecnologia`, `componentes`, `hogar`, `ropa`, `otros`.
- Sin costo puesto, TRM, `landed_cop`, `/peso` ni respaldo.
- Errores del dominio heredan de `DiscountzError`; `str(exc)` nunca lleva URLs, títulos, precios ni secretos.
- Logs: lista blanca de campos; nunca URLs con query, cuerpos de respuesta, mensajes de pydantic/psycopg ni secretos.
- mypy strict sin `Any` implícito; dataclasses `frozen=True, slots=True` para valores de dominio.
- Texto (comentarios, docstrings, mensajes) en español; identificadores en inglés.
- Pruebas primero (TDD); las de BD llevan `@pytest.mark.integration`.
