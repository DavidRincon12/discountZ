# discountZ

Monitor personal de ofertas: sigue productos y búsquedas en tiendas de Colombia (Éxito, Carulla, Olímpica, Panamericana) y de EE. UU. (Amazon, Newegg, Walmart, Best Buy, más el feed de Slickdeals), guarda un histórico de precios propio, descarta descuentos inflados y avisa por Telegram.

Corre en GitHub Actions con una base de datos Supabase Postgres gratuita: costo operativo $0.

## Puesta en marcha

Se hace una sola vez. Cada paso dice dónde se hace y qué se guarda.

### 1. Bot de Telegram

1. En Telegram, habla con [@BotFather](https://t.me/BotFather): `/newbot`, elige nombre y usuario, y guarda el **token** (`123456:ABC…`). No lo publiques: el usuario del bot tampoco va en el repo.
2. En BotFather, `/setjoingroups` → *Disable*, para que nadie lo meta en grupos.
3. Escríbele cualquier cosa a tu bot y abre en el navegador `https://api.telegram.org/bot<TOKEN>/getUpdates`. En la respuesta, `message.chat.id` es tu **chat_id** y `message.from.id` es tu **owner_id** (en un chat privado suelen coincidir).
4. Para desarrollar en local usa **otro bot**: un `getUpdates` local consumiría los mensajes de producción.

### 2. Base de datos en Supabase

1. Crea un proyecto gratuito en [Supabase](https://supabase.com/) (región `us-east-1`).
2. En *Project Settings → Data API*, **apaga** la Data API: discountZ no la usa.
3. En el editor SQL, crea el esquema y los dos roles (cambia las claves por valores aleatorios de 32+ caracteres, solo letras y números para no tener que codificarlos en la URL):

   ```sql
   CREATE SCHEMA discountz;
   CREATE ROLE discountz_owner LOGIN PASSWORD '<clave-migrador>';   -- dueño del esquema, sin superusuario
   GRANT discountz_owner TO postgres;                    -- para poder cederle el esquema
   ALTER SCHEMA discountz OWNER TO discountz_owner;
   GRANT CREATE ON DATABASE postgres TO discountz_owner;  -- `CREATE SCHEMA IF NOT EXISTS` del migrador lo exige
   CREATE ROLE discountz_app LOGIN PASSWORD '<clave-app>';          -- el ciclo: solo DML
   ALTER ROLE discountz_app SET search_path = discountz;
   ALTER ROLE discountz_app SET timezone = 'UTC';
   ALTER ROLE discountz_app SET statement_timeout = '15s';
   ALTER ROLE discountz_app SET lock_timeout = '5s';
   ALTER ROLE discountz_app SET idle_in_transaction_session_timeout = '60s';
   ALTER ROLE discountz_app SET idle_session_timeout = '20min';
   ```

4. En *Connect*, copia la URL del **pooler en modo sesión** (puerto `5432`, IPv4). Arma dos URLs, una por rol:
   `postgresql://discountz_app.<project_ref>:<clave-app>@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require`
   y la misma con `discountz_owner` y su clave.

### 3. Secretos de GitHub

En *Settings → Secrets and variables → Actions* del repo:

| Secreto | Valor |
|---|---|
| `DATABASE_URL` | URL del rol `discountz_app` |
| `TELEGRAM_BOT_TOKEN` | token del paso 1 |
| `TELEGRAM_CHAT_ID` | chat_id del paso 1 |
| `TELEGRAM_OWNER_ID` | owner_id del paso 1 |
| `HEALTHCHECKS_PING_URL` | opcional: URL de ping de [healthchecks.io](https://healthchecks.io/) (periodo 10 min, gracia 60 min) |

Luego, en *Settings → Environments*, crea el environment **`production-db`** con tu usuario como revisor obligatorio y agrégale el secreto `DATABASE_URL_MIGRATOR` (URL del rol `discountz_owner`).

### 4. Comprobar la configuración

En tu PC, con las mismas variables que usará el ciclo (PowerShell):

```powershell
$env:DISCOUNTZ_DATABASE_URL = "<URL del rol discountz_app>"
$env:DISCOUNTZ_TELEGRAM_BOT_TOKEN = "<token>"
$env:DISCOUNTZ_TELEGRAM_CHAT_ID = "<chat_id>"
$env:DISCOUNTZ_TELEGRAM_OWNER_ID = "<owner_id>"
uv run python -m discountz check-config
```

Debe imprimir `check-config: OK (…)`. Cierra la ventana al terminar para no dejar las claves en la sesión.

### 5. Migración inicial

*Actions → migrate → Run workflow*, con `to = head`. Apruébala cuando GitHub te lo pida (environment `production-db`). Es el único workflow que cambia el esquema; el ciclo nunca migra y se salta con aviso si la versión no coincide.

### 6. Encender el ciclo

El workflow `cycle` se omite mientras no exista el interruptor. Cuando la migración haya terminado, en *Settings → Secrets and variables → Actions → Variables* crea la variable **`DISCOUNTZ_ACTIVO`** con valor `true` y lanza *Actions → cycle → Run workflow* una vez: el run debe quedar en verde y, en el ciclo siguiente, el bot debe responder a `/estado`. Para apagarlo, cambia el valor a `false`.

### 7. Disparo externo cada 5 minutos

El `schedule` de GitHub (cada 30 min, `7,37 * * * *`) es solo el respaldo; el ritmo real lo da [cron-job.org](https://cron-job.org/):

1. En GitHub, *Settings → Developer settings → Fine-grained tokens*: crea un token **solo para este repo**, con permiso **Actions: Read and write** y nada más, que venza en 12 meses.
2. En cron-job.org, crea un trabajo cada 5 minutos:
   - `POST https://api.github.com/repos/DavidRincon12/discountZ/actions/workflows/cycle.yml/dispatches`
   - Cuerpo: `{"ref":"main"}`
   - Cabeceras: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`
3. En Telegram, dile al bot cuándo vence el token: `/token_vence AAAA-MM-DD`. El resumen diario te avisa 14 días antes.

El workflow `keepalive` reactiva cada lunes los workflows programados para que GitHub no los apague a los 60 días.

### 8. Primer seguimiento

En Telegram: `/seguir https://www.exito.com/<producto>/p`. El bot responde en el siguiente ciclo (10-40 min). `/ayuda` lista todos los comandos.

### Si algo falla

- Sin resumen diario de las 08:00: revisa *Actions → cycle* (workflows desactivados, secreto caducado o Supabase pausado).
- Para rotar un secreto: BotFather `/revoke`, `ALTER ROLE … PASSWORD` o regenerar el check de healthchecks, y actualiza el secreto en GitHub.
- Logs y artefactos de Actions se guardan 7 días y nunca contienen URLs, títulos, precios ni secretos.

## Desarrollo

### Requisitos

- Python 3.13
- [uv](https://docs.astral.sh/uv/) (versión fijada en `pyproject.toml`)
- PostgreSQL 17 para las pruebas de integración

### Comandos

```bash
uv sync                                   # instala dependencias
uv run pytest -q                          # pruebas unitarias
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports
uv run python -m discountz --help         # CLI
```

Pruebas de integración:

```bash
DISCOUNTZ_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  uv run pytest -q -m integration
```

## Documentación

- Arquitectura: `docs/architecture.md`
- Decisiones: `docs/decisions/`
- Diseño de la v1: `docs/superpowers/specs/2026-10-13-discountz-v1-design.md`
