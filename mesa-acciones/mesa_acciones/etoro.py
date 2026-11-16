"""Cliente de eToro: API pública oficial (public-api.etoro.com, cuenta REAL del titular) y datos públicos sin clave.

Claves: ~/.config/inversiones/etoro.env (ETORO_API_KEY, ETORO_USER_KEY; lectura y escritura). NUNCA otras claves
(perfil2.env / perfil3.env no se leen jamás: la ruta está fijada aquí y `_claves` rechaza cualquier otra).

Límites de la API: 60 peticiones/min compartidas por defecto (120/min en datos de mercado). `api` espacia las
llamadas autenticadas ≥ PAUSA_S dentro de cada proceso y reintenta solo en 429.
Velas: candle.etoro.com (público, sin cuota). Catálogo: api.etorostatic.com / www.etoro.com/sapi (público).
"""
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .comun import RAIZ, UA, get_json

API = "https://public-api.etoro.com"
CLAVES = Path.home() / ".config/inversiones/etoro.env"
CACHE = RAIZ / "cache"
URL_VELAS = "https://candle.etoro.com/candles/asc.json/{intervalo}/{n}/{id}"
URL_META = "https://api.etorostatic.com/sapi/instrumentsmetadata/V1.1/instruments"
URL_TRADING = "https://www.etoro.com/sapi/trade-real/instruments?InstrumentDataFilters=Activity,Rates,TradingData"
URL_GRUPOS = "https://api.etorostatic.com/sapi/app-data/web-client/app-data/instruments-groups.json"
URL_INVERSORES = "https://www.etoro.com/sapi/insights/insights/uniques"
PAUSA_S = 1.1
_ULTIMA = [0.0]

# estados de orders:lookup
EN_CURSO, HECHA, FALLIDA = {1, 2, 11, 12}, {3, 5}, {4, 6, 7, 8, 9, 10}


class ErrorAPI(Exception):
    """Respuesta de error de la API. `dudosa`=True si no sabemos si la orden llegó (timeout, 5xx, red)."""

    def __init__(self, codigo, cuerpo, dudosa=False):
        super().__init__(f"HTTP {codigo}: {str(cuerpo)[:300]}")
        self.codigo, self.cuerpo, self.dudosa = codigo, cuerpo, dudosa


def _claves():
    if CLAVES.name != "etoro.env":                       # salvaguarda: solo la cuenta del titular
        raise PermissionError("solo se permiten las claves del titular")
    env = dict(l.strip().split("=", 1) for l in CLAVES.read_text().splitlines() if "=" in l)
    return env["ETORO_API_KEY"].strip(), env["ETORO_USER_KEY"].strip()


def api(metodo, ruta, cuerpo=None, ref=None, timeout=40):
    """Llamada autenticada. `ref` = x-request-id (idempotencia de órdenes). Devuelve el JSON ({} si vacío).
    Lanza ErrorAPI; en POST/PATCH/DELETE marca dudosa la respuesta perdida (timeout, 5xx, red)."""
    k, u = _claves()
    h = {**UA, "x-api-key": k, "x-user-key": u, "x-request-id": ref or str(uuid.uuid4())}
    datos = None
    if cuerpo is not None:
        datos, h["Content-Type"] = json.dumps(cuerpo).encode(), "application/json"
    for intento in range(4):
        espera = PAUSA_S - (time.time() - _ULTIMA[0])
        if espera > 0:
            time.sleep(espera)
        _ULTIMA[0] = time.time()
        req = urllib.request.Request(API + ruta, data=datos, headers=h, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode() or "{}"
                return json.loads(txt)
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            if e.code == 429 and intento < 3:
                time.sleep(min(int(e.headers.get("Retry-After") or 20), 65))
                continue
            if metodo == "GET" and e.code >= 500 and intento < 2:
                time.sleep(3)
                continue
            raise ErrorAPI(e.code, txt, dudosa=metodo != "GET" and e.code >= 500) from None
        except (OSError, ValueError) as e:                # timeout, red, JSON roto
            if metodo == "GET" and intento < 2:
                time.sleep(3)
                continue
            raise ErrorAPI(0, f"{type(e).__name__}: {e}", dudosa=metodo != "GET") from None
    raise ErrorAPI(429, "límite de peticiones")


# ---------------------------------------------------------------- lectura

def cartera(f=api):
    """clientPortfolio: positions, credit, orders, ordersForOpen, ordersForClose…"""
    return f("GET", "/api/v1/trading/info/real/pnl")["clientPortfolio"]


def precios(ids, f=api):
    """{instrumentId: {bid, ask, fecha}} de hasta 1000 instrumentos en UNA llamada."""
    out = {}
    ids = [int(i) for i in ids]
    for i in range(0, len(ids), 900):
        r = f("GET", "/api/v2/market-data/rates?instrumentIds=" + ",".join(map(str, ids[i:i + 900])))
        for x in r.get("results") or []:
            if x.get("bid") and x.get("ask"):
                out[int(x["instrumentId"])] = {"bid": float(x["bid"]), "ask": float(x["ask"]), "fecha": x.get("date")}
    return out


def historial(desde, f=api):
    """Operaciones cerradas desde `desde` (YYYY-MM-DD): netProfit, closeRate, closeTimestamp, positionId…"""
    r = f("GET", f"/api/v1/trading/info/trade/history?minDate={desde}&pageSize=500")
    return r if isinstance(r, list) else r.get("items") or r.get("trades") or []


def consultar_orden(order_id=None, referencia=None, f=api):
    q = f"orderId={order_id}" if order_id else f"referenceId={referencia}"
    return f("GET", f"/api/v2/trading/info/orders:lookup?{q}")


def consultar_cierre(order_id, f=api):
    return f("GET", f"/api/v1/trading/info/real/close-orders/{order_id}")


def costes(cuerpo, f=api):
    """Costes previstos (what-if, NO ejecuta): {costType: USD}."""
    r = f("POST", "/api/v2/trading/info/costs", cuerpo)
    return {c["costType"]: c["value"] for c in r.get("costs", [])}


# ---------------------------------------------------------------- órdenes (solo largos x1 de acciones reales)

def cuerpo_compra(iid, importe, stop, objetivo):
    return {"action": "open", "transaction": "buy", "instrumentId": int(iid), "settlementType": "real",
            "orderType": "mkt", "leverage": 1, "amount": round(float(importe), 2), "orderCurrency": "usd",
            "stopLossRate": stop, "takeProfitRate": objetivo, "stopLossType": "fixed"}


def abrir(iid, importe, stop, objetivo, ref, f=api):
    """Compra a mercado x1 por importe en USD con SL y TP en la MISMA orden. 202 = aceptada, no ejecutada."""
    return f("POST", "/api/v3/trading/execution/orders", cuerpo_compra(iid, importe, stop, objetivo), ref=ref)


def cerrar(position_id, iid, ref, f=api):
    """Cierre a mercado de la posición entera. Devuelve orderForClose (con orderID)."""
    r = f("POST", f"/api/v1/trading/execution/market-close-orders/positions/{position_id}",
          {"InstrumentId": int(iid), "UnitsToDeduct": None}, ref=ref)
    return r.get("orderForClose") or r


def fijar_sl_tp(position_id, sl, tp, f=api):
    """Cambia SL y TP (asíncrono: 202 con operationId). Confirmar releyendo la cartera."""
    return f("PATCH", f"/api/v2/trading/positions/{position_id}",
             {"stopLossRate": sl, "takeProfitRate": tp, "stopLossType": "fixed"})


# ---------------------------------------------------------------- datos públicos

def velas(iid, intervalo="OneHour", n=200):
    """[(t 'YYYY-MM-DDTHH:MM:SSZ', open, high, low, close, volumen)] ascendentes. Intervalos: OneHour, OneDay…"""
    d = get_json(URL_VELAS.format(intervalo=intervalo, n=n, id=int(iid)), timeout=30)
    bloques = (d or {}).get("Candles") or []
    filas = bloques[0]["Candles"] if bloques else []
    return [(c["FromDate"][:19] + "Z", float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"]),
             float(c.get("Volume") or 0)) for c in filas]


def _cacheado(nombre, url, max_horas, timeout=60):
    f = CACHE / nombre
    if f.exists() and time.time() - f.stat().st_mtime < max_horas * 3600:
        return json.loads(f.read_text())
    datos = get_json(url, timeout=timeout)
    CACHE.mkdir(exist_ok=True)
    f.write_text(json.dumps(datos))
    return datos


def catalogo_acciones(max_horas=24 * 6):
    """Acciones comprables al contado (sin CFD) del catálogo público: [{id, simbolo, nombre, bolsa_id, bolsa,
    sector, inversores}]. Descarga ~75 MB; se cachea `max_horas`."""
    meta = _cacheado("meta.json", URL_META, max_horas)["InstrumentDisplayDatas"]
    trading = _cacheado("trading.json", URL_TRADING, max_horas, timeout=300)
    grupos = _cacheado("grupos.json", URL_GRUPOS, 24 * 7)
    inversores = {u["instrumentId"]: u for u in _cacheado("inversores.json", URL_INVERSORES, max_horas)}
    industrias = {i["IndustryID"]: i["IndustryName"] for i in grupos.get("StocksIndustries", [])}
    bolsas = {e["ExchangeID"]: e["ExchangeDescription"] for e in grupos.get("ExchangeInfo", [])}
    tr = {t["InstrumentID"]: t for t in trading["Instruments"]}
    out = []
    for m in meta:
        t = tr.get(m["InstrumentID"])
        if (not t or m["InstrumentTypeID"] != 5 or t.get("IsDelisted") or not t.get("AllowBuy")
                or not t.get("IsNonLeveragedBuyAllowed") or m.get("IsInternalInstrument")):
            continue
        out.append({"id": m["InstrumentID"], "simbolo": m["SymbolFull"].upper(), "nombre": m["InstrumentDisplayName"],
                    "bolsa_id": m.get("ExchangeID"), "bolsa": bolsas.get(m.get("ExchangeID"), ""),
                    "sector": industrias.get(m.get("StocksIndustryID"), ""),
                    "inversores": int(inversores.get(m["InstrumentID"], {}).get("total", 0)),
                    "minimo_usd": t.get("MinPositionAmount") or 10})
    return out
