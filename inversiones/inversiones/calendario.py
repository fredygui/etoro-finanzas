"""Calendario de presentación de resultados (Nasdaq, acciones de EE. UU.) para los días hábiles de un mes."""
import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import etoro

URL = "https://api.nasdaq.com/api/calendar/earnings?date={}"


def _dia(d):
    try:
        filas = ((etoro.get_json(URL.format(d.isoformat()), timeout=30) or {}).get("data") or {}).get("rows") or []
    except (OSError, ValueError):
        return d, None
    return d, filas


def _cap(s):
    try:
        return int(s.replace("$", "").replace(",", ""))
    except (AttributeError, ValueError):
        return 0


def resultados_mes(anio, mes, desde=None):
    """{símbolo: {fecha, hora, eps_previsto, capitalizacion}} y lista de días que no se pudieron descargar."""
    ultimo = calendar.monthrange(anio, mes)[1]
    dias = [date(anio, mes, d) for d in range(1, ultimo + 1)
            if date(anio, mes, d).weekday() < 5 and (not desde or date(anio, mes, d) >= desde)]
    out, fallidos = {}, []
    with ThreadPoolExecutor(4) as ex:
        for d, filas in ex.map(_dia, dias):
            if filas is None:
                fallidos.append(d.isoformat())
                continue
            for f in filas:
                out[f["symbol"].upper()] = {
                    "fecha": d.isoformat(),
                    "hora": {"time-pre-market": "antes de apertura", "time-after-hours": "tras el cierre"}
                    .get(f.get("time"), ""),
                    "eps_previsto": f.get("epsForecast") or "",
                    "capitalizacion": _cap(f.get("marketCap")),
                }
    return out, fallidos


def resultados_rango(desde, hasta):
    """{símbolo: {fecha, hora, eps_previsto, capitalizacion}} de los días hábiles entre `desde` y `hasta` (incl.)."""
    from datetime import timedelta
    dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1) if (desde + timedelta(days=i)).weekday() < 5]
    out, fallidos = {}, []
    with ThreadPoolExecutor(4) as ex:
        for d, filas in ex.map(_dia, dias):
            if filas is None:
                fallidos.append(d.isoformat())
                continue
            for f in filas:
                out[f["symbol"].upper()] = {
                    "fecha": d.isoformat(),
                    "hora": {"time-pre-market": "antes de apertura", "time-after-hours": "tras el cierre"}.get(f.get("time"), ""),
                    "eps_previsto": f.get("epsForecast") or "", "capitalizacion": _cap(f.get("marketCap"))}
    return out, fallidos
