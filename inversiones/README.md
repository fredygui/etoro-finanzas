# inversiones

Informe y plan mensual (día 1, 07:00) para la cartera real en eToro del titular, Perfil2 y Perfil3: **solo acciones**, aportación de **200 €/mes** y
operativa **solo el día 1** (compras/ventas con take profit y stop loss que eToro ejecuta durante el mes).
**Multiperfil** (titular, perfil2, perfil3; ver sección más abajo): la investigación es única y compartida, y cada perfil
tiene su cartera, su plan, su informe y su correo.
Flujo: `inversiones.timer` → `inversiones.service` (`python -m inversiones`) → correo (`orquestar.py`). **No es asesoramiento financiero.**

## Fases (`python -m inversiones`)
1. **Catálogo eToro** (`etoro.py`): endpoints públicos sin clave — instrumentos (`instrumentsmetadata`), datos de
   trading (comprable, delisted, CFD/real), nº de inversores por instrumento (`insights/uniques`) y velas diarias
   (`candle.etoro.com`). Cacheado 12 h en `cache/`.
2. **Cribado** (`cribado.py`): ~3.700 instrumentos con >= 300 inversores; momentum 12-1, 3m, 1m, cercanía al máximo
   de 52 semanas, volumen 20d/120d, crecimiento de inversores, popularidad y tendencia (SMA50/200), por percentiles
   dentro de cada tipo. Universo y lista corta: **solo acciones** (60) + caídas fuertes en valores populares
   (rebote); ETF/cripto/materias primas solo como referencias de contexto (SPY, sectores, oro, BTC...). ~8 min.
3. **Calendario** (`calendario.py`): resultados del mes desde la API pública de Nasdaq.
4. **Evaluación** (`informe.evaluar`): rentabilidad de las ideas de los 6 informes anteriores a fin de su mes y
   hasta hoy, frente al SPY; se le pasa a la investigación para corregir sesgos.
5. **Investigación** (`investigar.py`): `claude -p` (opus, esfuerzo alto) en modo `--restricted` solo con
   WebSearch/WebFetch/Agent: subagentes en paralelo (macro, acciones, rebote/otros, cartera actual),
   salida validada con `--json-schema`. Cada idea se comprueba contra el catálogo de eToro (se descartan las que no
   existan; se avisa si solo hay CFD). Un reintento; si falla, informe degradado solo con el cribado (código 2).
6. **Cartera y plan** (`cartera.py`): antes de investigar, carga la cartera real de tu cuenta con la API oficial de
   eToro (`public-api.etoro.com/api/v1/trading/info/real/pnl`; clave de solo lectura en
   `~/.config/inversiones/etoro.env`, permisos 600: `ETORO_USER_KEY` y `ETORO_API_KEY`): posiciones, efectivo y
   órdenes pendientes. Si la API falla, lo simula: da por ejecutado el plan del mes anterior (apertura del primer día
   de mercado >= día 1), cierra los lotes cuyo TP/SL saltó según las velas diarias (SL primero) y descuenta
   comisiones nocturnas de lo apalancado. Con la investigación calcula las órdenes del día 1: ventas, TP/SL para lo
   que ya hay e importes de compra (máx. 5, mín. 15 $) sobre aportación + efectivo + ventas. Reglas: stop >= 0,8
   desviaciones mensuales (máx. 35%); apalancamiento x1 por defecto, x2 con convicción >= 4, volatilidad <= 55% y
   SL×2 <= 40%, x5 casi nunca (eToro no tiene x4); como mucho 30% del dinero del mes apalancado.
7. **Informe** (`informe.py`): nota en `~/Obsidian/Proyectos/Inversiones/<YYYY-MM>.md` (+ `Índice.md`),
   histórico versionado en `informes/<YYYY-MM>.json`, nota `Inversiones/Cartera.md` y
   `~/.local/state/inversiones/ultimo.json` (HTML del correo).

## Perfiles (`inversiones/perfiles.py`)
| Perfil | Cartera | Claves API | Notas Obsidian | Informes | Correo |
|---|---|---|---|---|---|
| titular | `cartera.json` | `~/.config/inversiones/etoro.env` | `Inversiones/` | `informes/` | avisos@example.com |
| perfil2 | `cartera-perfil2.json` | `~/.config/inversiones/perfil2.env` | `Inversiones/Perfil2/` | `informes/perfil2/` | avisos@example.com |
| perfil3 | `cartera-perfil3.json` | `~/.config/inversiones/perfil3.env` | `Inversiones/Perfil3/` | `informes/perfil3/` | avisos@example.com |

- **Compartido** (una vez al mes): catálogo, cribado, calendario, evaluación y UNA llamada a `claude -p` (macro, ideas y
  revisión de carteras; recibe las posiciones de todos los perfiles marcadas por perfil; las decisiones sobre posiciones
  son por símbolo). **Por perfil**: cartera real (API con sus claves), plan (`cartera.calcular_plan`), informe y correo.
- Ficheros de claves (permiso 600), una variable por línea: `ETORO_API_KEY=` (clave **pública**, cabecera
  `x-api-key`) y `ETORO_USER_KEY=` (clave **privada** generada por el usuario, cabecera `x-user-key`), ambas con permiso
  Read (solo lectura). Si falta el fichero, el perfil usa la simulación.
- **Aportación de Perfil2 y Perfil3** (100-200 €/mes, variable): `config.aportacion_modo = "efectivo_real"`. Con la API
  sincronizada, el presupuesto del plan es el **efectivo real libre** en eToro el día del cálculo (más ventas), sin sumar
  la aportación otra vez (el ingreso ya está en el efectivo). Si la API falla o faltan las claves, la cartera se simula,
  se usa `aportacion_eur` (150) y el correo avisa de que los importes son para 150 € y se escalan en proporción. Menos de
  15 $ de presupuesto = sin compras. el titular usa `aportacion_modo = "fija"` (aportación + efectivo), como siempre. Todo
  en el `config` de cada cartera (`aportacion_eur`, `aportacion_min_eur`, `aportacion_max_eur`, `max_compras`...).
- Sin fichero de claves (o con error de API) un perfil usa la simulación y no bloquea a los demás; se avisa al titular
  por correo. Si un perfil falla, los otros siguen y el error va solo al titular.
- Correos: `destinatarios.json` → `perfiles.<nombre>` (informe de cada uno, solo a él) y `error` (fallos y avisos, solo
  el titular). Perfil2 y Perfil3 no reciben enlaces de la LAN ni de Obsidian.

## Base de datos de la cartera (`cartera.json`, `cartera-<perfil>.json`; versionados)
Lotes (cada compra = una posición de eToro con su TP/SL), efectivo en USD, planes por mes (`pendiente`,
`aplicado (supuesto)`, `descartado`) y movimientos. Configuración en `config` (aportación, comisión de cambio
estimada, máx. compras, apalancamientos). Normalmente se rellena sola desde eToro; sin API, si no ejecutas el plan
tal cual, corrígelo antes del siguiente día 1:
```
.venv/bin/python -m inversiones.cartera ver [--perfil perfil2]      # ver/sincronizar: todos por defecto
.venv/bin/python -m inversiones.cartera sincronizar                                    # cargar ya desde eToro
.venv/bin/python -m inversiones.cartera fijar MSFT 0.25 [--precio 520 --tp 640 --sl 450]  # 0 = eliminar
.venv/bin/python -m inversiones.cartera efectivo 12.5
.venv/bin/python -m inversiones.cartera aplicar 2026-11 | descartar 2026-11
```
`--perfil` (repetible o con comas) selecciona perfiles; `fijar`/`efectivo`/`aplicar`/`descartar` usan titular por
defecto y exigen un solo perfil. `--sin-claude` no modifica la cartera.

## Uso
```
.venv/bin/python -m inversiones                   # mes en curso
.venv/bin/python -m inversiones --mes 2026-12     # otro mes
.venv/bin/python -m inversiones --sin-claude      # solo cribado + evaluación (sin coste)
.venv/bin/python -m inversiones --reusar-cribado  # reutiliza cache/cribado-<mes>.json
.venv/bin/python -m inversiones --perfil perfil2    # solo ese perfil (repetible/comas); por defecto titular, perfil2 e perfil3
.venv/bin/python -m inversiones --sin-correo      # genera y guarda, sin enviar correos
.venv/bin/python -m inversiones --prueba --sin-claude --reusar-cribado  # ensayo seguro: sin correo, sin tocar carteras
                                                  #   ni notas/informes reales (salida en ~/.local/state/inversiones/prueba/)
python -m inversiones --solo-correo               # reenvía ultimo.json solo a titular (prueba del correo; con --perfil, el de ese perfil)
systemctl --user start inversiones.service        # como lo lanza el timer (¡ejecuta todo y envía a todos!)
logs inversiones.service
```
Ajustes por entorno (en `.env`, no versionado): `INVERSIONES_MODELO` (opus), `INVERSIONES_ESFUERZO` (high),
`INVERSIONES_TIMEOUT` (s, 7200), `INVERSIONES_ARGS` (argumentos extra del servicio).

## Servicios (systemd de usuario, `systemd/`)
- `inversiones.timer`: `OnCalendar=*-*-01 07:00:00 Europe/Madrid`, `Persistent=true` → solo perfil2 e perfil3.
- `inversiones-titular.timer`: `OnCalendar=Mon *-*-* 06:00:00 Europe/Madrid` → informe completo de titular cada lunes (`--perfil titular`; sobrescribe el informe/nota/plan del mes en curso). el titular ya no recibe el semanal ligero (el lanzador pasa `--perfil perfil2,perfil3` en modo real).
- `inversiones.service`: oneshot; el propio proceso genera el informe y envía el correo.

## Correo (`orquestar.py`)
Al terminar (también degradado, código interno 2) envía el asunto y HTML de `~/.local/state/inversiones/ultimo.json`
más el pie (enlaces a SilverBullet y nota de Obsidian) con `~/work/toolbox/bin/correo -p <destino>`, un envío por
destinatario. Destinatarios (no secretos) en `destinatarios.json` (`perfiles.<nombre>` y `error`); el HTML de cada perfil
está en `~/.local/state/inversiones/ultimo.json` (titular) o `ultimo-<perfil>.json`. Si el pipeline lanza una
excepción, envía a `error` el motivo (y `error` recibe también los avisos de perfiles en simulación) y las últimas 40 líneas de `journalctl --user -u inversiones.service`.
Salida 0 = informe generado y correo enviado; distinta de 0 si no se generó o no se pudo enviar.
`correo` usa la credencial Gmail SMTP guardada en n8n, que por tanto debe seguir instalado.

## Aportación y recordatorio de ingreso
- Los tres perfiles usan `aportacion_modo: "efectivo_real"`: con la API sincronizada, el dinero del mes es el efectivo
  libre en eToro (más ventas). La `aportacion_eur` solo se usa si la API falla (el titular 200 €, Perfil2/Perfil3 150 €).
- `recordatorio.sh [--solo-titular]`: penúltimo día del mes a las 10:00 (`inversiones-recordatorio.timer`,
  `OnCalendar=*-*~02 10:00 Europe/Madrid`) envía a cada perfil un correo recordando ingresar su aportación antes del
  día 1. `--solo-titular` manda los tres solo a titular (prueba).

## Informe semanal de riesgo y noticias (lunes 06:00)
Un informe por perfil (titular, perfil2, perfil3) con SOLO las noticias e indicadores de sus valores. **No es asesoramiento financiero.**
Arquitectura: `n8n` (flujo `inversionesSem01` en ~/work/n8n, Schedule `0 6 * * 1` Europe/Madrid) → `POST 127.0.0.1:5683/semanal/ejecutar`
(`inversiones.lanzador`, `inversiones-lanzador.service`, solo localhost) → `inversiones-semanal.service` (oneshot) →
`python -m inversiones.semanal` → `correo` a cada perfil. n8n sondea `/semanal/estado` cada minuto (máx. 1 h) y solo envía un correo
(credencial Gmail SMTP) al titular si el servicio falla; los informes y avisos los envía el propio script con `correo`
(reutiliza `destinatarios.json`; un fallo de un perfil no frena a los demás y va solo al titular).
- Módulos: `noticias.py` (RSS de Google News + Yahoo Finance de 7 días, por valor y macro, deduplicado; cada valor se descarga una vez),
  `riesgo.py` (velas de eToro: volatilidad 30d anualizada, caída vs máx. 52s, rentab. 1 sem/1 mes, beta y correlación vs SPY, distancia
  al SL, peso máx. y HHI, semáforo VIX [Yahoo ^VIX, o futuro de eToro] + SPY vs SMA200 + semana de petróleo/oro/BTC),
  `ranking.py` (UNA llamada `claude -p --model sonnet --effort low`, sin herramientas, `--json-schema`; puntuación 1-10, impacto, motivo,
  símbolos y párrafo por perfil; si falla, 2 intentos y luego ranking heurístico con aviso), `semanal.py` (orquesta, Markdown/HTML, envío),
  `calendario.resultados_rango` (resultados de la semana, Nasdaq).
- Posiciones: se leen de la API de eToro de cada perfil **sin escribir** en `cartera*.json` (para no alterar los planes del día 1); si
  falla, se usa la última sincronización y el correo lo avisa.
- Salidas: `~/Obsidian/Proyectos/Inversiones/[<Perfil>/]Semanal/<YYYY>-W<ww>.md`; JSON y HTML en `~/.local/state/inversiones/semanal/<perfil>/`.
- Límites: máx. 8 noticias por valor, 15 macro; en el correo, 12 noticias (máx. 5 macro). Coste ~0,30 $ por ejecución (Sonnet, ~145 titulares).
- Alertas: volatilidad > 50 %, caída > 20 % desde máx. 52s, -10 % en la semana, peso > 35 %, SL a < 5 %, apalancada, resultados esta semana, HHI > 0,25, semáforo no verde.
```
.venv/bin/python -m inversiones.semanal --prueba --sin-claude   # todo a ~/.local/state/inversiones/prueba/semanal/, sin enviar ni IA
.venv/bin/python -m inversiones.semanal --prueba                # con la llamada a claude, sin enviar
.venv/bin/python -m inversiones.semanal --solo-titular          # los 3 correos, solo al titular (perfil en el asunto)
curl -XPOST '127.0.0.1:5683/semanal/ejecutar?modo=real|solo-titular|prueba'; curl 127.0.0.1:5683/semanal/estado
```
En n8n, la ejecución manual del flujo usa `solo-titular` (nunca escribe a Perfil2/Perfil3); solo el disparo del lunes usa `real`.
