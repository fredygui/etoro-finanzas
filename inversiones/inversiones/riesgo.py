"""Indicadores de riesgo del informe semanal, calculados con las velas diarias de eToro (sin IA)."""
import json
import math
import urllib.request
from datetime import date, timedelta

from . import etoro

REFERENCIAS = {"SPY": "SPY", "petroleo": "OIL", "oro": "GOLD", "btc": "BTC"}
UMBRAL = {"vol": 50.0, "caida52": -20.0, "peso": 35.0, "semana": -10.0, "sl": 5.0}


def _antes(v, dias, ref=None):
    """Último cierre con fecha <= ref - dias (v = [(fecha, cierre, vol)])."""
    if not v:
        return None
    lim = (date.fromisoformat(ref or v[-1][0]) - timedelta(days=dias)).isoformat()
    prev = [c for f, c, _ in v if f <= lim]
    return prev[-1] if prev else None


def _rent(v, dias):
    p0 = _antes(v, dias)
    return round((v[-1][1] / p0 - 1) * 100, 1) if p0 else None


def _retornos(cierres):
    return [math.log(b / a) for a, b in zip(cierres, cierres[1:]) if a > 0 and b > 0]


def _std(x):
    if len(x) < 2:
        return None
    m = sum(x) / len(x)
    return math.sqrt(sum((i - m) ** 2 for i in x) / (len(x) - 1))


def _alineada(v, fechas):
    """Cierres de v en `fechas` (con arrastre del último cierre conocido)."""
    out, i, ult = [], 0, None
    for f in fechas:
        while i < len(v) and v[i][0] <= f:
            ult = v[i][1]
            i += 1
        out.append(ult)
    return out


def metricas(v, spy, n=60):
    """Indicadores de una serie de velas frente al SPY. Devuelve {} si no hay datos suficientes."""
    if len(v) < 25:
        return {}
    cierres = [c for _, c, _ in v]
    vol = _std(_retornos(cierres[-31:]))
    max52 = max(cierres[-252:])
    m = {"precio": round(cierres[-1], 2), "r1s": _rent(v, 7), "r1m": _rent(v, 30),
         "vol30": round(vol * math.sqrt(252) * 100, 1) if vol else None,
         "caida52": round((cierres[-1] / max52 - 1) * 100, 1), "beta": None, "corr": None}
    if spy and len(spy) > n:
        fechas = [f for f, _, _ in spy[-n:]]
        a, b = _alineada(v, fechas), [c for _, c, _ in spy[-n:]]
        if None not in a:
            ra, rb = _retornos(a), _retornos(b)
            sa, sb = _std(ra), _std(rb)
            if sa and sb and len(ra) == len(rb):
                ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
                cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / (len(ra) - 1)
                m["beta"], m["corr"] = round(cov / sb ** 2, 2), round(cov / (sa * sb), 2)
    return m


def vix():
    """VIX: Yahoo (gratis) y, si falla, el futuro del VIX en eToro. Devuelve {valor, hace_1s, fuente} o {}."""
    try:
        req = urllib.request.Request("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d",
                                     headers=etoro.UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)["chart"]["result"][0]
        cierres = [c for c in res["indicators"]["quote"][0]["close"] if c]
        return {"valor": round(res["meta"]["regularMarketPrice"], 1), "hace_1s": round(cierres[-6], 1) if len(cierres) > 5 else None,
                "fuente": "Yahoo ^VIX"}
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return {}


def vix_etoro(cat):
    for s in sorted(k for k in etoro.por_simbolo(cat) if k.startswith("VIX.")):
        v = etoro.velas(etoro.por_simbolo(cat)[s]["id"], 40)
        if v:
            return {"valor": round(v[-1][1], 1), "hace_1s": round(_antes(v, 7), 1) if _antes(v, 7) else None,
                    "fuente": f"eToro {s} (futuro)"}
    return {}


def mercado(series, cat):
    """Semáforo global: VIX, SPY frente a la SMA200 y movimiento semanal de SPY, petróleo, oro y BTC."""
    v = vix() or vix_etoro(cat)
    spy = series.get("SPY") or []
    sma = round(sum(c for _, c, _ in spy[-200:]) / 200, 2) if len(spy) >= 200 else None
    fila = {"vix": v, "spy": spy[-1][1] if spy else None, "sma200": sma,
            "spy_vs_sma200": round((spy[-1][1] / sma - 1) * 100, 1) if spy and sma else None,
            "semana": {k: _rent(series.get(k) or [], 7) for k in REFERENCIAS}}
    puntos, motivos = [], []

    def nivel(n, txt):
        puntos.append(n)
        if n:
            motivos.append(txt)

    if v.get("valor") is not None:
        nivel(2 if v["valor"] > 25 else 1 if v["valor"] >= 18 else 0, f"VIX {v['valor']}")
    if fila["spy_vs_sma200"] is not None:
        d = fila["spy_vs_sma200"]
        nivel(2 if d < -3 else 1 if d < 0 else 0, f"SPY {d:+.1f} % frente a su media de 200 sesiones")
    s = fila["semana"]
    if s["SPY"] is not None:
        nivel(2 if s["SPY"] < -6 else 1 if s["SPY"] < -3 else 0, f"SPY {s['SPY']:+.1f} % en la semana")
    for k, lim in (("petroleo", 8), ("btc", 10), ("oro", 5)):
        if s[k] is not None and abs(s[k]) > lim:
            nivel(1, f"{k} {s[k]:+.1f} % en la semana")
    rojo = 2 in puntos or sum(1 for p in puntos if p) >= 3
    fila["semaforo"] = "rojo" if rojo else "ambar" if any(puntos) else "verde"
    fila["motivos"] = motivos
    return fila


def cartera(pos, mets, series):
    """pos = [{simbolo, valor_usd, peso, ...}]; devuelve riesgo de la cartera: volatilidad, rentabilidades, beta, HHI, pesos."""
    pesos = [p["peso"] for p in pos]
    out = {"peso_max": round(max(pesos), 1) if pesos else None, "simbolo_max": max(pos, key=lambda p: p["peso"])["simbolo"] if pos else None,
           "hhi": round(sum((w / 100) ** 2 for w in pesos), 3) if pesos else None,
           "n_valores": len(pos)}
    pesos_ok = [(p, mets.get(p["simbolo"]) or {}) for p in pos]
    for k in ("r1s", "r1m", "beta"):
        con = [(p["peso"], m[k]) for p, m in pesos_ok if m.get(k) is not None]
        t = sum(w for w, _ in con)
        out[k] = round(sum(w * x for w, x in con) / t, 2 if k == "beta" else 1) if t else None
    # volatilidad de la cartera con los retornos diarios ponderados (30 sesiones, fechas de SPY)
    spy = series.get("SPY") or []
    if len(spy) > 40 and pos:
        fechas = [f for f, _, _ in spy[-32:]]
        ret, tw = [], 0.0
        cols = []
        for p in pos:
            v = series.get(p["simbolo"]) or []
            a = _alineada(v, fechas) if v else None
            if a and None not in a:
                cols.append((p["peso"], _retornos(a)))
                tw += p["peso"]
        if cols and tw:
            ret = [sum(w * r[i] for w, r in cols) / tw for i in range(len(cols[0][1]))]
            sd = _std(ret)
            out["vol30"] = round(sd * math.sqrt(252) * 100, 1) if sd else None
    return out


def alertas(pos, mets, resultados, mkt, cart):
    """Lista de textos de alerta de la cartera."""
    out = []
    for p in pos:
        m, s = mets.get(p["simbolo"]) or {}, p["simbolo"]
        if m.get("vol30") is not None and m["vol30"] > UMBRAL["vol"]:
            out.append(f"{s}: volatilidad alta ({m['vol30']:.0f} % anualizada a 30 días)")
        if m.get("caida52") is not None and m["caida52"] < UMBRAL["caida52"]:
            out.append(f"{s}: cae {abs(m['caida52']):.0f} % desde su máximo de 52 semanas")
        if m.get("r1s") is not None and m["r1s"] < UMBRAL["semana"]:
            out.append(f"{s}: {m['r1s']:.1f} % en la última semana")
        if p["peso"] > UMBRAL["peso"]:
            out.append(f"{s}: pesa {p['peso']:.0f} % de la cartera (concentración)")
        if p.get("dist_sl") is not None and p["dist_sl"] < UMBRAL["sl"]:
            out.append(f"{s}: a solo {p['dist_sl']:.1f} % de su stop loss")
        if p.get("apalancado"):
            out.append(f"{s}: posición apalancada")
        r = resultados.get(s)
        if r:
            out.append(f"{s}: presenta resultados el {r['fecha']} {r['hora']}".strip())
    if cart.get("hhi") and cart["hhi"] > 0.25:
        out.append(f"Cartera concentrada (HHI {cart['hhi']})")
    if mkt["semaforo"] != "verde":
        out.append(f"Mercado en semáforo {mkt['semaforo']}: " + "; ".join(mkt["motivos"]))
    return out
