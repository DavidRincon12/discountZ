# discountZ: perfil de temas e intereses (spec de diseño)

- Fecha: 2026-10-22
- Estado: aprobado por David el 2026-10-22
- Autor: David Santiago Rincón Bautista
- Base: spec del rastreo (`2026-10-14-rastreo-catalogo-design.md`), canales (`2026-10-17-canales-telegram-design.md`) y ADR 0012 (tope diario). Donde este spec no dice nada, mandan ellos.

## 0. Contexto y decisiones

### 0.1 Qué cambia

Hoy salen en tarjeta las primeras 8 ofertas de cada fuente al día, de cualquier tema. David es estudiante de ingeniería y quiere menos ofertas irrelevantes: tecnología, herramientas, ropa y lo útil para estudiar y trabajar como ingeniero. Este spec agrega dos cosas:

1. **Perfil de temas.** Cada tema del grupo (los mismos de `config/topics.toml` y sus hashtags) tiene un modo: `card` (sale en tarjeta), `interest` (se rastrea, pero solo sale si coincide con un interés) u `off` (no se rastrea y solo sale si coincide con un interés). El rastreo deja de recorrer las categorías `off`.
2. **Intereses.** Una lista de cosas concretas («multímetro», «silla ergonómica», «rtx 5070»), cada una con sus palabras en español y en inglés. Lo que coincide sale en tarjeta aunque su tema no sea `card`, aunque la fuente haya llenado su tope y sin el precio mínimo; sigue exigiendo un descuento real.

Se entrega en dos PR: el **PR 1** trae el perfil (§1 y §2) y el **PR 2** los intereses (§3 a §5). Mientras no haya intereses, el PR 1 deja Hogar, Deportes y Otros en `card` para no perder sus ofertas; el PR 2 los pasa a `interest` (revisión final del PR 1).

### 0.2 Decisiones de David (2026-10-22, hilo «Mejoras mayores del bot»)

| # | Decisión |
|---|---|
| P1 | Perfil amplio, no solo tecnología: herramientas, ropa y lo útil para un estudiante y futuro ingeniero. |
| P2 | El rastreo se recorta al perfil, no solo a tecnología. |
| P3 | `/interes` con muchos intereses de entrada, en español y en inglés. |
| P4 | Los temas del perfil y de los intereses son los mismos del grupo. |
| P5 | Aprobó el diseño corto del hilo (perfil, rastreo, intereses con tope propio, `/interes`). |

Quedan fuera de este spec: Woot, 9to5Toys y Mercado Libre (sub-proyectos aparte), la puntuación del cupo diario, el botón 👎 y la comparación por EAN.

### 0.3 Medición: ¿influye el idioma?

Sí. En las 946 ofertas etiquetadas a mano (`tests/fixtures/topics/ofertas-etiquetadas.jsonl`), los canales colombianos llegan casi todos en español (DescuentosTech: 166 en español y 4 en inglés). DealNews y los Amazon de Slickdeals llegan en inglés, igual que eBay, Woot y 9to5Toys. Por eso cada interés lleva palabras en los dos idiomas; las marcas y modelos (`rtx`, `ryzen`, `kindle`) sirven igual en ambos.

## 1. Perfil de temas (PR 1)

### 1.1 Configuración

Cada `[topics.<código>]` de `config/topics.toml` gana la clave `mode`, con `card` por defecto:

| Tema | Modo | Por qué |
|---|---|---|
| 📱 Tecnología | `card` | Núcleo del perfil |
| 🔌 Electrodomésticos | `card` | Tecnología del hogar |
| 🔧 Ferretería y autos | `card` | Herramientas |
| 👕 Moda y calzado | `card` | Ropa (P1) |
| 📚 Libros y papelería | `card` | Estudio |
| 🛋️ Hogar y muebles | `interest` | Escritorio, silla y lámpara están ahí |
| ⚽ Deportes | `interest` | Movilidad eléctrica y bicicletas están ahí |
| 📦 Otros | `interest` | Lo que una tienda no clasifica no se pierde |
| 💄 Belleza, 🛒 Mercado, 🧸 Bebés, 🐶 Mascotas | `off` | Fuera del perfil |

`errores` y `comunidad_us` no tienen modo propio: un error de precio sigue el modo del tema del producto, y `comunidad_us` es de las alertas de la v1 (§1.4).

### 1.2 Qué sale en tarjeta

- **Rastreo** (`app/crawl.py`, `process`): un candidato de un tema que no es `card` y que no coincide con un interés no se confirma. Se guarda su fila del catálogo (el histórico sigue), pero no se gasta la petición de confirmación ni se crea la alerta. Cuenta en el resumen de la corrida (`off_profile=N`).
- **Canales y feeds** (`app/channels.py`, `_handle`): un post de un tema que no es `card` y que no coincide con un interés queda visto sin enviarse (`_mark`, igual que lo que no compensa por casillero) y cuenta como `skipped`.
- Lo que queda fuera del perfil no va al resumen de las 21:00: no es una oferta retenida, es una oferta que no interesa.

### 1.3 Qué se rastrea

`CatalogSource.catalog_partitions` devuelve `CatalogPartition(key, path)` en lugar de `str`. `path` es la ruta de categoría de la partición (`/tecnologia/` en VTEX, `/home & garden/tools & workshop equipment/drills/` en eBay) o `/` cuando la partición no es una categoría (las franjas de precio de Alkosto y el catálogo de Best Buy). El cursor sigue guardando solo `key`.

La función pura `domain.topics.crawl_wanted(rules, modes, source, path)` decide si se recorre. Una partición se recorre si:

- su ruta es `/`, o
- el tema de su ruta (`assign_topic`) tiene modo `card` o `interest`, o
- alguna regla más profunda de esa fuente (o comodín) que empieza por su ruta lleva a un tema `card` o `interest`. Así `panamericana:/hogar/ferreteria/` mantiene la raíz `/hogar/`.

Efecto por tienda: Éxito, Panamericana, Jumbo y Pepe Ganga (VTEX, una partición por categoría raíz) y eBay (categorías de nivel 3) dejan de recorrer belleza, mercado, bebés y mascotas. Alkosto sigue entero: su barrido es por franjas de precio y casi todo su surtido es tecnología y electrodomésticos. La relectura de la capa caliente (`hot_catalog_ids`) salta lo guardado de temas `off`, que así vence con la retención; `fetch_changed` (Best Buy) no se puede filtrar por categoría: lo que traiga de esas categorías se guarda, pero no sale (§1.2). La aclaración salió de la revisión final del PR 1.

El catálogo que deja de recorrerse se borra solo con la retención de siempre, porque ya no se vuelve a ver. El peso de la base debería bajar.

### 1.4 Lo que no cambia

`/seguir` y `/buscar` son pedidos explícitos y no pasan por el perfil. Las alertas de la v1 de Slickdeals (`comunidad_us`) tampoco (§7).

## 2. Errores y bordes del PR 1

- Un `mode` desconocido en `topics.toml` es un error de configuración: `check-config` falla y el ciclo no arranca.
- Si una tienda renombra una raíz y su ruta cae en `otros`, se sigue rastreando (`otros` es `interest`) y la prueba de cobertura de rutas (`intentional_otros`) la delata.
- Cada corrida registra `crawl_partitions_skipped` con el número de particiones saltadas, sin rutas ni nombres.

## 3. Intereses (PR 2)

### 3.1 Dónde viven

- **Semilla:** `config/interests.toml`, versionada, agrupada por los temas del grupo (P4). Cada interés tiene `name`, `topic`, `terms` y opcionalmente `exclude`. Se cambia con un PR.
- **Personales:** tabla `interest` (migración `0012`), que `/interes` llena desde el grupo. Se suman a la semilla.

```toml
[[interests]]
name = "Multímetro"
topic = "ferreteria"
terms = ["multimetro", "multimeter", "pinza amperimetrica", "clamp meter"]
```

### 3.2 Coincidencia (`domain/interests.py`, puro)

- El título y cada término pasan por la misma normalización de los canales: `normalize_title` (minúsculas, sin tildes, número y unidad unidos) y `stem` (singular aproximado en español e inglés).
- Un término coincide si sus palabras aparecen seguidas en el título («silla ergonomica» coincide con «Silla Ergonómica de Oficina», no con «Silla de comedor ergonómica»).
- Un término de `exclude` presente en el título anula la coincidencia del interés (`monitor` excluye `bebe` y `baby`).
- Si coinciden varios, gana el término con más palabras («monitor arm» le gana a «monitor»); a igual largo, el primero del archivo y luego los personales.
- (Revisión final, 2026-10-23) El término tiene que empezar antes del primer conector del título («para», «con», «sin», «for», «with», «w», «without», «compatible»; uno al principio no corta): «Mochila para portátil» es un morral, no un portátil. El singular también ignora la «e» final, para que «cases» active el `exclude` «case» y «headphone» coincida con «headphones».

### 3.3 Qué hace una coincidencia

- Sale en tarjeta aunque el tema no sea `card` y aunque la fuente ya haya enviado sus 8 tarjetas del día. No cuenta para ese tope.
- No aplica el precio mínimo (100.000 COP / US$25) ni el ahorro mínimo (30.000 COP / US$5) del rastreo, ni el precio mínimo de lo que llega por casillero. Sí exige lo demás: el descuento contra el histórico propio (`t_bp` del tema), las 24 h de «antes» estable, la confirmación en la ficha y la regla de una tarjeta por promoción.
- Hay un máximo de 3 tarjetas por interés al día de Bogotá (`daily_cards_per_interest` en `config/interests.toml`). Lo que pasa de ahí va al resumen de las 21:00.
- (Revisión final, 2026-10-23) Con ese tope lleno, lo que también saldría sin el interés (tema `card` y con los mínimos) usa el tope de la fuente y cuenta para él: un interés nunca le quita la tarjeta a una oferta del perfil.
- La tarjeta lleva una línea «🎯 <nombre del interés>» bajo el título.

Para saltar los mínimos de precio en el rastreo, `CatalogObservation` gana `title` (VTEX `productName`, Alkosto `name_text_es`, eBay `title`). No se guarda en la base: solo vive durante la página.

### 3.4 Comando `/interes`

| Uso | Efecto |
|---|---|
| `/interes` | Lista los intereses agrupados por tema, con número; marca los de la semilla. |
| `/interes rtx 5070, rtx5070 [#tema]` | Agrega un interés personal: el nombre es la primera frase y los términos son todas las frases separadas por coma. Sin `#tema`, el tema sale del clasificador de los canales. |
| `/interes quitar <n>` | Quita un interés personal. Los de la semilla se quitan con un PR. |

Límites de entrada: hasta 100 intereses personales, 10 términos por interés y 40 caracteres por término; el nombre no puede repetir el de otro interés. Lo que no cumple se rechaza con el motivo.

### 3.5 Datos (migración `0012`, reversible)

```sql
CREATE TABLE interest (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  topic       text NOT NULL,
  terms       text[] NOT NULL CHECK (cardinality(terms) BETWEEN 1 AND 10),
  created_at  timestamptz NOT NULL
);
ALTER TABLE offer_log ADD COLUMN interest text;  -- nombre del interés que la sacó en tarjeta
```

`count_cards` (tope por fuente) pasa a contar solo `interest IS NULL`; el tope por interés cuenta `interest = <nombre>` en el día. `offer_log` guarda 7 días y pesa poco: no necesita índice nuevo.

## 4. Lista inicial de intereses (para revisar)

(Revisión final, 2026-10-23) Los términos ambiguos se ajustaron contra títulos reales de los canales y del rastreo (`tests/unit/test_interest_seed.py`): la lista vigente es `config/interests.toml`, no esta tabla.

| Tema | Interés | Palabras (español · inglés) | Excluye |
|---|---|---|---|
| 📱 | Portátil | portatil, computador portatil · laptop, notebook, macbook | funda, forro, sleeve, case, protector, sticker, skin, soporte, stand, mochila, backpack, maletin, bag |
| 📱 | Monitor | monitor · monitor | bebe, baby, presion, pressure, cardiaco, heart |
| 📱 | SSD | disco solido, unidad de estado solido · ssd, nvme | |
| 📱 | Memoria RAM | memoria ram · ram ddr4, ram ddr5, ddr4, ddr5, sodimm | |
| 📱 | Tarjeta gráfica | tarjeta grafica, tarjeta de video · graphics card, gpu, geforce, rtx, radeon | |
| 📱 | Procesador | procesador · cpu, ryzen, intel core, core ultra | |
| 📱 | Teclado mecánico | teclado mecanico · mechanical keyboard | |
| 📱 | Mouse | mouse inalambrico, mouse gamer · wireless mouse, gaming mouse, mouse | pad, alfombrilla |
| 📱 | Audífonos | audifonos, auriculares, diadema · headphones, earbuds, headset, airpods | funda, case, estuche |
| 📱 | Parlante | parlante, bocina · bluetooth speaker | |
| 📱 | Router wifi | router wifi, repetidor wifi · mesh wifi, wifi 6, wifi 7, access point | |
| 📱 | Almacenamiento externo | disco duro externo, disco externo, memoria usb · external hard drive, portable ssd, flash drive, microsd | |
| 📱 | Hub USB-C | hub usb c, adaptador multipuerto · usb c hub, docking station | |
| 📱 | Tablet | tablet, tableta · ipad, galaxy tab | funda, case, protector, vidrio, mica, teclado |
| 📱 | Celular | celular, telefono celular · smartphone, iphone, pixel, redmi | funda, forro, case, protector, vidrio, mica, cargador, charger, cable, soporte |
| 📱 | Smartwatch | reloj inteligente · smartwatch, apple watch, galaxy watch, smart band | correa, strap, protector, funda |
| 📱 | Power bank | bateria externa, cargador portatil · power bank | |
| 📱 | Webcam | camara web · webcam | |
| 📱 | Impresora | impresora, multifuncional · printer | 3d |
| 📱 | Impresora 3D | impresora 3d, filamento pla · 3d printer, bambu lab, creality, prusa, pla filament | |
| 📱 | Raspberry Pi | raspberry pi · raspberry pi | |
| 📱 | Arduino y ESP32 | microcontrolador, kit de sensores, protoboard · arduino, esp32, esp8266, microcontroller, sensor kit, breadboard | |
| 📱 | Lector de libros | lector de libros electronicos · kindle, e reader, ereader, kobo | funda, case |
| 📱 | Tableta gráfica | tableta grafica, tableta digitalizadora · drawing tablet, wacom, xp pen | |
| 🔧 | Taladro | taladro, rotomartillo · drill, hammer drill | broca, bits |
| 🔧 | Atornillador | atornillador, destornillador electrico, llave de impacto · impact driver, impact wrench, cordless screwdriver | |
| 🔧 | Destornilladores de precisión | juego de destornilladores, destornilladores de precision · screwdriver set, precision screwdriver, ifixit | |
| 🔧 | Kit de herramientas | kit de herramientas, juego de herramientas, caja de herramientas · tool kit, tool set, tool box, toolbox | |
| 🔧 | Llaves y copas | juego de llaves, juego de copas, llave de torque · socket set, wrench set, ratchet, torque wrench | |
| 🔧 | Pulidora | pulidora, esmeril angular · angle grinder | disco |
| 🔧 | Sierras | sierra circular, caladora, sierra ingletadora · circular saw, jigsaw, miter saw | hoja, blade |
| 🔧 | Herramienta rotativa | mototool, herramienta rotativa · dremel, rotary tool | |
| 🔧 | Multímetro | multimetro, pinza amperimetrica · multimeter, clamp meter | |
| 🔧 | Soldadura electrónica | cautin, estacion de soldadura, estano para soldar · soldering iron, soldering station, solder wire | |
| 🔧 | Osciloscopio | osciloscopio, generador de funciones, analizador logico · oscilloscope, function generator, logic analyzer | |
| 🔧 | Fuente de laboratorio | fuente de laboratorio, fuente regulada · bench power supply, dc power supply | |
| 🔧 | Calibrador | calibrador, pie de rey, micrometro · caliper, vernier, micrometer | |
| 🔧 | Medición | nivel laser, medidor laser, flexometro · laser level, laser measure, tape measure | |
| 🔧 | Pistola de silicona y calor | pistola de silicona, pistola de calor · hot glue gun, heat gun | |
| 🔧 | Linterna | linterna, linterna frontal · flashlight, headlamp | |
| 🔧 | Prensa de banco | prensa de banco, tornillo de banco, banco de trabajo · bench vise, workbench | |
| 🔧 | Inflador y compresor | inflador portatil, compresor de aire · tire inflator, air compressor | |
| 📚 | Calculadora | calculadora cientifica, calculadora graficadora · scientific calculator, graphing calculator, ti 84, ti nspire, casio fx, hp prime | |
| 📚 | Libros de ingeniería | ingenieria, calculo, fisica universitaria, termodinamica, estatica · engineering, calculus, physics, thermodynamics, statics | |
| 📚 | Cuadernos | cuaderno, libreta, cuaderno inteligente · rocketbook | |
| 📚 | Dibujo técnico | escuadras, compas, regla t, portaminas, estilografo · drafting, mechanical pencil, technical pen | |
| 👕 | Morral | morral, mochila, maletin para portatil · backpack, laptop bag | |
| 👕 | Botas de seguridad | botas de seguridad, botas punta de acero, calzado de seguridad · safety boots, steel toe, work boots | |
| 👕 | Chaqueta impermeable | chaqueta impermeable, impermeable · rain jacket, waterproof jacket | |
| 🛋️ | Escritorio | escritorio, escritorio elevable · desk, standing desk | |
| 🛋️ | Silla de oficina | silla ergonomica, silla de oficina, silla de escritorio, silla gamer · office chair, ergonomic chair, gaming chair | |
| 🛋️ | Lámpara de escritorio | lampara de escritorio, barra de luz para monitor · desk lamp, monitor light bar | |
| 🛋️ | Soporte de monitor o portátil | soporte para monitor, brazo para monitor, soporte para portatil · monitor arm, monitor stand, laptop stand | |
| ⚽ | Movilidad eléctrica | patineta electrica, scooter electrico, bicicleta electrica · electric scooter, e bike, ebike | |
| ⚽ | Bicicleta | bicicleta · bicycle, bike | infantil, nino, kids, estatica, stationary, juguete |

## 5. Errores y bordes del PR 2

- `config/interests.toml` con un tema desconocido, un interés sin términos, nombres repetidos o términos de más de 40 caracteres falla en `check-config`.
- Un título vacío no coincide con nada. Un término de una sola palabra con menos de 3 letras («ti», «pc») no se acepta, para no coincidir con cualquier cosa; en `exclude` sí vale («3d»).
- El tope por interés usa el día de Bogotá, como el tope por fuente.
- `/interes` solo se lee del grupo (como los demás comandos). Los términos se guardan normalizados y nunca se registran en los logs.

## 6. Pruebas

- **Unitarias (dominio):** `crawl_wanted` (raíz `card`, `off`, regla más profunda, ruta `/`); coincidencia de intereses (español e inglés, tildes, plural, frase seguida, `exclude`, título vacío, orden); parseo de `/interes` (agregar, listar, quitar, límites); admisión (fuera de perfil, interés que salta el tope, tope por interés).
- **Fuentes:** `catalog_partitions` con rutas en VTEX, eBay, Alkosto y Best Buy; `title` en las observaciones con las fixtures que ya existen.
- **Integración (Postgres):** migración `0012` de ida y vuelta; `count_cards` sin intereses; tope por interés; listado y borrado de intereses personales.
- **Golden:** tarjeta del rastreo y del canal con la línea «🎯».
- **Configuración:** la semilla carga, sus temas existen y ningún interés repite nombre.

## 7. Slickdeals

Los términos de Slickdeals (vigentes desde el 5 de marzo de 2026) prohíben acceder a sus servicios por medios automáticos sin permiso escrito y mostrar sus ofertas en otra app; su robots.txt cierra el RSS que se lee hoy. El feed oficial de FeedBurner responde, pero los términos siguen aplicando. David decidió el 2026-10-22 dejar `slickdeals` y `slickdeals_amazon` encendidas por ahora, asumiendo el riesgo como con Alkosto y eBay; el PR 1 lo anota en `docs/estado.md`. Sus alertas de la v1 (`comunidad_us`) siguen sin pasar por el perfil; `slickdeals_amazon` es un canal y sí pasa por él.

## 8. ADR

La primera tarea del plan registra el ADR 0013 «Perfil de temas e intereses»: modo por tema, rastreo por perfil e intereses que saltan el perfil y el tope.

## 9. Cuándo revisar

- Se sostiene con (hoy): base en 236 de 500 MB, ~60 tarjetas al día con el tope de 8 por fuente.
- Revisar si: las tarjetas bajan de 10 al día (perfil demasiado estrecho), un interés llena su tope 3 días seguidos (términos demasiado amplios) o la base pasa de 400 MB.
