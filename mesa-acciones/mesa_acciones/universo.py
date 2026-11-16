"""Universo de la mesa: acciones líquidas de eToro de EE. UU. y la zona euro (se recalcula cada semana).

1. Catálogo público de eToro → acciones comprables al contado (sin CFD) en las bolsas de `horario.BOLSAS`.
2. Preselección por popularidad en eToro (nº de inversores): `preseleccion_eeuu` + `preseleccion_ue`.
3. Liquidez con velas diarias: precio ≥ `precio_min` y volumen mediano de 20 días ≥ `vol_min_eeuu` (USD) /
   `vol_min_ue` (EUR). Se quedan los `max_eeuu` + `max_ue` de más volumen.
El diferencial (≤ scout.diferencial_max_pct) se filtra en cada pasada del Scout con el precio en vivo.
Resultado en data/universo.json. Las posiciones abiertas se gestionan aunque no estén en el universo.
"""
from concurrent.futures import ThreadPoolExecutor
from statistics import median

from . import etoro, horario
from .comun import DATA, guardar, iso, leer, log

RUTA = DATA / "universo.json"


def cargar():
    return leer(RUTA, {}).get("instrumentos", [])


def por_simbolo():
    return {i["simbolo"]: i for i in cargar()}


def por_id():
    return {i["id"]: i for i in cargar()}


def _liquidez(i):
    try:
        v = etoro.velas(i["id"], "OneDay", 25)
    except (OSError, ValueError, KeyError):
        return i, None, None
    if len(v) < 15:
        return i, None, None
    return i, v[-1][4], median(x[5] * x[4] for x in v[-21:-1])


def construir(c):
    u = c["universo"]
    cat = etoro.catalogo_acciones()
    excl = {s.upper() for s in u.get("excluir", [])}
    out = []
    for reg in ("EEUU", "UE"):
        pre = sorted((i for i in cat if horario.region(i["bolsa_id"]) == reg and i["simbolo"] not in excl),
                     key=lambda i: -i["inversores"])[:u[f"preseleccion_{reg.lower()}"]]
        with ThreadPoolExecutor(8) as ex:
            medidas = list(ex.map(_liquidez, pre))
        vmin = u[f"vol_min_{reg.lower()}"]
        buenas = [(i, px, vol) for i, px, vol in medidas if px and px >= u["precio_min"] and vol and vol >= vmin]
        buenas.sort(key=lambda x: -x[2])
        for i, px, vol in buenas[:u[f"max_{reg.lower()}"]]:
            out.append({**i, "region": reg, "moneda": horario.MONEDA[reg], "vol_mediana_20d": round(vol),
                        "precio_ref": px})
        log(f"universo {reg}: {len(pre)} preseleccionadas → {len(buenas)} líquidas → {min(len(buenas), u[f'max_{reg.lower()}'])}")
    if out:
        guardar(RUTA, {"generado": iso(), "instrumentos": out})
    return out


def instrumento(iid, cat_cache={}):
    """Datos mínimos de cualquier instrumento (también fuera del universo) para gestionar una posición."""
    iid = int(iid)
    u = por_id()
    if iid in u:
        return u[iid]
    if not cat_cache:
        try:
            cat_cache.update({i["id"]: i for i in etoro.catalogo_acciones()})
        except (OSError, ValueError, KeyError) as e:
            log(f"universo: catálogo no disponible ({str(e)[:100]})")
    i = cat_cache.get(iid)
    if not i:
        return None
    reg = horario.region(i["bolsa_id"])
    return {**i, "region": reg, "moneda": horario.MONEDA.get(reg, "USD")}
