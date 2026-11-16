"""Horario de las bolsas que opera la mesa (EE. UU. y zona euro) y sus festivos.

Regiones (por ExchangeID de eToro):
- EEUU (4 NASDAQ, 5 NYSE): 9:30-16:00 hora de Nueva York (15:30-22:00 en Madrid salvo los días de desfase de horario
  de verano). Festivos de la NYSE (paquete `holidays`, mercado XNYS); cierre a las 13:00 el día después de Acción de
  Gracias, el 24-dic y el 3-jul si son laborables.
- UE: 9:00-17:30 hora de Madrid. Xetra (6) y Madrid (10) con sus calendarios (XETR, XMAD); Euronext (9 París,
  30 Ámsterdam, 23 Bruselas, 22 Lisboa) con el de TARGET (TAR); Milán (11) con TARGET + 15-ago, 24-dic y 31-dic.
Fuera de estas bolsas (Londres por el impuesto de timbre del 0,5 %, Zúrich, nórdicas, Hong Kong…) la mesa no opera.
`margen_apertura_min` / `margen_cierre_min` (config.horario) evitan los primeros y últimos minutos (huecos y
diferenciales anchos).
"""
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays

NY, MADRID = ZoneInfo("America/New_York"), ZoneInfo("Europe/Madrid")
BOLSAS = {4: ("EEUU", "XNYS"), 5: ("EEUU", "XNYS"), 6: ("UE", "XETR"), 10: ("UE", "XMAD"), 9: ("UE", "TAR"),
          30: ("UE", "TAR"), 23: ("UE", "TAR"), 22: ("UE", "TAR"), 11: ("UE", "XMIL")}
MONEDA = {"EEUU": "USD", "UE": "EUR"}
SESION = {"EEUU": (NY, dtime(9, 30), dtime(16, 0)), "UE": (MADRID, dtime(9, 0), dtime(17, 30))}


def region(bolsa_id):
    b = BOLSAS.get(int(bolsa_id or 0))
    return b[0] if b else None


@lru_cache(maxsize=64)
def _festivos(cal, anio):
    if cal == "XMIL":
        f = dict(holidays.financial_holidays("TAR", years=anio))
        for m, d, n in ((8, 15, "Ferragosto"), (12, 24, "Nochebuena"), (12, 31, "Nochevieja")):
            f.setdefault(date(anio, m, d), n)
        return f
    f = dict(holidays.financial_holidays(cal, years=anio))
    if cal in ("XETR", "XMAD", "TAR"):                    # Xetra, BME y Euronext no abren el 24 ni el 31-dic
        for m, d in ((12, 24), (12, 31)):
            if cal != "TAR" or date(anio, m, d).weekday() < 5:
                f.setdefault(date(anio, m, d), "cerrado")
    return f


def festivo(bolsa_id, d):
    b = BOLSAS.get(int(bolsa_id or 0))
    return None if not b else _festivos(b[1], d.year).get(d)


def _cierre_ny(d, fin):
    """13:00 en las sesiones cortas de la NYSE."""
    acc = [x for x, n in holidays.financial_holidays("XNYS", years=d.year).items() if "Thanksgiving" in str(n)]
    cortos = {a + timedelta(days=1) for a in acc} | {date(d.year, 12, 24), date(d.year, 7, 3)}
    return dtime(13, 0) if d in cortos else fin


def sesion(bolsa_id, d):
    """(apertura, cierre) aware de la sesión del día `d` (fecha local de la bolsa), o None si no abre."""
    r = region(bolsa_id)
    if not r or d.weekday() >= 5 or festivo(bolsa_id, d):
        return None
    tz, ini, fin = SESION[r]
    if r == "EEUU":
        fin = _cierre_ny(d, fin)
    return datetime.combine(d, ini, tz), datetime.combine(d, fin, tz)


def abierto(bolsa_id, t=None, margen_ap=0, margen_ci=0):
    """True si la bolsa está en sesión en `t` (aware; por defecto ahora), con márgenes en minutos."""
    t = t or datetime.now(MADRID)
    r = region(bolsa_id)
    if not r:
        return False
    s = sesion(bolsa_id, t.astimezone(SESION[r][0]).date())
    return bool(s) and s[0] + timedelta(minutes=margen_ap) <= t <= s[1] - timedelta(minutes=margen_ci)


def abierta_region(reg, t=None, margen_ap=0, margen_ci=0):
    bolsa = 5 if reg == "EEUU" else 6
    return abierto(bolsa, t, margen_ap, margen_ci)


def alguna_abierta(t=None, margen_ap=0, margen_ci=0):
    return any(abierto(b, t, margen_ap, margen_ci) for b in (5, 6, 10, 9, 11))


def proxima_apertura(bolsa_id, t=None):
    t = t or datetime.now(MADRID)
    r = region(bolsa_id)
    d = t.astimezone(SESION[r][0]).date()
    for k in range(10):
        s = sesion(bolsa_id, d + timedelta(days=k))
        if s and s[1] > t:
            return max(s[0], t) if s[0] > t else s[0]
    return None


def estado_txt(t=None):
    t = t or datetime.now(MADRID)
    partes = []
    for nombre, b in (("EE. UU.", 5), ("Xetra", 6), ("Euronext", 9), ("Madrid", 10), ("Milán", 11)):
        if abierto(b, t):
            partes.append(f"{nombre} abierta")
        else:
            p = proxima_apertura(b, t)
            partes.append(f"{nombre} cerrada{f' (abre {p.astimezone(MADRID):%a %d %H:%M})' if p else ''}"
                          + (f" [festivo: {festivo(b, t.astimezone(SESION[region(b)][0]).date())}]"
                             if festivo(b, t.astimezone(SESION[region(b)][0]).date()) else ""))
    return " · ".join(partes)
