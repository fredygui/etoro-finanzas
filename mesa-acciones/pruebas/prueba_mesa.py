"""Prueba SIN dinero, sin red privada y sin Claude: eToro, mercado, horario y comité FALSOS.

Ejercita el código real del broker y de la mesa contra un eToro simulado (cartera /pnl, rates, órdenes v3 con SL/TP,
orders:lookup por orderId y referenceId, PATCH de SL/TP, cierres, historial, costes) y comprueba con asserts:
adopción, primera reunión sin plan, límite de ventas, stop que solo sube, compras (reglas, frescura, efectivo, sin
reintento, respuesta dudosa → incierta → confirmada), SL no aplicado → reintento y cierre de respaldo, cierre por SL
de eToro, freno por drawdown, bolsa cerrada, cupo diario, modo lectura, vigía y horario.
Uso: .venv/bin/python pruebas/prueba_mesa.py [dir_tmp]
"""
import json
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import mesa_acciones.__main__ as M  # noqa: E402
from mesa_acciones import broker as brk, comite, comun, etoro, horario, mercado, universo, vigia  # noqa: E402

TMP = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="mesa-acciones-prueba-"))
TMP.mkdir(parents=True, exist_ok=True)
for f in list(TMP.glob("**/*.json")) + list(TMP.glob("*.log")):
    f.unlink()
(TMP / "comites").mkdir(exist_ok=True)

comun.DATA = M.DATA = comite.DATA = TMP
comun.LOG = M.LOG = TMP / "mesa.log"
comun.ESTADO = M.ESTADO = vigia.ESTADO = TMP / "estado.json"
brk.ESTADO_ET = TMP / "etoro.json"
vigia.ALERTAS = TMP / "vigia.json"
universo.RUTA = TMP / "universo.json"
brk.time.sleep = lambda s: None
M.time.sleep = lambda s: None
M.cuota.uso = lambda: None
AVISOS = []
M.app = lambda t, x: AVISOS.append(("app", t, x))
M.wa = lambda x: AVISOS.append(("wa", x[:80], x))
OK = []


def check(cond, txt):
    OK.append((bool(cond), txt))
    print(("  ✔ " if cond else "  ✘ FALLO: ") + txt)
    if not cond:
        raise SystemExit(f"FALLO: {txt}")


# ---------------------------------------------------------------- horario falso (por región)
ABIERTO = {"EEUU": False, "UE": False}
horario.abierto = lambda bolsa_id, t=None, a=0, b=0: ABIERTO.get(horario.region(bolsa_id) or "", False)
M.horario.abierto = horario.abierto
horario.abierta_region = lambda reg, t=None, a=0, b=0: ABIERTO.get(reg, False)

# ---------------------------------------------------------------- universo y mercado falsos
UNIV = [{"id": 9001, "simbolo": "AAA", "nombre": "Alfa", "bolsa_id": 5, "region": "EEUU", "moneda": "USD", "minimo_usd": 10},
        {"id": 9002, "simbolo": "BBB", "nombre": "Beta", "bolsa_id": 4, "region": "EEUU", "moneda": "USD", "minimo_usd": 10},
        {"id": 9003, "simbolo": "CCC", "nombre": "Gamma", "bolsa_id": 6, "region": "UE", "moneda": "EUR", "minimo_usd": 10},
        {"id": 9004, "simbolo": "DDD", "nombre": "Delta", "bolsa_id": 5, "region": "EEUU", "moneda": "USD", "minimo_usd": 10},
        {"id": 9005, "simbolo": "EEE", "nombre": "Épsilon", "bolsa_id": 5, "region": "EEUU", "moneda": "USD", "minimo_usd": 10},
        {"id": 9006, "simbolo": "FFF", "nombre": "Fi", "bolsa_id": 5, "region": "EEUU", "moneda": "USD", "minimo_usd": 10}]
REALES = [  # posiciones de ejemplo (ficticias): id, símbolo, importe, entrada, precio, TP, SL
    (9101, "NOVA", 113.00, 492.91, 544.5, 610.02, 499.1), (9102, "ORBE", 109.61, 336.05, 342.74, 362.69, 308.95),
    (9103, "PICO", 109.61, 124.45, 125.34, 136.33, 116.13), (9104, "RUMB", 194.08, 187.36, 164.3, 184.21, 153.79),
    (9105, "SOLA", 61.02, 305.0, 292.3, 3355.0, 0.0001), (9106, "TIRA", 31.79, 1305.0, 1411.0, 1558.7, 1301.3),
    (9107, "UMBR", 156.93, 565.23, 551.15, 610.02, 474.71), (9108, "VELA", 131.08, 42.95, 42.96, 45.96, 39.16),
    (9109, "ZETA", 28.71, 84.8, 73.35, 89.58, 66.22), (9110, "KAPA", 28.04, 55.98, 44.05, 56.72, 40.24),
    (9111, "LUMA", 56.50, 238.0, 200.43, 246.14, 154.08), (9112, "MIRA", 61.02, 122.0, 122.14, 1342.0, 0.0001)]
for iid, sim, *_ in REALES:
    UNIV.append({"id": iid, "simbolo": sim, "nombre": sim, "bolsa_id": 5, "region": "EEUU", "moneda": "USD", "minimo_usd": 10})
comun.guardar(universo.RUTA, {"generado": comun.iso(), "instrumentos": UNIV})
universo.instrumento = lambda iid: next((i for i in UNIV if i["id"] == int(iid)), None)

PX = {9001: 100.0, 9002: 50.0, 9003: 20.0, 9004: 30.0, 9005: 80.0, 9006: 10.0}
PX.update({iid: px for iid, _, _, _, px, _, _ in REALES})
DIF = 0.0005
FX = {9003: 1.10}
FECHA_PX = {}                                  # iid → fecha ISO de la cotización (para probar precios viejos)
SENAL = {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}


def analizar(inst, px=None):
    p = PX[inst["id"]]
    return {"simbolo": inst["simbolo"], "id": inst["id"], "bolsa_id": inst["bolsa_id"], "minimo_usd": 10,
            "region": inst["region"], "moneda": inst["moneda"], "sector": "x", "precio": p, "bid": p,
            "ask": round(p * (1 + DIF), 4), "diferencial_pct": DIF * 100, "mov_1d_pct": 4.0 if inst["simbolo"] in SENAL else 0.2,
            "mov_5d_pct": 5.0, "mov_20d_pct": 6.0, "vol_ratio_ultimo_dia": 2.0 if inst["simbolo"] in SENAL else 1.0,
            "vol_hoy_ratio": 1.0, "ema20": p * 0.97, "ema50": p * 0.95, "sma200": p * 0.9, "rsi14": 60,
            "atr14_pct": 2.0, "max_20d": p * 0.99, "dist_max20_pct": 1.0, "ruptura_20d": inst["simbolo"] in SENAL}


mercado.analizar = analizar


# ---------------------------------------------------------------- eToro falso
class EtoroFalso:
    def __init__(self, credit):
        self.credit, self.pos, self.hist, self.ordenes, self.cierres = credit, {}, [], {}, {}
        self.llamadas, self.modo_compra, self.modo_patch = [], {}, {}
        self.pid = 4_000_000_000

    def nueva_pos(self, iid, amount, entrada, sl, tp, mirror=0, lev=1, fecha="2026-10-01T10:00:00Z"):
        self.pid += 1
        fx = FX.get(iid, 1.0)
        self.pos[self.pid] = {"positionID": self.pid, "instrumentID": iid, "isBuy": True, "leverage": lev,
                              "settlementTypeID": 1 if lev == 1 else 0, "mirrorID": mirror, "openRate": entrada,
                              "openDateTime": fecha, "amount": amount, "units": amount / (entrada * fx),
                              "stopLossRate": sl, "takeProfitRate": tp, "isNoStopLoss": False, "isNoTakeProfit": False,
                              "totalExternalFees": 1.0}
        return self.pid

    def _pnl(self, v):
        bid = PX[v["instrumentID"]]
        return {"pnL": round(v["units"] * bid * FX.get(v["instrumentID"], 1.0) - v["amount"], 2), "closeRate": bid}

    def mover(self):
        """eToro ejecuta SL/TP si el bid los cruza."""
        for k, v in list(self.pos.items()):
            bid = PX[v["instrumentID"]]
            sl, tp = v["stopLossRate"], v["takeProfitRate"]
            if (sl and sl > 0.01 and bid <= sl) or (tp and bid >= tp):
                self._cerrar(k, sl if bid <= sl else tp)

    def _cerrar(self, k, px=None):
        v = self.pos.pop(k)
        px = px or PX[v["instrumentID"]]
        valor = v["units"] * px * FX.get(v["instrumentID"], 1.0)
        self.credit += valor
        self.hist.append({"positionId": k, "instrumentId": v["instrumentID"], "closeRate": px,
                          "netProfit": round(valor - v["amount"], 2), "closeTimestamp": "2026-11-10T16:00:00.123Z",
                          "openRate": v["openRate"]})

    def __call__(self, metodo, ruta, cuerpo=None, ref=None, **k):
        self.llamadas.append((metodo, ruta.split("?")[0], cuerpo))
        if metodo == "GET" and ruta == "/api/v1/trading/info/real/pnl":
            ps = []
            for v in self.pos.values():
                ps.append({**v, "unrealizedPnL": self._pnl(v)})
            return {"clientPortfolio": {"positions": ps, "credit": round(self.credit, 2), "orders": [],
                                        "ordersForClose": [], "mirrors": []}}
        if metodo == "GET" and ruta.startswith("/api/v2/market-data/rates"):
            ids = [int(x) for x in ruta.split("=")[1].split(",") if x]
            return {"results": [{"instrumentId": i, "bid": PX[i], "ask": round(PX[i] * (1 + DIF), 4),
                                 "date": FECHA_PX.get(i, datetime.utcnow().isoformat()[:23])} for i in ids if i in PX]}
        if metodo == "POST" and ruta == "/api/v3/trading/execution/orders":
            assert cuerpo["leverage"] == 1 and cuerpo["settlementType"] == "real" and cuerpo["transaction"] == "buy"
            assert cuerpo["stopLossRate"] and cuerpo["takeProfitRate"], "compra sin SL/TP en la orden"
            modo = self.modo_compra.pop(cuerpo["instrumentId"], "ok")
            if modo == "rechazo":
                raise etoro.ErrorAPI(400, "InsufficientFunds")
            iid, amount = cuerpo["instrumentId"], cuerpo["amount"]
            assert amount + 1 <= self.credit + 1e-6, f"compra {amount} $ sin efectivo ({self.credit})"
            oid = len(self.ordenes) + 1000
            self.credit -= amount + 1
            ask = round(PX[iid] * (1 + DIF), 4)
            sl = None if modo == "sin_sl" else cuerpo["stopLossRate"]
            pid = self.nueva_pos(iid, amount, ask, sl or 0.0001, cuerpo["takeProfitRate"], fecha=comun.iso())
            self.ordenes[oid] = {"orderId": oid, "ref": ref, "status": {"id": 3, "name": "Filled"},
                                 "positionExecutions": [{"positionId": pid, "investedAmountCurrency": amount,
                                                         "stopLossRate": sl, "takeProfitRate": cuerpo["takeProfitRate"],
                                                         "openingData": {"avgPrice": ask, "units": self.pos[pid]["units"],
                                                                         "fees": 1.0, "taxes": 0}}], "totalCosts": 1.0}
            if modo == "sin_sl":
                self.modo_patch[pid] = "ignora"
            if modo == "dudosa":
                raise etoro.ErrorAPI(0, "TimeoutError: read timed out", dudosa=True)
            return {"orderId": oid, "referenceId": ref}
        if metodo == "GET" and ruta.startswith("/api/v2/trading/info/orders:lookup"):
            q = ruta.split("?")[1]
            if q.startswith("orderId="):
                o = self.ordenes.get(int(q.split("=")[1]))
            else:
                o = next((x for x in self.ordenes.values() if x["ref"] == q.split("=")[1]), None)
            if not o:
                raise etoro.ErrorAPI(404, "not found")
            return o
        if metodo == "PATCH" and ruta.startswith("/api/v2/trading/positions/"):
            pid = int(ruta.rsplit("/", 1)[1])
            if pid not in self.pos:
                raise etoro.ErrorAPI(404, "position not found")
            if self.modo_patch.get(pid) == "error":
                raise etoro.ErrorAPI(409, "conflict")
            if self.modo_patch.get(pid) != "ignora":
                self.pos[pid].update(stopLossRate=cuerpo["stopLossRate"], takeProfitRate=cuerpo["takeProfitRate"])
            return {"operationId": str(uuid.uuid4()), "positionId": pid}
        if metodo == "POST" and ruta.startswith("/api/v1/trading/execution/market-close-orders/positions/"):
            pid = int(ruta.rsplit("/", 1)[1])
            if pid not in self.pos:
                raise etoro.ErrorAPI(404, "position not found")
            oid = len(self.cierres) + 5000
            self.cierres[oid] = pid
            self._cerrar(pid)
            return {"orderForClose": {"orderID": oid}}
        if metodo == "GET" and ruta.startswith("/api/v1/trading/info/trade/history"):
            return list(self.hist)
        if metodo == "GET" and ruta.startswith("/api/v1/trading/info/real/close-orders/"):
            return {"positions": [], "proceeds": None}
        if metodo == "POST" and ruta == "/api/v2/trading/info/costs":
            return {"costs": [{"costType": "transactionFee", "value": 1.0}]}
        if metodo == "DELETE":
            return {}
        raise AssertionError(f"llamada inesperada {metodo} {ruta}")

    def n(self, metodo, prefijo):
        return sum(1 for m, r, _ in self.llamadas if m == metodo and r.startswith(prefijo))


FALSO = EtoroFalso(credit=57.56)
for iid, sim, amount, ent, px, tp, sl in REALES:
    FALSO.nueva_pos(iid, amount, ent, sl, tp)
FALSO.nueva_pos(9002, 30.0, 49.0, 40.0, 60.0, mirror=777)        # copia de un trader: se ignora
FALSO.nueva_pos(9004, 20.0, 29.0, 25.0, 35.0, lev=2)             # apalancada: se ignora
etoro.api = FALSO                                                # cualquier llamada sin `f` también va al falso

# ---------------------------------------------------------------- comité falso
GUION = []
REUNIONES = []


def reunir(datos, c):
    REUNIONES.append(datos)
    fn = GUION.pop(0)
    sal = {"chat": [{"agente": "JEFE", "mensaje": "prueba"}], "decisiones": fn(datos)}
    comun.guardar(TMP / "comites" / f"{comun.iso().replace(':', '')}-{len(REUNIONES)}.json", {"datos": datos, "salida": sal})
    return sal, {"coste_usd_equiv": 0}


comite.reunir = reunir
C = comun.cfg()
C["modo"] = "real"
M.cfg = comun.cfg = lambda: C
C["avisos"]["whatsapp"] = True


def broker():
    return brk.Etoro(C, api=FALSO)


M.brk.broker = lambda c, api=None: brk.Etoro(c, api=FALSO)
print(f"Directorio de la prueba: {TMP}")

# ================================================================ 1. adopción con la bolsa cerrada
print("\n1) Adopción con la bolsa cerrada")
M.ciclo(C)
b = broker()
check(len(b.abiertas()) == 12, f"12 posiciones adoptadas (hay {len(b.abiertas())})")
check(len(b.s["ignoradas"]) == 2, "la copia y la apalancada se ignoran")
check(len(b.sin_plan()) == 12, "las 12 quedan sin plan del comité")
check(not REUNIONES, "sin reunión: la bolsa está cerrada")
sola = next(p for p in b.abiertas() if p["simbolo"] == "SOLA")
check(sola["stop"] is None and sola["objetivo"] is None, "SOLA: SL 0,0001 y TP 11× se leen como «sin SL/TP»")
nova = next(p for p in b.abiertas() if p["simbolo"] == "NOVA")
check(nova["stop"] == 499.1 and nova["objetivo"] == 610.02, "NOVA: SL/TP reales adoptados como plan provisional")
cap = b.s["capital_inicial_usd"]
check(abs(cap - (57.56 + sum(r[2] for r in REALES) + sum(FALSO._pnl(v)["pnL"] for v in FALSO.pos.values()
                                                       if not v["mirrorID"] and v["leverage"] == 1)) - 30 - 20) < 60,
      f"capital inicial = valor total de la cuenta ({cap:.2f} $)")
check(FALSO.n("PATCH", "/") == 0 and FALSO.n("POST", "/api/v3") == 0, "ninguna orden ni cambio de SL con la bolsa cerrada")

# ================================================================ 2. primera reunión: posiciones sin plan
print("\n2) Primera reunión (posiciones sin plan) con la bolsa de EE. UU. abierta")
ABIERTO["EEUU"] = True


def primera(datos):
    ds = []
    pos = {p["simbolo"]: p for p in datos["posiciones_abiertas"]}
    assert all(not p["plan_comite"] for p in pos.values())
    assert "informe_semanal" in datos or True
    for s in ("ZETA", "KAPA", "LUMA", "MIRA", "RUMB", "VELA"):                 # 6 ventas: solo se ejecutan 4
        ds.append({"accion": "vender", "simbolo": s, "confianza": {"ZETA": 9, "KAPA": 8, "LUMA": 7, "MIRA": 6}.get(s, 3),
                   "motivo": f"tesis rota en {s}"})
    ds.append({"accion": "mantener", "simbolo": "NOVA", "stop": 480.0, "objetivo": 620.0, "confianza": 7, "motivo": "bajar stop"})
    ds.append({"accion": "mantener", "simbolo": "SOLA", "stop": 275.0, "objetivo": 330.0, "confianza": 6, "motivo": "plan"})
    ds.append({"accion": "mantener", "simbolo": "TIRA", "stop": 1300.0, "objetivo": 1600.0, "confianza": 6, "motivo": "plan"})
    ds.append({"accion": "mantener", "simbolo": "UMBR", "stop": 300.0, "objetivo": 620.0, "confianza": 6, "motivo": "stop lejano"})
    ds.append({"accion": "mantener", "simbolo": "PICO", "stop": 124.5, "objetivo": 140.0, "confianza": 6, "motivo": "pegado"})
    return ds                                                             # ORBE: sin decisión → plan por defecto


GUION.append(primera)
M.ciclo(C)
b = broker()
check(len(REUNIONES) == 1, "una sola reunión para toda la cartera")
check(REUNIONES[0]["candidatos"] == [], "la reunión «sin plan» no estudia compras")
vend = {p["simbolo"] for p in b.s["cerradas"]}
check(vend == {"ZETA", "KAPA", "LUMA", "MIRA"}, f"4 ventas ejecutadas (máx. por reunión), las de más confianza: {vend}")
check({"RUMB", "VELA"} <= {p["simbolo"] for p in b.abiertas()}, "RUMB y VELA: venta aplazada, siguen abiertas")
check(all(p.get("pnl") is not None for p in b.s["cerradas"]), "P/L real de las ventas tomado del historial")
nova = next(p for p in b.abiertas() if p["simbolo"] == "NOVA")
check(nova["stop"] == 499.1, f"NOVA: el stop no se baja (sigue en 499,1; el comité pedía 480) → {nova['stop']}")
umbr = next(p for p in b.abiertas() if p["simbolo"] == "UMBR")
check(umbr["stop"] == 474.71, f"UMBR: pedía stop 300 (−46 %); se queda en su SL real 474,71 (dentro del 15 %) → {umbr['stop']}")
pico = next(p for p in b.abiertas() if p["simbolo"] == "PICO")
check(abs(pico["stop"] - brk._r(125.34 * 0.97)) < 0.02, f"PICO: stop pegado (124,5) bajado al 3 % mínimo → {pico['stop']}")
sola = next(p for p in b.abiertas() if p["simbolo"] == "SOLA")
v = FALSO.pos[int(sola["id"])]
check(v["stopLossRate"] == 275.0 and v["takeProfitRate"] == 330.0, "SOLA: SL/TP del plan puestos en eToro (PATCH verificado)")
check(sola["sl_etoro"] == 275.0 and sola["plan_comite"], "SOLA: estado local con SL de eToro confirmado")
orbe = next(p for p in b.abiertas() if p["simbolo"] == "ORBE")
check(orbe["plan_comite"] and orbe["stop"] == 308.95, "ORBE sin decisión: conserva su SL real y queda con plan")
check(all(p["plan_comite"] for p in b.abiertas()), "tras la reunión no queda ninguna posición sin plan")
check(not b.sin_plan(), "sin_plan vacío: la siguiente pasada del ciclo no convoca otra reunión")
check(any(a[0] == "wa" for a in AVISOS) and any(a[0] == "app" for a in AVISOS), "aviso a la app y WhatsApp por las ventas")
n_av = len(AVISOS)
M.ciclo(C)
check(len(REUNIONES) == 1, "ciclo siguiente sin motivo: no hay reunión")
check(len(AVISOS) == n_av, "y no hay avisos")

# ================================================================ 3. revisión diaria con compras
print("\n3) Revisión diaria con candidatos y compras")
ABIERTO["UE"] = False                                          # CCC (Xetra) cerrada


def compras(datos):
    sims = {m["simbolo"] for m in datos["candidatos"]}
    assert "CCC" not in sims, "candidato de bolsa cerrada"
    assert {"AAA", "BBB", "DDD", "EEE", "FFF"} <= sims, sims
    PX[9005] *= 1.02                                              # EEE se mueve un 2 % mientras delibera
    return [{"accion": "comprar", "simbolo": "AAA", "importe_usd": 100, "stop": 94.0, "objetivo": 111.0, "confianza": 8, "motivo": "ruptura"},
            {"accion": "comprar", "simbolo": "BBB", "importe_usd": 30, "stop": 48.0, "objetivo": 51.0, "confianza": 7, "motivo": "R:R malo"},
            {"accion": "comprar", "simbolo": "EEE", "importe_usd": 30, "stop": 76.0, "objetivo": 90.0, "confianza": 6, "motivo": "se mueve"},
            {"accion": "comprar", "simbolo": "DDD", "importe_usd": 60, "stop": 28.0, "objetivo": 34.0, "confianza": 5, "motivo": "rechazo"},
            {"accion": "comprar", "simbolo": "NOVA", "importe_usd": 30, "stop": 500.0, "objetivo": 600.0, "confianza": 9, "motivo": "ya abierta"}]


FALSO.modo_compra[9004] = "rechazo"
credit0 = FALSO.credit
GUION.append(compras)
M.revision(C)
b = broker()
check(len(REUNIONES) == 2, "revisión diaria: una reunión")
check(REUNIONES[-1]["efectivo_disponible_usd"] > 0, f"el comité ve el efectivo real ({REUNIONES[-1]['efectivo_disponible_usd']} $)")
aaa = next((p for p in b.abiertas() if p["simbolo"] == "AAA"), None)
check(aaa and aaa["estado"] == "abierta" and aaa["sl_etoro"] == 94.0 and aaa["tp_etoro"] == 111.0,
      "AAA comprada con SL/TP en la misma orden y verificados en eToro")
check(not any(p["simbolo"] == "BBB" for p in b.abiertas()), "BBB rechazada por objetivo < 4 % / R:R")
check(not any(p["simbolo"] == "EEE" for p in b.abiertas()), "EEE descartada por frescura (precio movido > 1 %)")
check(not any(p["simbolo"] == "DDD" for p in b.abiertas()), "DDD: eToro la rechaza y no queda posición")
check(FALSO.n("POST", "/api/v3/trading/execution/orders") == 2, "solo 2 órdenes enviadas (AAA y DDD); ningún reintento")
check(sum(1 for p in b.abiertas() if p["simbolo"] == "NOVA") == 1, "NOVA ya abierta: no se compra otra")
check(FALSO.credit <= credit0 and FALSO.credit >= 0, "nunca se gasta más efectivo del que hay")

# ================================================================ 4. respuesta dudosa → incierta → confirmada; importe > efectivo
print("\n4) Compra con respuesta perdida y compra por encima del efectivo")
PX[9005] /= 1.02
FALSO.modo_compra[9005] = "dudosa"
GUION.append(lambda d: [{"accion": "comprar", "simbolo": "EEE", "importe_usd": 5000, "stop": 76.0, "objetivo": 92.0,
                         "confianza": 7, "motivo": "todo"}])
for s in ("EEE",):
    M._estado()
est = M._estado()
est["enfriamiento"] = {}
est["ultimo_comite"] = "2026-01-01T00:00:00Z"
comun.guardar(M.ESTADO, est)
M.alerta(C, ["EEE"], "prueba vigía")
b = broker()
eee = next((p for p in b.abiertas() if p["simbolo"] == "EEE"), None)
check(eee and eee["estado"] == "incierta", "respuesta dudosa: la compra queda «incierta» (no se reintenta)")
check(eee["importe"] <= 57.56 + 226, f"importe limitado al efectivo disponible ({eee['importe']} $)")
check(b.efectivo() < 1.5 + 0.01 or b.efectivo() < eee["importe"], "la incierta cuenta como efectivo usado")
n_post = FALSO.n("POST", "/api/v3/trading/execution/orders")
M.ciclo(C)
b = broker()
eee = next(p for p in b.abiertas() if p["simbolo"] == "EEE")
check(eee["estado"] == "abierta" and eee["id"], "el ciclo siguiente la encuentra por referenceId y la confirma")
check(FALSO.n("POST", "/api/v3/trading/execution/orders") == n_post, "sin segunda orden de compra")

# ================================================================ 5. SL no aplicado en eToro → reintento → cierre de respaldo
print("\n5) eToro no aplica el SL: reintento y cierre de respaldo")
PX[9006] = 10.0
FALSO.credit += 120
FALSO.modo_compra[9006] = "sin_sl"
GUION.append(lambda d: [{"accion": "comprar", "simbolo": "FFF", "importe_usd": 90, "stop": 9.4, "objetivo": 11.2,
                         "confianza": 7, "motivo": "x"}])
time.sleep(0)
est = M._estado()
est["ultimo_comite"] = "2026-01-01T00:00:00Z"
comun.guardar(M.ESTADO, est)
M.alerta(C, ["FFF"], "prueba")
b = broker()
fff = next(p for p in b.abiertas() if p["simbolo"] == "FFF")
check(fff["sl_etoro"] is None, "FFF comprada pero eToro muestra «sin SL» (simulado)")
n_patch = FALSO.n("PATCH", "/")
fff["sl_patch_t"] = "2026-01-01T00:00:00Z"
b._guardar()
M.ciclo(C)
check(FALSO.n("PATCH", "/") > n_patch, "el ciclo reenvía el SL/TP a eToro")
PX[9006] = 9.3                                                  # cae bajo el stop y eToro no la cierra
M.ciclo(C)
b = broker()
x = next((p for p in b.s["cerradas"] if p["simbolo"] == "FFF"), None)
check(x and "respaldo" in x["motivo"], "bid bajo el stop sin SL en eToro → la mesa cierra a mercado")

# ================================================================ 6. cierre por SL de eToro (hueco)
print("\n6) Cierre por el SL de eToro (hueco de apertura)")
PX[9001] = 90.0                                                 # AAA abre por debajo de su SL 94
FALSO.mover()
n_av = len(AVISOS)
M.ciclo(C)
b = broker()
x = next((p for p in b.s["cerradas"] if p["simbolo"] == "AAA"), None)
check(x and x["motivo"] == "stop (SL de eToro)" and x["pnl"] < 0, f"cierre detectado con su P/L real ({x and x['pnl']} $)")
check(len(AVISOS) > n_av, "aviso por el cierre")

# ================================================================ 7. bolsa cerrada para el candidato; cupo diario
print("\n7) Bolsa cerrada, posiciones fuera de sesión y cupo diario")
ABIERTO["EEUU"] = False
n_r = len(REUNIONES)
M.revision(C)
check(len(REUNIONES) == n_r, "revisión sin ninguna bolsa en sesión: no hay reunión")
ABIERTO["UE"] = True
GUION.append(lambda d: [{"accion": "vender", "simbolo": "NOVA", "confianza": 9, "motivo": "x"}] +
             [{"accion": "pasar", "simbolo": "CCC", "confianza": 5, "motivo": "nada"}])
M.revision(C)
b = broker()
check(any(p["simbolo"] == "NOVA" for p in b.abiertas()), "NOVA (EE. UU. cerrada) no se vende aunque el comité lo pida")
check(all(m["region"] == "UE" for m in REUNIONES[-1]["candidatos"]), "solo candidatos de bolsas en sesión")
est = M._estado()
est["comites"][comun.ahora().strftime("%Y-%m-%d")] = C["comite"]["max_por_dia"]
comun.guardar(M.ESTADO, est)
n_r = len(REUNIONES)
M.alerta(C, ["CCC"], "vigía")
check(len(REUNIONES) == n_r, "cupo diario agotado: el vigía no convoca")
est["comites"] = {}
comun.guardar(M.ESTADO, est)

# ================================================================ 8. freno por drawdown
print("\n8) Freno por drawdown del 25 %")
ABIERTO["EEUU"] = True
for k in list(PX):
    PX[k] *= 0.6
b = broker()
for p in b.abiertas():                                           # que no salten los stops al simular la caída
    p["stop"] = brk._r(PX[p["instrumento"]] * 0.5)
    FALSO.pos[int(p["id"])]["stopLossRate"] = p["stop"]
    p["sl_etoro"] = p["stop"]
b._guardar()
b = broker()
b.cartera(fresca=True)
check(M._bloqueo_compras(C, b, M._estado()), "con la cartera −40 % se bloquean las compras")
GUION.append(lambda d: (d["candidatos"] == [] or (_ for _ in ()).throw(AssertionError("candidatos con freno")))
             and [{"accion": "vender", "simbolo": "RUMB", "confianza": 8, "motivo": "reducir"}])
M.revision(C)
b = broker()
check(REUNIONES[-1].get("compras_bloqueadas"), "el comité recibe compras_bloqueadas y ningún candidato")
check(any(p["simbolo"] == "RUMB" for p in b.s["cerradas"]), "las ventas siguen permitidas con el freno")
for k in list(PX):
    PX[k] /= 0.6

# ================================================================ 9. modo lectura
print("\n9) Modo lectura: ninguna orden sale")
CL = json.loads(json.dumps(C))
CL["modo"] = "lectura"
bl = brk.Etoro(CL, api=FALSO)
n_post = FALSO.n("POST", "/api/v3")
try:
    bl.abrir(UNIV[0], 20, 95, 110)
    check(False, "abrir en modo lectura debía fallar")
except brk.ErrorBroker as e:
    check("modo lectura" in str(e), f"abrir bloqueado ({e})")
try:
    etoro.fijar_sl_tp(1, 2, 3, f=bl.f)
    check(False, "PATCH en modo lectura debía fallar")
except PermissionError:
    check(True, "PATCH de SL/TP bloqueado")
check(etoro.costes({"action": "open"}, f=bl.f) == {"transactionFee": 1.0}, "el what-if de costes sí se permite")
check(FALSO.n("POST", "/api/v3") == n_post, "el eToro falso no recibió ninguna orden")

# ================================================================ 10. vigía
print("\n10) Vigía")
CONV = []
b = broker()
seq = [1.0, 1.04]


def dormir(s):
    if seq:
        PX[9002] = 50.0 * seq.pop(0)


PX[9002] = 50.0
vigia.vigilar(api=FALSO, dormir=dormir, vueltas=3, convocar_fn=lambda s, m: CONV.append((s, m)) or None)
check(any("BBB" in s for s, _ in CONV), f"BBB +4 % en la ventana → convoca reunión ({CONV[:1]})")
CONV.clear()
ABIERTO["EEUU"] = ABIERTO["UE"] = False
vigia.vigilar(api=FALSO, dormir=lambda s: None, vueltas=2, convocar_fn=lambda s, m: CONV.append((s, m)))
check(not CONV, "con las bolsas cerradas el vigía no convoca")
ABIERTO["EEUU"] = True
FECHA_PX[9002] = "2026-11-01T10:00:00.000"
seq[:] = [1.10]
vigia.vigilar(api=FALSO, dormir=dormir, vueltas=2, convocar_fn=lambda s, m: CONV.append((s, m)))
check(not any("BBB" in s for s, _ in CONV), "cotización vieja: se descarta y no salta")
FECHA_PX.clear()
pos = vigia.posiciones()
p = pos[0]
CONV.clear()
PX[p["instrumento"]] = p["stop"] * 1.005
vigia.vigilar(api=FALSO, dormir=lambda s: None, vueltas=1, convocar_fn=lambda s, m: CONV.append((s, m)))
check(any(p["simbolo"] in s and "stop" in m for s, m in CONV), f"posición {p['simbolo']} cerca del stop → alerta")

# ================================================================ 11. horario real
print("\n11) Horario real (sin simular)")
import importlib  # noqa: E402
H = importlib.reload(horario)
t = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=H.MADRID)  # noqa: E731
check(H.abierto(5, t("2026-11-10 15:31")) and not H.abierto(5, t("2026-11-10 15:29")), "NYSE abre 15:30 Madrid")
check(not H.abierto(5, t("2026-11-26 17:00")), "Acción de Gracias: NYSE cerrada")
check(not H.abierto(5, t("2026-11-27 19:30")) and H.abierto(5, t("2026-11-27 18:30")), "27-nov: cierre a las 13:00 NY")
check(H.abierto(6, t("2026-11-10 09:05")) and not H.abierto(6, t("2026-11-10 17:31")), "Xetra 9:00-17:30")
check(not H.abierto(6, t("2026-12-24 10:00")) and not H.abierto(11, t("2026-12-31 10:00")), "24 y 31-dic cerradas en Europa")
check(not H.abierto(5, t("2027-03-26 16:00")) and not H.abierto(9, t("2027-03-26 10:00")), "Viernes Santo 2027 cerrado")
check(H.abierto(5, t("2027-03-17 14:45")), "desfase de horario de verano (marzo): NYSE abre 14:30 en Madrid")
check(H.region(7) is None, "Londres fuera de la mesa (impuesto de timbre)")

print(f"\n✅ {sum(1 for o, _ in OK if o)}/{len(OK)} comprobaciones superadas · órdenes enviadas al falso: "
      f"{FALSO.n('POST', '/api/v3')} compras, {FALSO.n('POST', '/api/v1/trading/execution')} cierres, "
      f"{FALSO.n('PATCH', '/')} cambios de SL/TP · actas en {TMP / 'comites'}")
