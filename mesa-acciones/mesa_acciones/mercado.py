"""Datos de mercado e indicadores para el Scout y CHARTS (velas públicas de eToro, sin cuota de la API).

Velas diarias (~260, para EMA/SMA200, RSI, ATR, rangos y volumen típico) y horarias (~60, para el momento intradía y
la frescura). Los precios van en la moneda del instrumento (USD en EE. UU., EUR en la zona euro).
"""
import time
from concurrent.futures import ThreadPoolExecutor
from statistics import median

from . import etoro
from .comun import de_iso


def _ema(xs, n):
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def _rsi(c, n=14):
    g = p = 0.0
    for a, b in zip(c[-n - 1:-1], c[-n:]):
        d = b - a
        g, p = g + max(d, 0), p + max(-d, 0)
    return 100.0 if p == 0 else 100 - 100 / (1 + g / p)


def _atr(v, n=14):
    trs = [max(h - l, abs(h - pc), abs(l - pc)) for (_, _, h, l, _, _), (_, _, _, _, pc, _) in zip(v[-n:], v[-n - 1:-1])]
    return sum(trs) / n


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if b else None


def analizar(inst, px=None):
    """Métricas de un instrumento del universo ({id, simbolo, bolsa_id, region, moneda…}). `px` = {bid, ask, fecha}
    de la API (si no, precio = último cierre). Lanza IndexError si hay menos de 60 días de historia."""
    d = etoro.velas(inst["id"], "OneDay", 260)
    h = etoro.velas(inst["id"], "OneHour", 60)
    if len(d) < 60:
        raise IndexError("menos de 60 días de historia")
    # sesión «de hoy» = fecha de la última vela horaria; las diarias de esa fecha o posteriores (eToro abre la del día
    # siguiente con el after-hours de EE. UU.) no cuentan como cerradas
    hoy = h[-1][0][:10] if h else d[-1][0][:10]
    cerr = [x for x in d if x[0][:10] < hoy]
    c = [x[4] for x in cerr]
    p = (px or {}).get("bid") or (h[-1][4] if h else c[-1])
    vols_usd = [x[5] * x[4] for x in cerr[-21:-1]]
    med = median(vols_usd) if vols_usd else 0
    vol_ult = cerr[-1][5] * cerr[-1][4]
    hoy_vol = sum(x[5] * x[4] for x in h if x[0][:10] == hoy) if h else 0
    m = {
        "simbolo": inst["simbolo"], "id": inst["id"], "bolsa_id": inst.get("bolsa_id"),
        "minimo_usd": inst.get("minimo_usd"), "nombre": inst.get("nombre", ""), "region": inst.get("region"),
        "moneda": inst.get("moneda", "USD"), "sector": inst.get("sector", ""),
        "precio": round(p, 4),
        "mov_1d_pct": _pct(p, c[-1]), "mov_5d_pct": _pct(p, c[-6]), "mov_20d_pct": _pct(p, c[-21]),
        "mov_60d_pct": _pct(p, c[-61]) if len(c) > 61 else None,
        "vol_ratio_ultimo_dia": round(vol_ult / med, 2) if med else None,
        "vol_hoy_ratio": round(hoy_vol / med, 2) if med else None,
        "vol_mediana_20d": round(med),
        "ema20": round(_ema(c[-120:], 20), 4), "ema50": round(_ema(c[-200:], 50), 4),
        "sma200": round(sum(c[-200:]) / len(c[-200:]), 4) if len(c) >= 200 else None,
        "rsi14": round(_rsi(c), 1), "atr14_pct": round(_atr(cerr) / p * 100, 2),
        "max_20d": max(x[2] for x in cerr[-20:]), "min_20d": min(x[3] for x in cerr[-20:]),
        "max_52s": max(x[2] for x in cerr[-252:]), "min_52s": min(x[3] for x in cerr[-252:]),
        "ultimos_5d": [[x[0][:10], round(x[1], 4), round(x[2], 4), round(x[3], 4), round(x[4], 4)] for x in cerr[-5:]],
        "ultimas_6h": [[x[0][11:16], round(x[1], 4), round(x[2], 4), round(x[3], 4), round(x[4], 4)] for x in h[-6:]],
        "vela_hora": h[-1][0] if h else None,
        "analizado": int(time.time()),
    }
    m["dist_max20_pct"] = _pct(p, m["max_20d"])
    m["dist_max52s_pct"] = _pct(p, m["max_52s"])
    m["ruptura_20d"] = p > max(x[2] for x in cerr[-20:])
    if px:
        m.update(bid=px["bid"], ask=px["ask"], diferencial_pct=round((px["ask"] - px["bid"]) / px["ask"] * 100, 3),
                 precio_fecha=px.get("fecha"))
    return m


def analizar_varios(insts, precios, hilos=6):
    """{simbolo: métricas} en paralelo (los que fallan se omiten)."""
    def uno(i):
        try:
            return analizar(i, precios.get(i["id"]))
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
            return None
    with ThreadPoolExecutor(hilos) as ex:
        return {m["simbolo"]: m for m in ex.map(uno, insts) if m}


def edad_precio_s(px, ahora_ts=None):
    """Segundos desde la última cotización (campo `fecha` de la API) o None."""
    try:
        return (ahora_ts or time.time()) - de_iso(px["fecha"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
