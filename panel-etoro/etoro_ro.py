"""Cliente eToro de SOLO LECTURA para el panel: únicamente peticiones GET, con caché.

Claves: ~/.config/inversiones/etoro.env (solo las del titular). No existe ningún método que abra, cierre o modifique
posiciones ni SL/TP: `_get` es el único punto de salida a la API y fuerza el método GET.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://public-api.etoro.com"
CLAVES = Path.home() / ".config/inversiones/etoro.env"
URL_VELAS = "https://candle.etoro.com/candles/asc.json/{intervalo}/{n}/{id}"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) panel-etoro/1.0", "Accept": "application/json",
      "Accept-Encoding": "identity"}
TTL_API = 60          # s mínimos entre llamadas iguales a la API autenticada
TTL_VELAS = 300
_cache, _lock = {}, threading.Lock()


def _claves():
    env = dict(l.strip().split("=", 1) for l in CLAVES.read_text().splitlines() if "=" in l)
    return env["ETORO_API_KEY"].strip(), env["ETORO_USER_KEY"].strip()


def _http(url, headers, timeout=25):
    req = urllib.request.Request(url, headers={**UA, **headers}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _cacheado(clave, ttl, fn):
    """Devuelve (datos, edad_s). Si la llamada falla y hay datos antiguos, los sirve (edad creciente)."""
    with _lock:
        t, d = _cache.get(clave, (0, None))
        if d is not None and time.time() - t < ttl:
            return d, int(time.time() - t)
        try:
            d = fn()
            _cache[clave] = (time.time(), d)
            return d, 0
        except (OSError, ValueError, KeyError):
            if d is not None:
                return d, int(time.time() - t)
            raise


def _get(ruta):
    k, u = _claves()
    import uuid
    return _http(API + ruta, {"x-api-key": k, "x-user-key": u, "x-request-id": str(uuid.uuid4())})


def cartera():
    """(clientPortfolio, edad_s): positions, credit…"""
    return _cacheado("cartera", TTL_API, lambda: _get("/api/v1/trading/info/real/pnl")["clientPortfolio"])


def precios(ids):
    ids = sorted(int(i) for i in ids)

    def f():
        r = _get("/api/v2/market-data/rates?instrumentIds=" + ",".join(map(str, ids)))
        return {int(x["instrumentId"]): {"bid": float(x["bid"]), "ask": float(x["ask"]), "fecha": x.get("date")}
                for x in r.get("results") or [] if x.get("bid") and x.get("ask")}
    return _cacheado(("precios", tuple(ids)), TTL_API, f)


def historial(desde):
    def f():
        r = _get(f"/api/v1/trading/info/trade/history?minDate={desde}&pageSize=500")
        return r if isinstance(r, list) else r.get("items") or r.get("trades") or []
    return _cacheado(("hist", desde), 300, f)


def velas(iid, intervalo="OneHour", n=120):
    """[[t, o, h, l, c], …] ascendentes. Fuente pública sin clave."""
    def f():
        d = _http(URL_VELAS.format(intervalo=intervalo, n=n, id=int(iid)), {})
        bl = (d or {}).get("Candles") or []
        filas = bl[0]["Candles"] if bl else []
        return [[c["FromDate"][:19] + "Z", float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"])]
                for c in filas]
    return _cacheado(("velas", int(iid), intervalo, n), TTL_VELAS, f)
