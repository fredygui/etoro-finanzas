"""Cribado cuantitativo del universo eToro: métricas técnicas por instrumento y lista corta para la investigación.

El cribado no predice nada por sí solo: selecciona candidatos con momentum/tendencia/acumulación (factores con
evidencia empírica a 1-12 meses) y caídas fuertes en activos populares, para que la investigación busque catalizadores.
"""
from datetime import date, timedelta
from statistics import mean, pstdev

from . import etoro

MIN_INVERSORES = 300          # filtro de liquidez/popularidad en eToro
MIN_VELAS = 120
CUOTAS = {"Acción": 60}       # el inversor solo opera acciones (ETF/cripto/materias primas solo como referencia)
N_REBOTE = 12
REFERENCIAS = ["SPY", "QQQ", "IWM", "DIA", "XLK", "SMH", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
               "XLRE", "XLC", "GLD", "SLV", "USO", "TLT", "EEM", "EWG", "BTC", "ETH", "SOL", "GOLD", "OIL"]
PESOS = {"mom_12_1": .25, "r3m": .20, "r1m": .10, "cerca_max": .15, "vol_ratio": .10, "inversores_crec": .07,
         "inversores": .08, "tendencia": .05}


def _cierre_en(v, dia):
    """Último cierre con fecha <= dia."""
    iso = dia.isoformat()
    previos = [c for f, c, _ in v if f <= iso]
    return previos[-1] if previos else None


def _rsi(cierres, n=14):
    if len(cierres) <= n:
        return None
    dif = [b - a for a, b in zip(cierres[-n - 1:-1], cierres[-n:])]
    sube = sum(d for d in dif if d > 0) / n
    baja = -sum(d for d in dif if d < 0) / n
    return 100.0 if baja == 0 else round(100 - 100 / (1 + sube / baja), 1)


def metricas(v, hoy=None):
    if len(v) < MIN_VELAS:
        return None
    hoy = hoy or date.fromisoformat(v[-1][0])
    cierres = [c for _, c, _ in v]
    vols = [x for _, _, x in v]
    ult = cierres[-1]
    if ult <= 0:
        return None

    def ret(dias, hasta=0):
        desde = hoy - timedelta(days=dias)
        if v[0][0] > desde.isoformat():
            return None               # sin histórico tan largo
        a = _cierre_en(v, desde)
        b = _cierre_en(v, hoy - timedelta(days=hasta)) if hasta else ult
        return round((b / a - 1) * 100, 1) if a and b else None

    diarios = [b / a - 1 for a, b in zip(cierres[-21:-1], cierres[-20:]) if a > 0]
    sma50, sma200 = mean(cierres[-50:]), (mean(cierres[-200:]) if len(cierres) >= 200 else None)
    v20, v120 = mean(vols[-20:]), mean(vols[-120:])
    max52 = max(cierres[-260:])
    return {
        "precio": ult, "fecha": v[-1][0],
        "r1m": ret(30), "r3m": ret(91), "r6m": ret(182), "r12m": ret(365), "mom_12_1": ret(365, 30),
        "cerca_max": round(ult / max52 * 100, 1),
        "volatilidad": round(pstdev(diarios) * (252 ** .5) * 100, 1) if len(diarios) > 5 else None,
        "rsi14": _rsi(cierres),
        "vol_ratio": round(v20 / v120, 2) if v120 else None,
        "sma50": round(sma50, 4), "sma200": round(sma200, 4) if sma200 else None,
        "tendencia": int(ult > sma50 and (sma200 is None or sma50 > sma200)),
    }


def _percentiles(filas, campo):
    vals = sorted(f[campo] for f in filas if f.get(campo) is not None)
    if not vals:
        return
    for f in filas:
        x = f.get(campo)
        f.setdefault("_p", {})[campo] = .5 if x is None else sum(1 for y in vals if y <= x) / len(vals)


def cribar(cat=None, log=print):
    cat = cat or etoro.catalogo()
    universo = [i for i in cat.values() if i["inversores"] >= MIN_INVERSORES and i["tipo"] in CUOTAS]
    idx = etoro.por_simbolo(cat)
    refs = [i for s in REFERENCIAS if (i := idx.get(s))]
    ids = {i["id"] for i in universo} | {i["id"] for i in refs}
    log(f"cribado: {len(cat)} instrumentos comprables, {len(universo)} con >= {MIN_INVERSORES} inversores; "
        f"descargando velas de {len(ids)}")
    todas = etoro.velas_varios(sorted(ids))

    filas = []
    for i in universo:
        m = metricas(todas.get(i["id"], []))
        if m:
            filas.append({**i, **m})
    log(f"cribado: {len(filas)} con histórico suficiente")

    lista, rebote = [], []
    for tipo, cupo in CUOTAS.items():
        grupo = [f for f in filas if f["tipo"] == tipo]
        for campo in PESOS:
            _percentiles(grupo, campo)
        for f in grupo:
            s = sum(PESOS[c] * f["_p"].get(c, .5) for c in PESOS)
            if (f["rsi14"] or 50) > 80:
                s -= .05                      # sobrecompra extrema: más riesgo de recogida de beneficios
            f["puntuacion"] = round(s * 100, 1)
        grupo.sort(key=lambda f: -f["puntuacion"])
        lista += grupo[:cupo]
        # caídas fuertes recientes en activos populares con tendencia larga aún sana
        rebote += [f for f in grupo if f["inversores"] >= 2000 and (f["r1m"] or 0) <= -12
                   and (f["r12m"] or 0) > 0 and (f["rsi14"] or 50) < 40]
    rebote.sort(key=lambda f: f["r1m"])
    elegidos = {f["id"] for f in lista}
    rebote = [f for f in rebote if f["id"] not in elegidos][:N_REBOTE]

    referencias = []
    for i in refs:
        m = metricas(todas.get(i["id"], []))
        if m:
            referencias.append({"simbolo": i["simbolo"], "nombre": i["nombre"],
                                **{k: m[k] for k in ("precio", "r1m", "r3m", "r12m", "cerca_max", "rsi14")}})

    limpiar = lambda f: {k: v for k, v in f.items() if not k.startswith("_")}
    return {
        "fecha": max((f["fecha"] for f in filas), default=date.today().isoformat()),
        "universo": len(filas),
        "momentum": [limpiar(f) for f in lista],
        "rebote": [limpiar(f) for f in rebote],
        "referencias": referencias,
        # lista compacta de símbolos operables para que la investigación no proponga nada fuera de eToro
        "operables": [f["simbolo"] for f in sorted(filas, key=lambda f: -f["inversores"])],
    }
