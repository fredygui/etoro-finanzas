"""Prueba de SOLO LECTURA contra la API real de eToro (cuenta del titular). No envía órdenes ni cambia SL/TP.

Comprueba: cartera (/pnl) y adopción local, precios (rates) de posiciones y universo, horario, velas públicas y
análisis, historial de operaciones, orders:lookup (por orderId de una posición real y por un referenceId inexistente),
costes what-if de una compra con SL/TP en la orden (mismo cuerpo que enviaría la mesa) y de un cierre, el Scout y
que el broker en modo lectura bloquea cualquier escritura. Usa un directorio de datos temporal: no toca data/.
Uso: .venv/bin/python pruebas/prueba_lectura.py
"""
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import mesa_acciones.__main__ as M  # noqa: E402
from mesa_acciones import broker as brk, comun, etoro, horario, mercado, universo  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="mesa-acciones-lectura-"))
comun.DATA = M.DATA = TMP
comun.LOG = M.LOG = TMP / "mesa.log"
comun.ESTADO = M.ESTADO = TMP / "estado.json"
brk.ESTADO_ET = TMP / "etoro.json"
universo.RUTA = RAIZ / "data/universo.json"
C = comun.cfg()
C["modo"] = "lectura"
ESCRITURAS = []
_api = etoro.api


def espia(metodo, ruta, cuerpo=None, ref=None, **k):
    if metodo != "GET":
        ESCRITURAS.append((metodo, ruta))
    return _api(metodo, ruta, cuerpo, ref=ref, **k)


RES = []


def paso(nombre, fn):
    t0 = time.time()
    try:
        r = fn()
        RES.append((True, nombre, r))
        print(f"✔ {nombre} ({time.time() - t0:.1f} s): {r}")
        return r
    except Exception as e:  # noqa: BLE001
        RES.append((False, nombre, f"{type(e).__name__}: {e}"))
        print(f"✘ {nombre}: {type(e).__name__}: {str(e)[:400]}")
        return None


b = brk.Etoro(C, api=espia)

paso("horario", horario.estado_txt)
cp = paso("cartera /pnl", lambda: (lambda cp: f"credit {cp['credit']} $, {len(cp['positions'])} posiciones, "
                                   f"{len(cp.get('ordersForOpen') or [])} órdenes de apertura y "
                                   f"{len(cp.get('ordersForClose') or [])} de cierre pendientes, "
                                   f"{len(cp.get('mirrors') or [])} copias")(b.cartera(fresca=True)))
paso("sincronizar (adopción en estado temporal)", lambda: (lambda c: f"{len(b.abiertas())} adoptadas, "
     f"{len(b.sin_plan())} sin plan, ignoradas {b.s.get('ignoradas')}, capital inicial {b.s['capital_inicial_usd']} $, "
     f"cierres {len(c)}")(b.revisar()))
paso("posiciones adoptadas", lambda: "; ".join(
    f"{p['simbolo']}({p.get('region')}) {p['importe']}$ SL {p['sl_etoro'] or '—'} TP {p['tp_etoro'] or '—'}" for p in b.abiertas()))
paso("efectivo disponible y patrimonio", lambda: f"efectivo {b.efectivo():.2f} $ (colchón {b.colchon():.2f}), "
                                                 f"cartera {b.patrimonio():.2f} $")
ids = [p["instrumento"] for p in b.abiertas()]
px = paso("rates de las posiciones (1 llamada)", lambda: (lambda r: {p["simbolo"]: r.get(p["instrumento"])
                                                                     for p in b.abiertas()})(etoro.precios(ids, f=espia)))
if px:
    paso("edad de las cotizaciones", lambda: {s: round(mercado.edad_precio_s(q) / 60) for s, q in px.items() if q})
u = universo.cargar()
paso("universo", lambda: f"{len(u)} acciones ({sum(1 for i in u if i['region'] == 'EEUU')} EE. UU., "
                         f"{sum(1 for i in u if i['region'] == 'UE')} UE): {', '.join(i['simbolo'] for i in u[:12])}…")
paso("rates del universo (1 llamada)", lambda: f"{len(etoro.precios([i['id'] for i in u], f=espia))}/{len(u)} con bid/ask")
paso("velas OneHour/OneDay de MSFT", lambda: (len(etoro.velas(1004, 'OneHour', 60)), len(etoro.velas(1004, 'OneDay', 260)),
                                               etoro.velas(1004, 'OneHour', 3)[-1]))
m = paso("análisis de MSFT", lambda: {k: v for k, v in mercado.analizar(
    {"id": 1004, "simbolo": "MSFT", "bolsa_id": 4, "region": "EEUU", "moneda": "USD"}).items()
    if k in ("precio", "mov_1d_pct", "mov_20d_pct", "rsi14", "atr14_pct", "ema50", "sma200", "vol_ratio_ultimo_dia", "vela_hora")})
paso("historial de operaciones (30 días)", lambda: (lambda h: f"{len(h)} cerradas; ejemplo: "
     f"{json.dumps({k: h[0].get(k) for k in ('positionId', 'instrumentId', 'closeRate', 'netProfit', 'closeTimestamp', 'fees')}) if h else '-'}")
     (etoro.historial(time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400)), f=espia)))
vivas = b.vivas()
reciente = sorted(vivas.values(), key=lambda v: v["openDateTime"])[-1]
paso(f"orders:lookup por orderId ({reciente['orderID']}, {reciente['instrumentID']})", lambda: (lambda o: {
    "status": o.get("status"), "positionExecutions": [{k: x.get(k) for k in ("positionId", "stopLossRate", "takeProfitRate")}
                                                      for x in o.get("positionExecutions") or []],
    "openingData": {k: ((o.get("positionExecutions") or [{}])[0].get("openingData") or {}).get(k)
                    for k in ("avgPrice", "units", "fees")}})(etoro.consultar_orden(order_id=reciente["orderID"], f=espia)))


def lookup_inexistente():
    try:
        etoro.consultar_orden(referencia=str(uuid.uuid4()), f=espia)
        return "devolvió algo (inesperado)"
    except etoro.ErrorAPI as e:
        return f"HTTP {e.codigo} (la mesa trata 404/400 como «sin rastro»)"


paso("orders:lookup por referenceId inexistente", lookup_inexistente)
msft = next((p for p in b.abiertas() if p["simbolo"] == "MSFT"), b.abiertas()[0])
q = (px or {}).get(msft["simbolo"]) or {"ask": msft["entrada"]}
cuerpo = etoro.cuerpo_compra(msft["instrumento"], 50, brk._r(q["ask"] * 0.92), brk._r(q["ask"] * 1.12))
paso("costes what-if de una COMPRA de 50 $ con SL/TP en la orden (cuerpo exacto de la mesa)",
     lambda: etoro.costes(cuerpo, f=espia))
paso("costes what-if de una COMPRA en EUR (si hay alguna del universo UE)",
     lambda: next((etoro.costes(etoro.cuerpo_compra(i["id"], 50, None, None), f=espia) | {"simbolo": i["simbolo"]}
                   for i in u if i["region"] == "UE"), "sin acciones UE en el universo"))
paso(f"costes what-if del CIERRE de {msft['simbolo']}", lambda: etoro.costes({"action": "close", "positionIds": [int(msft["id"])]}, f=espia))


def lectura_bloquea():
    try:
        b.abrir({"id": 1004, "simbolo": "MSFT"}, 20, 400, 700)
        return "NO bloqueó (¡fallo!)"
    except brk.ErrorBroker as e:
        return f"bloqueado: {e}"


paso("modo lectura bloquea compras", lectura_bloquea)
paso("modo lectura bloquea SL/TP", lambda: (lambda: (b.asegurar_sl({**msft, "stop": 1.0, "objetivo": 2.0, "sl_etoro": 0,
                                                                     "simbolo": "MSFT"}, forzar=True), "falló en silencio y quedó en el log")[1])())
regiones = {"EEUU", "UE"}
paso("Scout completo (solo lectura; con la bolsa cerrada los precios son del cierre)",
     lambda: (lambda r: f"{len(r[1])} analizadas, {len(r[0])} candidatos: "
              f"{[(m['simbolo'], m['senal'], m.get('diferencial_pct')) for m in r[0]]}")(M.scout(C, regiones, api=espia)))
print(f"\nEscrituras enviadas a eToro: {ESCRITURAS}")
print(f"{sum(1 for r in RES if r[0])}/{len(RES)} pasos OK · estado temporal en {TMP}")
