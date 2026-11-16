# mesa-acciones

Mesa de trading **SWING de acciones** en **eToro con dinero real** (cuenta del titular, en USD), derivada de la antigua
mesa-cripto (eliminada el 2026-11-16). Gestiona TODA la cartera de acciones x1 de la cuenta: efectivo libre y posiciones.
Sustituye al ejecutor automático de los lunes de `inversiones` (pausado desde el 2026-11-09); el informe semanal de
las 06:00 sigue y el comité lo recibe como una opinión más.

| Agente | Qué hace | Coste |
|---|---|---|
| 🔍 SCOUT | Python: universo líquido (EE. UU. + zona euro), diferencial ≤ 0,3 %, señales swing (ruptura de 20 días con tendencia y volumen, retroceso en tendencia alcista, movimiento ≥ 3 % con volumen); ~10 candidatos | gratis |
| 🚨 VIGÍA | Servicio permanente: precios de eToro cada 60 s (una llamada) y alertas → reunión extraordinaria | gratis |
| 📰 NEWS · 🌡️ SENTIMIENTO · 📈 CHARTS · 🤨 ESCÉPTICO · 👔 JEFE | Comité: UNA llamada a `claude -p` (Sonnet, solo WebSearch, `--strict-mcp-config`) que ve cartera + efectivo + candidatos + informe semanal y devuelve todas las decisiones | cuota Claude |

## Reglas fijas (no negociables)
- Solo **acciones** (sin ETF, cripto ni CFD), **compras x1** al contado (`settlementType: real`, `leverage: 1`), sin cortos.
- **Nunca se ingresa dinero**: el tope de cualquier compra es el efectivo real (`credit`) menos un colchón
  (máx. de 1 $ y 0,5 %) y lo comprometido en compras sin confirmar.
- Solo las claves del titular (`~/.config/inversiones/etoro.env`); jamás las de Perfil2 o Perfil3.
- Solo opera con la **bolsa del instrumento en sesión** (`horario.py`): EE. UU. 9:30-16:00 NY (15:30-22:00 Madrid),
  zona euro 9:00-17:30 Madrid, con festivos de NYSE, Xetra, BME, Euronext (TARGET) y Milán y las sesiones cortas
  de la NYSE; sin los primeros 15 ni los últimos 10 minutos. Londres, Zúrich, nórdicas y Asia quedan fuera.

## Riesgo (`config.json → riesgo`)
- Cada posición tiene **stop y objetivo** que se ponen en eToro como **SL/TP** (red de seguridad ante huecos de
  apertura o caídas de servidor). Las compras llevan SL/TP **en la misma orden**; los cambios de plan se envían por
  `PATCH` y se verifican releyendo la cartera (reintento cada 20 min si no coinciden). Si el bid cruza el stop con la
  bolsa abierta y eToro no la cerró, la mesa vende a mercado (respaldo).
- Stop entre `stop_min_pct` y `stop_max_pct` (3-15 %) bajo el precio; **el stop solo sube** (también respecto al SL
  real de las posiciones adoptadas); objetivo ≥ `objetivo_min_pct` (4 %).
- **R:R neto ≥ 1,2** con los costes reales: `(objetivo% − costes%) / (stop% + costes%)`, costes% = 1 $ de compra
  (+ `comision_venta_usd`) sobre el importe + diferencial (+ 2 × 0,5 % de conversión en acciones en EUR). Con 1 $ fijo,
  las compras pequeñas casi nunca pasan: es intencionado.
- **Freno por drawdown del 25 %** sobre el valor total de la cuenta al arrancar (`data/etoro.json →
  capital_inicial_usd`): bloquea solo compras; las ventas siguen.
- Compras **sin reintento**: la posición se guarda «incierta» antes de enviar (x-request-id propio); si la respuesta
  se pierde, el ciclo la busca por `referenceId` (orders:lookup) y la confirma o la descarta a los 30 min.
- **Frescura** antes de cada compra: bolsa en sesión, cotización de < 5 min, ask fresco a ≤ 1 % del que vio el comité y
  diferencial razonable. Stop/objetivo/R:R se revalidan con ese ask.
- Sin límite de posiciones (una por símbolo) ni tope de tamaño (`max_pct_cartera: 0` = sin tope).
- Como mucho `comite.max_ventas_por_reunion` (4) ventas por reunión, las de más confianza primero; el resto queda
  `venta_aplazada` con su plan y se revisa en la siguiente. Cada venta necesita su motivo.

## Posiciones adoptadas
Toda posición larga x1 de acciones de la cuenta se adopta (también las abiertas a mano). Si su SL/TP de eToro es real
(no el 0,0001 / ~11× la entrada de «sin SL/TP») se toma como plan provisional y como suelo del stop. Quedan
`plan_comite=false` hasta que el comité les pone plan: eso convoca la primera reunión en cuanto abre su bolsa.
Se ignoran las copias (CopyTrader), los cortos, las apalancadas y los CFD.

## Reuniones (como mucho `comite.max_por_dia` = 6 al día, y nunca con la cuota de Claude ≥ 60 % 5 h / 70 % semana)
- **Revisión diaria** `mesa-acciones-revision.timer` (L-V 16:15 Madrid, las dos regiones abiertas): Scout + toda la
  cartera + informe semanal.
- **VIGÍA** (`mesa-acciones-vigia.service`): universo ±3 % en ≤ 60 min (enfriamiento 4 h), alertas manuales
  (`python -m mesa_acciones vigia SIM --sube 3 --en 60 | --debajo P | quitar SIM`) y posiciones a ≤ 1 % del
  stop/objetivo o ±4 % desde la última reunión. 30 min mínimo entre reuniones del vigía.
- **Posición sin plan** (`mesa-acciones-ciclo.timer`, cada 15 min L-V 8:45-22:15): el ciclo sincroniza con eToro
  (cierres por SL/TP con su P/L real del historial, adopciones, compras inciertas, SL de respaldo) y convoca si hay
  posiciones sin plan con su bolsa abierta.
- Universo: `mesa-acciones-universo.timer` (domingos 07:00) reconstruye `data/universo.json` (catálogo público,
  popularidad en eToro y volumen mediano ≥ 50 M$/día en EE. UU. y ≥ 10 M€ en la zona euro).

## Avisos
`avisos.solo_operaciones=true`: notificación en app-movil (`aviso`) y WhatsApp **solo** cuando hay compra o venta
(un mensaje por reunión) o un cierre por SL/TP. Fallos, freno y reuniones sin operaciones solo al log y a las actas.
`mesa-acciones-informe.timer` (L-V 22:30): resumen del día a la app solo si hubo compras o ventas.

## Uso
```bash
.venv/bin/python -m mesa_acciones estado        # cartera, planes y SL/TP de eToro
.venv/bin/python -m mesa_acciones sincronizar   # relee la cuenta (adopta/cierra) y muestra el estado
.venv/bin/python -m mesa_acciones scout         # tabla del Scout (sin coste)
.venv/bin/python -m mesa_acciones comite SIM…   # fuerza un comité (gasta cuota y OPERA en real)
.venv/bin/python -m mesa_acciones evaluar 10    # contrafactual de los «pasar»
MESA_LECTURA=1 …                                # modo lectura: ninguna orden ni cambio de SL/TP sale
MESA_SIN_WA=1 …                                 # sin avisos
.venv/bin/python pruebas/prueba_mesa.py         # simulador: eToro, mercado, horario y comité falsos (sin red ni Claude)
.venv/bin/python pruebas/prueba_lectura.py      # solo lectura contra la API real (no toca data/)
```

## Pausar
- Todo: `systemctl --user disable --now mesa-acciones-vigia.service mesa-acciones-ciclo.timer mesa-acciones-revision.timer`.
- Sin parar servicios: `"modo": "lectura"` en `config.json` (nada se envía a eToro; sigue sincronizando y reuniéndose).
- Las posiciones conservan su SL/TP en eToro aunque la mesa esté parada.
- Volver al ejecutor de los lunes: `systemctl --user enable --now inversiones-ejecutar.timer` (tras pausar la mesa).

## Datos (`data/`, fuera de git)
`etoro.json` (posiciones, cerradas, capital inicial), `estado.json` (cupos, enfriamientos), `universo.json`,
`vigia.json` (alertas manuales), `comites/*.json` (actas), `mesa.log`.

## API de eToro usada
`GET /api/v1/trading/info/real/pnl` (cartera), `GET /api/v2/market-data/rates` (bid/ask, hasta 1000 por llamada),
`POST /api/v3/trading/execution/orders` (compra x1 con SL/TP), `GET /api/v2/trading/info/orders:lookup`
(orderId/referenceId), `PATCH /api/v2/trading/positions/{id}` (SL/TP), `POST /api/v1/trading/execution/market-close-orders/positions/{id}`,
`GET /api/v1/trading/info/trade/history`, `POST /api/v2/trading/info/costs` (what-if). Velas: `candle.etoro.com` (público).
Límite por defecto: 60 peticiones/min compartidas; el cliente espacia las llamadas ≥ 1,1 s.
