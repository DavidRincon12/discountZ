# discountZ

Monitor personal de ofertas: sigue productos y búsquedas en tiendas de Colombia (Éxito, Carulla, Olímpica, Panamericana, Jumbo, Pepe Ganga, Alkosto) y de EE. UU. (Amazon, Newegg, Walmart, Best Buy, más el feed de Slickdeals), guarda un histórico de precios propio, descarta descuentos inflados y avisa por Telegram. Además rastrea el catálogo completo con stock de las siete tiendas colombianas y de Best Buy, y publica cada bajada real como una tarjeta con foto en un grupo de Telegram con un tema por categoría.

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
| `TELEGRAM_CHANNEL_ID` | opcional: id del canal general de ofertas (paso 12) |
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

### 9. Rastreo del catálogo y grupo con temas

Se hace después de fusionar el PR del rastreo. Las alertas pasan del chat privado a un grupo con un tema por categoría.

1. **Migración `0002`.** Justo después de fusionar: *Actions → migrate → Run workflow* con `to = head` y apruébala. Mientras no corra, el ciclo se salta con aviso (la versión del esquema no coincide).
2. **Permitir grupos al bot.** En [@BotFather](https://t.me/BotFather): `/setjoingroups`, elige el bot y luego *Enable*. Al terminar el paso 3 puedes volver a ponerlo en *Disable*.
3. **Crear el grupo.** En Telegram: *Nuevo grupo*, agrega solo a tu bot, ponle nombre (por ejemplo «discountZ») y créalo. En la información del grupo, *Editar → Temas* y actívalos. Luego *Administradores → Agregar administrador*, elige el bot y deja activado **Gestionar temas** (los demás permisos pueden ir apagados).
4. **Id del grupo.** Abre [Telegram Web](https://web.telegram.org/a/), entra al grupo y copia el número de la barra de direcciones después de `#` (empieza por `-100`). Ese es el nuevo **chat_id**.
5. **Secretos.** En *Settings → Secrets and variables → Actions*:
   - Cambia `TELEGRAM_CHAT_ID` por el id del grupo (`-100…`). `TELEGRAM_OWNER_ID` no cambia: solo tú puedes dar comandos, ahora desde el grupo.
   - Crea `BESTBUY_API_KEY` con tu clave de [developer.bestbuy.com](https://developer.bestbuy.com/) (*Get API Key*, gratis).
   - Cuando Walmart apruebe tu acceso a la API de afiliados ([walmart.io](https://walmart.io/)): crea `WALMART_CONSUMER_ID`, `WALMART_KEY_VERSION`, `WALMART_PRIVATE_KEY` (el PEM completo) y `WALMART_PUBLISHER_ID`. Van los cuatro o ninguno.
6. **Temas.** En el grupo escribe `/temas`. En el ciclo siguiente el bot crea los 14 temas y responde en *General* con la lista. Es idempotente: si borras un tema, vuelve a escribir `/temas`.
7. **Best Buy.** En el grupo: `/fuente on bestbuy_us`.
8. **Encender el rastreo.** En *Settings → Secrets and variables → Actions → Variables* crea **`DISCOUNTZ_RASTREO`** con valor `true` y lanza *Actions → crawl → Run workflow* una vez.
9. **Disparo horario.** En cron-job.org crea un segundo trabajo **cada hora**, igual al del paso 7 pero con la URL
   `POST https://api.github.com/repos/DavidRincon12/discountZ/actions/workflows/crawl.yml/dispatches`
   (mismo token, cuerpo y cabeceras). El `schedule` del workflow (cada 3 h) es el respaldo.

Las primeras vueltas solo aprenden precios: un producto necesita su precio estable de al menos 24 h antes de que una bajada cuente, así que las tarjetas empiezan a llegar después del primer día. El resumen diario de las 08:00 suma una línea con los productos por tienda, la duración de la última vuelta, los cambios del día y las alertas por tema.

### 10. Canales de ofertas de Telegram

El ciclo de 5 minutos lee también los canales públicos [DescuentosTech](https://t.me/s/DescuentosTech), [DescuentosHome](https://t.me/s/DescuentosHome), [Clubgratis](https://t.me/s/Clubgratis), [cajitatech](https://t.me/s/cajitatech) y [OFERTU](https://t.me/s/ofertu), cada uno como mucho cada 3 minutos y sin usar tu cuenta de Telegram.

1. **Migraciones `0003` y `0006`.** Justo después de fusionar: *Actions → migrate → Run workflow* con `to = head` (la `0006` agrega DescuentosHome y OFERTU).
2. **Nada más.** La primera lectura de cada canal solo marca el punto de partida: no reenvía ofertas viejas. Desde la siguiente, cada oferta nueva llega como tarjeta con foto al tema de su categoría, con la etiqueta «📣 Visto en <canal> · sin verificar» y los botones «Ver oferta» y «Ver en el canal».

Los precios de los canales no se comparan con el histórico propio: por eso dicen «sin verificar». Si la misma oferta sale en dos canales en menos de 24 h (mismo cupón, o título parecido con precio a ±10 %), llega una sola vez. De OFERTU solo llegan los posts con código de descuento: los demás son catálogo de Amazon sin precio anterior. Para apagar un canal: `/fuente off tg_descuentostech` (o `tg_descuentoshome`, `tg_clubgratis`, `tg_cajitatech`, `tg_ofertu`). El resumen diario suma la línea «Canales: N ofertas (M repetidas)».

### 11. Ofertas de Amazon de DealNews y Slickdeals

Amazon bloquea las lecturas desde Actions, así que sus ofertas llegan por otro camino, también «sin verificar»: el ciclo lee el [feed de Amazon de DealNews](https://www.dealnews.com/s313/Amazon/) como mucho cada 10 minutos y la portada de Slickdeals cada 30, de la que solo toma lo que lleva a amazon.com.

1. **Migración `0004`.** Justo después de fusionar: *Actions → migrate → Run workflow* con `to = head`. Mientras no corra, el ciclo se salta con aviso.
2. **Nada más.** La primera lectura de cada feed solo marca lo que hay. Desde la siguiente, cada oferta nueva llega como tarjeta con foto al tema de su categoría, con «📣 Visto en DealNews · sin verificar» (o en Slickdeals) y el cupón cuando lo pide. La de DealNews trae un solo botón, «Ver en DealNews», con su enlace sin cambios, como piden sus términos; la de Slickdeals trae «Ver oferta» (Amazon) y «Ver en Slickdeals» (el hilo).

Las ofertas repetidas con los canales llegan una sola vez y suman en la línea «Canales» del resumen diario. Para apagar un feed: `/fuente off dealnews` o `/fuente off slickdeals_amazon`.

### 12. Canal general de ofertas

Opcional: un canal de Telegram de solo lectura que recibe una copia de cada oferta, sin importar la categoría. Los temas del grupo siguen igual y los comandos se siguen dando solo en el grupo.

1. **Crear el canal.** En Telegram: *Nuevo canal*, ponle nombre (por ejemplo «discountZ · Todas las ofertas») y elige *Privado* o *Público*. En un canal solo los administradores publican; el resto solo lee.
2. **Hacer admin al bot.** En el canal: *Administradores → Agregar administrador*, busca tu bot y deja activado **Publicar mensajes** (los demás pueden ir apagados).
3. **Id del canal.** Abre [Telegram Web](https://web.telegram.org/a/), entra al canal y copia el número de la barra de direcciones después de `#` (empieza por `-100`).
4. **Secreto.** En *Settings → Secrets and variables → Actions* crea `TELEGRAM_CHANNEL_ID` con ese número. Desde el ciclo siguiente cada oferta llega también al canal.

Al canal solo van las ofertas (no los avisos del sistema ni las respuestas a comandos) y sin los botones «Seguir» y «Silenciar», que solo funcionan en el grupo. Si el bot pierde el permiso de publicar, el grupo sigue recibiendo todo y el log del ciclo dice `channel_off`. Para apagar la copia, borra el secreto.

### 13. Alkosto

Alkosto se lee por el índice de Algolia que usa su propia web para buscar, con la clave pública de solo búsqueda que publica su portada: no hay secretos nuevos. Sus términos («Usos NO permitidos») prohíben los programas que monitorean el sitio; se sumó igual por decisión tuya (ADR 0009). Si Alkosto bloquea o reclama, se apaga con `/fuente off alkosto`.

1. **Migración `0007`.** Justo después de fusionar: *Actions → migrate → Run workflow* con `to = head` y apruébala.
2. Nada más: el job `crawl` ya trae `alkosto` en su matriz y lee el catálogo entero (~14 mil productos, ~20 peticiones) en cada corrida horaria. Como en las demás tiendas, las tarjetas llegan cuando un producto lleva 24 h con precio estable.

### 14. Jumbo y Pepe Ganga

Las dos están en VTEX, como Éxito y Olímpica, y se leen igual (sin secretos nuevos). Sus términos no dicen nada de robots y su robots.txt no bloquea la API; si alguna bloquea o reclama, se apaga con `/fuente off jumbo` o `/fuente off pepeganga`.

1. **Migración `0008`.** Justo después de fusionar: *Actions → migrate → Run workflow* con `to = head` y apruébala.
2. Nada más: el job `crawl` ya trae las dos en su matriz. Jumbo (~45 mil productos con stock, 38 % de supermercado) da unas 6 vueltas al día y Pepe Ganga (~13 mil) una por hora. Las tarjetas llegan cuando un producto lleva 24 h con precio estable.

### Si algo falla

- Sin resumen diario de las 08:00: revisa *Actions → cycle* (workflows desactivados, secreto caducado o Supabase pausado).
- Una alerta llegó a *General* con el aviso «falta su tema»: escribe `/temas` en el grupo.
- Un canal o un feed aparece como roto en `/estado`: cambió el HTML de `t.me/s` o el RSS de DealNews o Slickdeals, y su circuit breaker lo pausa solo; las demás fuentes siguen.
- Un job de *Actions → crawl* en rojo con código 4: la tienda bloqueó el rastreo y su circuit breaker la pausa sola; `/estado` muestra cuándo reintenta. Si ya sabes que volvió, `/fuente on <codigo>` cierra el breaker de una vez.
- Código 3: la base de datos no respondió ni tras reconectar; la corrida siguiente sigue desde donde iba.
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
- Diseño del rastreo de catálogo: `docs/superpowers/specs/2026-10-14-rastreo-catalogo-design.md`
