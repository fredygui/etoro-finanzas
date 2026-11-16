"""Datos públicos de eToro: catálogo de instrumentos, nº de inversores por instrumento, cotizaciones y velas.

Endpoints sin autenticación que usa la web de eToro. Se cachean en cache/ (el catálogo cambia poco).
La cartera real se lee de la API pública oficial (`cuenta`); con clave de escritura, `abrir`/`cerrar`/`fijar_tp_sl` operan\nen la cuenta REAL (los usa ejecutar.py).
"""
import json
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "cache"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept": "application/json",
      "Accept-Encoding": "identity"}

URL_META = "https://api.etorostatic.com/sapi/instrumentsmetadata/V1.1/instruments"
URL_TRADING = "https://www.etoro.com/sapi/trade-real/instruments?InstrumentDataFilters=Activity,Rates,TradingData"
URL_GRUPOS = "https://api.etorostatic.com/sapi/app-data/web-client/app-data/instruments-groups.json"
URL_INVERSORES = "https://www.etoro.com/sapi/insights/insights/uniques"
URL_VELAS = "https://candle.etoro.com/candles/asc.json/OneDay/{n}/{id}"
CLAVES = Path.home() / ".config/inversiones/etoro.env"      # ETORO_USER_KEY / ETORO_API_KEY (el titular: lectura y escritura desde 2026-11-09); por perfil: perfiles.py

TIPOS = {1: "Divisa", 2: "Materia prima", 4: "Índice", 5: "Acción", 6: "ETF", 10: "Cripto"}


def get_json(url, timeout=60, intentos=3):
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except (OSError, ValueError):
            if i == intentos - 1:
                raise
            time.sleep(2 * (i + 1))


API = "https://public-api.etoro.com"


class ErrorAPI(Exception):
    """Respuesta HTTP de error de la API de eToro (código y cuerpo)."""
    def __init__(self, codigo, cuerpo):
        super().__init__(f"HTTP {codigo}: {cuerpo[:300]}")
        self.codigo, self.cuerpo = codigo, cuerpo


def api(metodo, ruta, cuerpo=None, claves=None, timeout=60, ref=None):
    """Llamada autenticada a la API oficial (cuenta REAL). `ref` = x-request-id (idempotencia); por defecto uno nuevo.
    Reintenta solo en 429 (Retry-After). Devuelve el JSON (o {} si viene vacío)."""
    env = dict(l.strip().split("=", 1) for l in Path(claves or CLAVES).read_text().splitlines() if "=" in l)
    h = {**UA, "x-api-key": env["ETORO_API_KEY"], "x-user-key": env["ETORO_USER_KEY"],
         "x-request-id": ref or str(uuid.uuid4())}
    datos = None
    if cuerpo is not None:
        datos, h["Content-Type"] = json.dumps(cuerpo).encode(), "application/json"
    for intento in range(4):
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
            raise ErrorAPI(e.code, txt) from None
    raise ErrorAPI(429, "límite de peticiones")


def cuenta(claves=None):
    """Cartera real (posiciones con P/L, órdenes pendientes y efectivo) desde la API pública de eToro.
    `claves` = fichero .env del perfil (por defecto el del titular). Sin fichero lanza FileNotFoundError."""
    return api("GET", "/api/v1/trading/info/real/pnl", claves=claves)["clientPortfolio"]


# ---------------------------------------------------------------- operativa (clave con permiso de escritura)

def costes(cuerpo, claves=None):
    """Costes previstos (what-if, no ejecuta): {costType: valor USD}. cuerpo = el de abrir o {action: close, positionIds}."""
    r = api("POST", "/api/v2/trading/info/costs", cuerpo, claves)
    return {c["costType"]: c["value"] for c in r.get("costs", [])}


def abrir(iid, importe, claves=None, ref=None, real=True):
    """Compra a mercado x1 por importe (USD). Devuelve {orderId, referenceId}: aceptada, aún no ejecutada."""
    return api("POST", "/api/v3/trading/execution/orders", {
        "action": "open", "transaction": "buy", "instrumentId": iid, "settlementType": "real" if real else "cfd",
        "orderType": "mkt", "leverage": 1, "amount": round(importe, 2), "orderCurrency": "usd"}, claves, ref=ref)


def consultar_orden(order_id, claves=None):
    """Estado de una orden de apertura (status.id: 3/5 ejecutada, 4/10 rechazada, 7/8/9 cancelada/caducada,
    1/2/11/12 en curso; 11 = esperando a que abra el mercado)."""
    return api("GET", f"/api/v2/trading/info/orders:lookup?orderId={order_id}", claves=claves)


def cancelar_orden(order_id, claves=None):
    return api("DELETE", f"/api/v2/trading/execution/orders/{order_id}", claves=claves)


def cerrar(position_id, iid, unidades=None, claves=None, ref=None):
    """Cierra una posición entera (unidades=None) o en parte. Devuelve orderForClose (con orderID)."""
    return api("POST", f"/api/v1/trading/execution/market-close-orders/positions/{position_id}",
               {"InstrumentId": iid, "UnitsToDeduct": unidades}, claves, ref=ref)["orderForClose"]


def consultar_cierre(order_id, claves=None):
    """Resultado de un cierre: statusID, errorCode/errorMessage, proceeds y positions[{rate, units, amount}]."""
    return api("GET", f"/api/v1/trading/info/real/close-orders/{order_id}", claves=claves)


def fijar_tp_sl(position_id, tp=None, sl=None, claves=None):
    cuerpo = {k: v for k, v in (("takeProfitRate", tp), ("stopLossRate", sl)) if v is not None}
    if sl is not None:
        cuerpo["stopLossType"] = "fixed"
    return api("PATCH", f"/api/v2/trading/positions/{position_id}", cuerpo, claves)


def _cacheado(nombre, url, max_horas, timeout=60):
    f = CACHE / nombre
    if f.exists() and time.time() - f.stat().st_mtime < max_horas * 3600:
        return json.loads(f.read_text())
    datos = get_json(url, timeout=timeout)
    CACHE.mkdir(exist_ok=True)
    f.write_text(json.dumps(datos))
    return datos


def catalogo(max_horas=12):
    """Instrumentos comprables en eToro: {id: {...}} con símbolo, nombre, tipo, sector, inversores y precio."""
    meta = _cacheado("meta.json", URL_META, max_horas)["InstrumentDisplayDatas"]
    trading = _cacheado("trading.json", URL_TRADING, max_horas, timeout=300)
    grupos = _cacheado("grupos.json", URL_GRUPOS, 24 * 7)
    inversores = {u["instrumentId"]: u for u in _cacheado("inversores.json", URL_INVERSORES, max_horas)}
    industrias = {i["IndustryID"]: i["IndustryName"] for i in grupos.get("StocksIndustries", [])}
    bolsas = {e["ExchangeID"]: e["ExchangeDescription"] for e in grupos.get("ExchangeInfo", [])}
    precios = {r["InstrumentID"]: r for r in trading["Rates"]}
    tr = {t["InstrumentID"]: t for t in trading["Instruments"]}

    out = {}
    for m in meta:
        iid = m["InstrumentID"]
        t = tr.get(iid)
        # (InstrumentsToActivityState indica mercado abierto ahora, no si es operable)
        if (not t or t["IsDelisted"] or not t["AllowBuy"] or m.get("IsInternalInstrument") or m["InstrumentTypeID"] not in TIPOS):
            continue
        u = inversores.get(iid, {})
        out[iid] = {
            "id": iid,
            "simbolo": m["SymbolFull"],
            "nombre": m["InstrumentDisplayName"],
            "tipo": TIPOS[m["InstrumentTypeID"]],
            "sector": industrias.get(m.get("StocksIndustryID"), ""),
            "bolsa": bolsas.get(m.get("ExchangeID"), ""),
            "inversores": int(u.get("total", 0)),
            "inversores_crec": round(float(u.get("growth", 0)), 2),
            "pct_compra": u.get("buy"),
            "precio": precios.get(iid, {}).get("LastExecution"),
            # compra sin apalancamiento permitida (en acciones/ETF/cripto suele ser el activo real, sin CFD)
            "real": bool(t.get("IsNonLeveragedBuyAllowed")),
        }
    return out


def por_simbolo(cat):
    return {v["simbolo"].upper(): v for v in cat.values()}


def url_mercado(simbolo):
    return f"https://www.etoro.com/markets/{simbolo.lower()}"


def velas(iid, n=400):
    """Velas diarias [(fecha 'YYYY-MM-DD', cierre, volumen)] en orden ascendente."""
    d = get_json(URL_VELAS.format(n=n, id=iid), timeout=30)
    bloques = (d or {}).get("Candles") or []
    filas = bloques[0]["Candles"] if bloques else []
    return [(c["FromDate"][:10], float(c["Close"]), float(c.get("Volume") or 0)) for c in filas]


def velas_ohlc(iid, n=60):
    """Velas diarias [(fecha, apertura, mínimo, máximo, cierre)] ascendentes: para comprobar TP/SL alcanzados."""
    d = get_json(URL_VELAS.format(n=n, id=iid), timeout=30)
    bloques = (d or {}).get("Candles") or []
    filas = bloques[0]["Candles"] if bloques else []
    return [(c["FromDate"][:10], float(c["Open"]), float(c["Low"]), float(c["High"]), float(c["Close"]))
            for c in filas]


def velas_varios(ids, n=400, hilos=12, fn=velas):
    def una(iid):
        try:
            return iid, fn(iid, n)
        except (OSError, ValueError, KeyError):
            return iid, []
    with ThreadPoolExecutor(hilos) as ex:
        return dict(ex.map(una, ids))


@lru_cache(maxsize=1)
def _trading():
    return {t["InstrumentID"]: t for t in _cacheado("trading.json", URL_TRADING, 12, timeout=300)["Instruments"]}


def condiciones(iid):
    """Condiciones de operativa del instrumento: apalancamientos, mínimo por posición y comisión nocturna."""
    t = _trading().get(iid, {})
    return {"apalancamientos": t.get("Leverages") or [1], "minimo_usd": t.get("MinPositionAmount") or 10,
            # USD por unidad y noche (el fin de semana se cobra triple: ~30 noches al mes)
            "nocturna_apalancada": t.get("LeveragedBuyOverNightFee") or 0,
            "sl_max_apalancada": t.get("MaxStopLossPercentageLeveragedBuy") or 50}
