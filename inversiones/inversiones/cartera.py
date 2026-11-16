"""Base de datos de la cartera real en eToro (cartera.json, versionado) y plan de órdenes del día 1.

Operativa del inversor: solo acciones, aporta `aportacion_eur` al mes y solo opera el día 1 (las órdenes llevan
TP/SL que eToro ejecuta solo durante el mes). Cada compra es un lote (en eToro, una posición con su propio TP/SL).

Ciclo: el informe del mes M calcula un plan (pendiente). El informe de M+1 carga la cartera real de la cuenta de
eToro (API oficial, clave de solo lectura). Sin clave o si la API falla, da el plan por ejecutado a la apertura del
primer día de mercado >= día 1 de M y comprueba con velas diarias qué TP/SL saltaron; en ese caso, si no se ejecutó
tal cual, corrígelo con este CLI antes del día 1:

Multiperfil (titular, perfil2, perfil3): `--perfil <nombre>` (repetible o con comas). `ver` y `sincronizar` van por defecto a
todos; las órdenes que modifican datos (fijar, efectivo, aplicar, descartar) van por defecto a titular.

python -m inversiones.cartera ver [--perfil perfil2]
python -m inversiones.cartera sincronizar                                          # posiciones y efectivo reales (API)
python -m inversiones.cartera fijar MSFT 0.25 [--precio 520] [--tp 640 --sl 450]   # sustituye los lotes de MSFT
python -m inversiones.cartera efectivo 12.5                                        # efectivo libre en USD
python -m inversiones.cartera aplicar 2026-11 | descartar 2026-11                  # plan ejecutado / no ejecutado
"""
import argparse
import json
from datetime import date, datetime

from . import etoro, perfiles

FICHERO = perfiles.PERFILES["titular"].cartera      # compatibilidad: cartera del titular
CONFIG = {"aportacion_eur": 200, "comision_cambio_pct": 0.5, "max_compras": 5, "apalancamientos": [1, 2, 5],
          "max_apalancado_pct": 30,
          # costes reales de eToro (API /trading/info/costs, 2026-11-09): 1 USD al abrir, ~0,2 al cerrar, spread ~0,15-0,3%
          "comision_compra_usd": 1.0, "comision_venta_usd": 1.0, "spread_pct": 0.3,
          "colchon_pct": 2.0}       # margen por si el precio se mueve entre el informe y la ejecución
NOCHES_MES = 30          # eToro cobra triple la noche del viernes: ~30 cargos al mes


def cargar(perfil=None):
    perfil = perfil or perfiles.PERFILES["titular"]
    if perfil.cartera.exists():
        cart = json.loads(perfil.cartera.read_text())
    else:
        cart = {"efectivo_usd": 0.0, "posiciones": [], "planes": {}, "movimientos": []}
    cart["config"] = {**CONFIG, **perfil.config, **cart.get("config", {})}
    return cart


def guardar(cart, perfil=None):
    perfil = perfil or perfiles.PERFILES["titular"]
    perfil.cartera.write_text(json.dumps(cart, ensure_ascii=False, indent=1) + "\n")


def _mov(cart, fecha, tipo, simbolo, unidades=None, precio=None, importe=None, nota=""):
    cart["movimientos"].append({"fecha": fecha, "tipo": tipo, "simbolo": simbolo, "unidades": unidades,
                                "precio": precio, "importe_usd": None if importe is None else round(importe, 2),
                                "nota": nota})


def valor(lote, precio):
    """Valor del lote en USD (con apalancamiento: margen + P/L de la exposición). `fx` pasa la divisa del activo a USD
    (lo da eToro al sincronizar; p. ej. EUR/USD para las acciones de Fráncfort)."""
    if precio is None:
        return None
    fx = lote.get("fx") or 1
    if lote.get("apalancamiento", 1) == 1 or lote.get("precio_entrada") is None:
        return lote["unidades"] * precio * fx
    return lote["importe_usd"] + lote["unidades"] * (precio - lote["precio_entrada"]) * fx


def eurusd(cat):
    return etoro.por_simbolo(cat).get("EURUSD", {}).get("precio") or 1.10


# ---------------------------------------------------------------- aplicar planes y revisar TP/SL

def _primera_vela(v, desde):
    return next((x for x in v if x[0] >= desde), None)


def aplicar_plan(cart, mes, ohlc, log=print):
    """Da por ejecutado el plan del mes a la apertura del primer día de mercado >= fecha_ejecucion."""
    plan = cart["planes"][mes]
    hechas = []
    for o in sorted(plan["ordenes"], key=lambda o: o["tipo"] != "vender"):   # ventas primero
        vela = _primera_vela(ohlc.get(o["id"], []), plan["fecha_ejecucion"]) if o.get("id") else None
        if o["tipo"] != "ajustar" and not vela:
            log(f"cartera: {o['simbolo']} sin cotización desde {plan['fecha_ejecucion']}; plan de {mes} sigue pendiente")
            return False
        hechas.append((o, vela))
    cart["efectivo_usd"] += plan["aportacion_usd"]
    _mov(cart, plan["fecha_ejecucion"], "aportación", "", importe=plan["aportacion_usd"],
         nota=f"{cart['config']['aportacion_eur']} € a {plan['eurusd']}")
    for o, vela in hechas:
        lotes = [l for l in cart["posiciones"] if l["simbolo"] == o["simbolo"]]
        if o["tipo"] == "vender":
            frac, p = o["pct"] / 100, vela[1]
            for l in lotes:
                cobro = valor(l, p) * frac
                cart["efectivo_usd"] += cobro
                _mov(cart, vela[0], "venta", l["simbolo"], round(l["unidades"] * frac, 6), p, cobro, f"{o['pct']:.0f}%")
                l["unidades"] *= 1 - frac
                if l.get("importe_usd"):
                    l["importe_usd"] *= 1 - frac
            cart["posiciones"] = [l for l in cart["posiciones"] if l["unidades"] > 1e-9]
        elif o["tipo"] == "ajustar":
            for l in lotes:
                if l.get("apalancamiento", 1) == 1:
                    l.update(tp=o["tp"], sl=o["sl"], tpsl_desde=plan["fecha_ejecucion"])
        else:
            p = vela[1]
            uds = o["importe_usd"] * o["apalancamiento"] / p
            cart["efectivo_usd"] -= o["importe_usd"]
            cart["posiciones"].append({
                "simbolo": o["simbolo"], "id": o["id"], "nombre": o["nombre"], "unidades": round(uds, 6),
                "apalancamiento": o["apalancamiento"], "importe_usd": o["importe_usd"], "precio_entrada": p,
                "fecha_entrada": vela[0], "tp": o["tp"], "sl": o["sl"], "tpsl_desde": vela[0],
                "cargos_hasta": vela[0], "nocturna": o.get("nocturna", 0)})
            _mov(cart, vela[0], "compra", o["simbolo"], round(uds, 6), p, o["importe_usd"], f"x{o['apalancamiento']}")
    plan["estado"] = "aplicado (supuesto)"
    log(f"cartera: plan de {mes} dado por ejecutado")
    return True


def revisar_tp_sl(cart, ohlc, hoy):
    """Cierra los lotes cuyo SL/TP saltó (SL primero si ambos el mismo día; con hueco, a la apertura)."""
    cerrados = []
    for l in list(cart["posiciones"]):
        v = [x for x in ohlc.get(l.get("id"), []) if x[0] > l.get("tpsl_desde", "9999")]
        for f, ap, mn, mx, _ in v:
            if l.get("sl") and mn <= l["sl"]:
                motivo, p = "stop loss", min(ap, l["sl"])
            elif l.get("tp") and mx >= l["tp"]:
                motivo, p = "take profit", max(ap, l["tp"])
            else:
                continue
            cobro = valor(l, p)
            cart["efectivo_usd"] += cobro
            _mov(cart, f, motivo, l["simbolo"], l["unidades"], p, cobro)
            cart["posiciones"].remove(l)
            cerrados.append({"simbolo": l["simbolo"], "fecha": f, "motivo": motivo, "precio": p,
                             "importe_usd": round(cobro, 2)})
            break
    # comisiones nocturnas de los lotes apalancados (aprox.: por noche natural)
    for l in cart["posiciones"]:
        if l.get("apalancamiento", 1) > 1 and l.get("cargos_hasta"):
            noches = (date.fromisoformat(hoy) - date.fromisoformat(l["cargos_hasta"])).days
            coste = noches * l.get("nocturna", 0) * l["unidades"]
            if coste > 0:
                l["importe_usd"] -= coste
                _mov(cart, hoy, "comisión nocturna", l["simbolo"], importe=-coste, nota=f"{noches} noches")
            l["cargos_hasta"] = hoy
    cart["revisado_hasta"] = hoy
    return cerrados


def actualizar(cart, mes, cat, hoy, log=print, perfil=None):
    """Carga la cartera real de eToro; sin API, simula planes pendientes y TP/SL con velas. Devuelve los cierres."""
    perfil = perfil or perfiles.PERFILES["titular"]
    try:
        cp = etoro.cuenta(perfil.claves)
    except (OSError, ValueError, KeyError) as e:
        log(f"cartera[{perfil.nombre}]: sin acceso a la API de eToro ({e!r}); se simula con velas")
        cart.update(origen_cartera="simulada", error_api=repr(e)[:200])
    else:
        return sincronizar(cart, cp, cat, hoy, mes, log=log)
    ids = {l["id"] for l in cart["posiciones"] if l.get("id")}
    ids |= {o["id"] for p in cart["planes"].values() if p["estado"] == "pendiente" for o in p["ordenes"] if o.get("id")}
    ohlc = etoro.velas_varios(sorted(ids), n=80, fn=etoro.velas_ohlc) if ids else {}
    for m in sorted(m for m, p in cart["planes"].items() if p["estado"] == "pendiente" and m < mes):
        if not aplicar_plan(cart, m, ohlc, log=log):
            break
    cerrados = revisar_tp_sl(cart, ohlc, hoy)
    for c in cerrados:
        log(f"cartera: {c['simbolo']} cerrada por {c['motivo']} el {c['fecha']} a {c['precio']}")
    return cerrados


# ---------------------------------------------------------------- valoración y plan

def resumen(cart, cat, crib_metr=None):
    """Posiciones agrupadas por símbolo con precio actual, valor y P/L (si se conoce la entrada)."""
    idx = {v["id"]: v for v in cat.values()}
    grupos = {}
    for l in cart["posiciones"]:
        ins = idx.get(l.get("id"), {})
        p = ins.get("precio")
        g = grupos.setdefault(l["simbolo"], {"simbolo": l["simbolo"], "id": l.get("id"), "nombre": ins.get("nombre", l.get("nombre", "")),
                                             "sector": ins.get("sector", ""), "precio": p, "unidades": 0.0,
                                             "valor_usd": 0.0, "coste_usd": 0.0, "coste_conocido": True, "lotes": 0,
                                             "apalancado": False, "tp": l.get("tp"), "sl": l.get("sl")})
        g["lotes"] += 1
        g["unidades"] += l["unidades"]
        g["apalancado"] |= l.get("apalancamiento", 1) > 1
        val = valor(l, p)
        if val is None:
            g["valor_usd"] = None
        elif g["valor_usd"] is not None:
            g["valor_usd"] += val
        if l.get("precio_entrada") is None:
            g["coste_conocido"] = False
        else:
            g["coste_usd"] += l.get("importe_usd") or l["unidades"] * l["precio_entrada"]
    total = sum(g["valor_usd"] or 0 for g in grupos.values())
    for g in grupos.values():
        g["peso_pct"] = round((g["valor_usd"] or 0) / total * 100, 1) if total else None
        g["pl_pct"] = (round(((g["valor_usd"] or 0) / g["coste_usd"] - 1) * 100, 1)
                       if g["coste_conocido"] and g["coste_usd"] else None)
        m = (crib_metr or {}).get(g["simbolo"].upper(), {})
        g.update({k: m.get(k) for k in ("r1m", "r3m", "r12m", "rsi14", "volatilidad", "cerca_max")})
        g["valor_usd"] = None if g["valor_usd"] is None else round(g["valor_usd"], 2)
    return sorted(grupos.values(), key=lambda g: -(g["valor_usd"] or 0)), round(total, 2)


def _ajustar_stop(sl_pct, volatilidad):
    """Stop mínimo según la volatilidad mensual (para no saltar por ruido en un mes sin vigilancia), máx. 35%."""
    sigma_mes = (volatilidad or 0) / 12 ** .5
    return round(min(max(abs(sl_pct), 0.8 * sigma_mes, 4), 35), 1)


def _apalancamiento(pedido, conv, vol, sl_pct, cond, permitidos):
    ok = [x for x in permitidos if x in cond["apalancamientos"]]
    lev = max([x for x in ok if x <= pedido] or [1])
    vol = vol if vol is not None else 99
    while lev > 1:
        if (lev == 5 and not (conv >= 5 and vol <= 30)) or sl_pct * lev > min(40, cond["sl_max_apalancada"]):
            lev = 2 if 2 in ok and lev == 5 else 1
        elif lev == 2 and not (conv >= 4 and vol <= 55):
            lev = 1
        else:
            break
    return lev


def calcular_plan(cart, mes, hoy, inv, cat, crib_metr, log=print):
    """Órdenes del día 1: ventas/ajustes de la cartera y compras con importe, apalancamiento, TP y SL."""
    cfg = cart["config"]
    fx = eurusd(cat)
    a, m = map(int, mes.split("-"))
    ejec = max(date(a, m, 1).isoformat(), hoy)
    # aportación: "fija" (el titular: aportación + efectivo). "efectivo_real" (Perfil2, Perfil3): la aportación ya es efectivo
    # en la cuenta, así que con la API sincronizada el presupuesto es solo el efectivo (no se cuenta dos veces); si la
    # cartera es simulada se usa la aportación por defecto y el informe avisa de que los importes se escalan.
    real = cfg.get("aportacion_modo") == "efectivo_real" and cart.get("origen_cartera") == "eToro"
    aport = 0.0 if real else round(cfg["aportacion_eur"] * fx * (1 - cfg["comision_cambio_pct"] / 100), 2)
    idx = {v["id"]: v for v in cat.values()}
    pos, _ = resumen(cart, cat, crib_metr)
    por_sim = {g["simbolo"].upper(): g for g in pos}
    ordenes, ventas = [], 0.0
    spread, com_v, com_c = cfg["spread_pct"] / 100, cfg["comision_venta_usd"], cfg["comision_compra_usd"]

    for d in inv.get("cartera", []):
        g = por_sim.get(d["simbolo"].upper())
        if not g or not g["id"] or g["precio"] is None:
            continue
        p = g["precio"]
        if d["accion"] in ("vender", "reducir"):
            pct = 100 if d["accion"] == "vender" else min(max(d.get("pct_vender") or 50, 10), 90)
            # lo que entra de verdad: valor menos spread y comisión de cierre de cada lote
            ventas += max((g["valor_usd"] or 0) * pct / 100 * (1 - spread) - com_v * g["lotes"], 0)
            ordenes.append({"tipo": "vender", "simbolo": g["simbolo"], "id": g["id"], "nombre": g["nombre"],
                            "pct": pct, "importe_usd": round((g["valor_usd"] or 0) * pct / 100, 2),
                            "precio_ref": p, "motivo": d["motivo"]})
        if d["accion"] != "vender" and not g["apalancado"]:
            sl = _ajustar_stop(d["sl_pct"], g.get("volatilidad"))
            tp = max(abs(d["tp_pct"]), sl)
            ordenes.append({"tipo": "ajustar", "simbolo": g["simbolo"], "id": g["id"], "nombre": g["nombre"],
                            "precio_ref": p, "tp_pct": tp, "sl_pct": sl, "tp": round(p * (1 + tp / 100), 2),
                            "sl": round(p * (1 - sl / 100), 2), "motivo": d["motivo"],
                            "tp_actual": g["tp"], "sl_actual": g["sl"]})

    bruto = aport + max(cart.get("efectivo_usd", 0), 0) + ventas
    colchon = round(bruto * cfg["colchon_pct"] / 100, 2)
    presupuesto = round(bruto - colchon, 2)      # lo que se puede gastar en compras + sus comisiones

    def neto(n):
        """Importe invertible con n compras: cada una paga comisión de apertura y spread."""
        return max((presupuesto - n * com_c) / (1 + spread), 0)
    vendidos = {o["simbolo"].upper() for o in ordenes if o["tipo"] == "vender" and o["pct"] == 100}
    compras = [pk for pk in inv["picks"] if pk.get("peso_pct", 0) > 0 and pk.get("id")
               and pk["simbolo"].upper() not in vendidos] if presupuesto >= 15 else []
    compras = sorted(compras, key=lambda pk: -pk["peso_pct"])[:cfg["max_compras"]]
    while compras:
        suma = sum(pk["peso_pct"] for pk in compras)
        minimo = max(etoro.condiciones(pk["id"])["minimo_usd"] for pk in compras)
        if len(compras) == 1 or min(pk["peso_pct"] for pk in compras) / suma * neto(len(compras)) >= max(minimo, 15):
            break
        compras.pop()                    # la de menor peso no llega al mínimo de eToro: se reparte entre el resto

    if compras and neto(len(compras)) < max(etoro.condiciones(compras[0]["id"])["minimo_usd"], 10):
        compras = []                      # ni una compra llega al mínimo después de comisiones
    invertible = neto(len(compras))
    apalancado = 0.0
    filas = []
    for pk in compras:
        cond = etoro.condiciones(pk["id"])
        crib = crib_metr.get(pk["simbolo"].upper(), {})
        vol = crib.get("volatilidad")
        sl = _ajustar_stop(pk["stop_pct"], vol)
        permitidos = [1] if cfg.get("ejecucion_automatica") else cfg["apalancamientos"]   # ejecución automática: solo x1
        lev = _apalancamiento(pk.get("apalancamiento", 1), pk["conviccion"], vol, sl, cond, permitidos)
        filas.append([pk, cond, sl, lev])
    suma = sum(pk["peso_pct"] for pk, *_ in filas)
    for f in sorted(filas, key=lambda f: f[0]["conviccion"]):      # límite de margen apalancado: baja los de menor convicción
        imp = f[0]["peso_pct"] / suma * invertible
        if f[3] > 1 and apalancado + imp > invertible * cfg["max_apalancado_pct"] / 100:
            f[3] = 1
        elif f[3] > 1:
            apalancado += imp
    asignado = 0.0
    for i, (pk, cond, sl, lev) in enumerate(filas):
        imp = (int((invertible - asignado) * 100) / 100 if i == len(filas) - 1      # redondeo hacia abajo
               else float(int(pk["peso_pct"] / suma * invertible)))
        asignado += imp
        p = pk["precio_ref"]
        tp = max(abs(pk["objetivo_pct"]), sl)
        uds = imp * lev / p
        if lev != pk.get("apalancamiento", 1):
            log(f"plan: {pk['simbolo']} apalancamiento x{pk.get('apalancamiento', 1)} -> x{lev} (reglas de riesgo)")
        ordenes.append({
            "tipo": "comprar", "simbolo": pk["simbolo"], "id": pk["id"], "nombre": pk["nombre"],
            "refuerzo": pk["simbolo"].upper() in por_sim, "conviccion": pk["conviccion"],
            "importe_usd": imp, "importe_eur": round(imp / fx, 2), "apalancamiento": lev,
            "exposicion_usd": round(imp * lev, 2), "unidades": round(uds, 6), "precio_ref": p,
            "tp_pct": tp, "sl_pct": sl, "tp": round(p * (1 + tp / 100), 2), "sl": round(p * (1 - sl / 100), 2),
            "perdida_sl_usd": round(imp * lev * sl / 100, 2), "nocturna": cond["nocturna_apalancada"] if lev > 1 else 0,
            "coste_mes_usd": round(cond["nocturna_apalancada"] * uds * NOCHES_MES, 2) if lev > 1 else 0})

    plan = {"estado": "pendiente", "generado": datetime.now().astimezone().isoformat(timespec="seconds"),
            "fecha_ejecucion": ejec, "eurusd": fx, "aportacion_usd": aport,
            "modo_aportacion": ("efectivo_real" if real else "estimada_" + str(cfg["aportacion_eur"]) + "_eur")
            if cfg.get("aportacion_modo") == "efectivo_real" else "fija", "efectivo_previo_usd":
            round(cart.get("efectivo_usd", 0), 2), "ventas_usd": round(ventas, 2), "presupuesto_usd": presupuesto,
            "colchon_usd": colchon, "comisiones_usd": round(len(filas) * com_c + sum(o["importe_usd"] for o in ordenes
                                                                                   if o["tipo"] == "comprar") * spread, 2),
            "ejecucion_automatica": bool(cfg.get("ejecucion_automatica")), "ordenes": ordenes}
    cart["planes"][mes] = plan
    return plan


# ---------------------------------------------------------------- sincronizar con eToro

def sincronizar(cart, cp, cat, hoy, mes=None, log=print):
    """Sustituye posiciones, efectivo y órdenes pendientes por los reales de eToro y da por resueltos los planes
    pendientes anteriores a `mes`. Devuelve las posiciones que ya no están (cerradas en eToro)."""
    idx = {v["id"]: v for v in cat.values()}

    def lote(p, entrada):
        ins = idx.get(p["instrumentID"], {})
        tp, sl = p.get("takeProfitRate") or 0, p.get("stopLossRate") or 0
        return {"simbolo": ins.get("simbolo", str(p["instrumentID"])), "id": p["instrumentID"],
                "nombre": ins.get("nombre", ""), "apalancamiento": p["leverage"], "importe_usd": p["amount"],
                # eToro guarda «sin TP» como ~10× la entrada (o 0) y «sin SL» como un precio casi nulo
                "tp": tp if entrada and 0 < tp < 5 * entrada else None,
                "sl": sl if entrada and sl > 0.05 * entrada else None}

    posiciones = []
    for p in cp["positions"]:
        if not p.get("isBuy", True):
            print(f"aviso: posición {p['positionID']} en corto, ignorada")
            continue
        l = lote(p, p["openRate"])
        l.update(unidades=p["units"], precio_entrada=p["openRate"], fecha_entrada=p["openDateTime"][:10],
                 tpsl_desde=hoy, cargos_hasta=hoy, position_id=p["positionID"],
                 fx=(p.get("unrealizedPnL") or {}).get("closeConversionRate") or 1,
                 nocturna=etoro.condiciones(p["instrumentID"])["nocturna_apalancada"] if p["leverage"] > 1 else 0)
        posiciones.append(l)
    vivas = {l["position_id"] for l in posiciones}
    cerrados = [{"simbolo": l["simbolo"], "fecha": hoy, "motivo": "cerrada en eToro", "precio": None,
                 "importe_usd": None} for l in cart["posiciones"] if l.get("position_id") not in (None, *vivas)]
    for c in cerrados:
        _mov(cart, hoy, "cierre (eToro)", c["simbolo"])
    for m, p in cart["planes"].items():
        if p["estado"] == "pendiente" and (mes is None or m < mes):
            p["estado"] = "sustituido por la cuenta real"
    cart["posiciones"] = posiciones
    cart["ordenes_etoro"] = [{**lote(o, (idx.get(o["instrumentID"]) or {}).get("precio")),
                              "fecha": o["openDateTime"][:10], "order_id": o["orderID"]}
                             for o in cp.get("orders", []) if o.get("isBuy", True)]
    _mov(cart, hoy, "sincronización eToro", "", importe=cp["credit"] - cart["efectivo_usd"],
         nota=f"{len(posiciones)} posiciones, {len(cart['ordenes_etoro'])} órdenes pendientes")
    cart["efectivo_usd"] = cp["credit"]    # «disponible»: eToro ya le ha restado lo reservado en órdenes pendientes
    cart["reservado_usd"] = round(sum(o["importe_usd"] for o in cart["ordenes_etoro"]), 2)
    cart["origen_cartera"] = "eToro"
    cart.pop("error_api", None)
    cart["sincronizado"] = datetime.now().astimezone().isoformat(timespec="seconds")
    log(f"cartera: sincronizada con eToro ({len(posiciones)} posiciones, efectivo {cp['credit']:.2f} USD, "
        f"{len(cart['ordenes_etoro'])} órdenes pendientes, {len(cerrados)} cerradas)")
    return cerrados


# ---------------------------------------------------------------- CLI

def _ver(cart, cat):
    pos, total = resumen(cart, cat)
    print(f"Efectivo: {cart['efectivo_usd']:.2f} USD (reservado en órdenes: {cart.get('reservado_usd', 0):.2f}) · "
          f"valor posiciones: {total:.2f} USD")
    for g in pos:
        print(f"  {g['simbolo']:<8} {g['unidades']:>11.5f} uds · {g['precio'] or '¿?':>9} · "
              f"{g['valor_usd'] if g['valor_usd'] is not None else '¿?':>9} USD · lotes {g['lotes']}"
              + (f" · TP {g['tp'] or '—'} SL {g['sl'] or '—'}" if g["tp"] or g["sl"] else "")
              + ("" if g["id"] else "  ⚠ símbolo no encontrado en eToro"))
    for o in cart.get("ordenes_etoro", []):
        print(f"  orden pendiente: comprar {o['simbolo']} por {o['importe_usd']} USD x{o['apalancamiento']} ({o['fecha']})")
    for mes, p in sorted(cart["planes"].items()):
        print(f"Plan {mes}: {p['estado']} ({len(p['ordenes'])} órdenes, ejecución {p['fecha_ejecucion']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--perfil", action="append", help="titular, perfil2, perfil3 (repetible o con comas)")
    pp = argparse.ArgumentParser(add_help=False)       # --perfil también tras la orden
    pp.add_argument("--perfil", action="append", dest="perfil_b", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="orden", required=True)
    sub.add_parser("ver", parents=[pp])
    sub.add_parser("sincronizar", parents=[pp], help="posiciones, efectivo y órdenes reales desde la API de eToro")
    f = sub.add_parser("fijar", parents=[pp], help="sustituye los lotes de un símbolo por uno con estas unidades (0 = eliminar)")
    f.add_argument("simbolo")
    f.add_argument("unidades", type=float)
    f.add_argument("--precio", type=float, help="precio medio de entrada (USD)")
    f.add_argument("--tp", type=float)
    f.add_argument("--sl", type=float)
    e = sub.add_parser("efectivo", parents=[pp], help="efectivo libre en la cuenta (USD)")
    e.add_argument("usd", type=float)
    for o in ("aplicar", "descartar"):
        sub.add_parser(o, parents=[pp]).add_argument("mes")
    a = ap.parse_args()
    a.perfil = (a.perfil or []) + (a.perfil_b or []) or None
    lista = perfiles.elegir(a.perfil) if a.perfil or a.orden in ("ver", "sincronizar") else [perfiles.PERFILES["titular"]]
    if a.orden not in ("ver", "sincronizar") and len(lista) != 1:
        raise SystemExit(f"'{a.orden}' modifica datos: indica un solo --perfil")
    cat = etoro.catalogo()
    hoy = date.today().isoformat()
    rc = 0
    for pf in lista:
        print(f"== {pf.nombre} ==")
        try:
            rc |= _orden(a, pf, cat, hoy)
        except Exception as ex:  # noqa: BLE001 — un perfil que falla no bloquea a los demás
            print(f"error en {pf.nombre}: {ex!r}")
            rc = 1
    return rc


def _orden(a, pf, cat, hoy):
    cart = cargar(pf)
    if a.orden == "sincronizar":
        sincronizar(cart, etoro.cuenta(pf.claves), cat, hoy)
    elif a.orden == "fijar":
        ins = etoro.por_simbolo(cat).get(a.simbolo.upper())
        sim = ins["simbolo"] if ins else a.simbolo.upper()
        cart["posiciones"] = [l for l in cart["posiciones"] if l["simbolo"].upper() != sim.upper()]
        if a.unidades > 0:
            cart["posiciones"].append({
                "simbolo": sim, "id": ins and ins["id"], "nombre": ins["nombre"] if ins else "", "unidades": a.unidades,
                "apalancamiento": 1, "importe_usd": a.precio and round(a.precio * a.unidades, 2),
                "precio_entrada": a.precio, "fecha_entrada": None, "tp": a.tp, "sl": a.sl, "tpsl_desde": hoy})
        _mov(cart, hoy, "ajuste manual", sim, a.unidades, a.precio)
        if not ins:
            print(f"aviso: {a.simbolo} no está en el catálogo de eToro (no se podrá valorar)")
    elif a.orden == "efectivo":
        _mov(cart, hoy, "ajuste manual", "", importe=a.usd - cart["efectivo_usd"], nota="efectivo")
        cart["efectivo_usd"] = a.usd
    elif a.orden == "aplicar":
        p = cart["planes"][a.mes]
        ids = sorted({o["id"] for o in p["ordenes"] if o.get("id")})
        if not aplicar_plan(cart, a.mes, etoro.velas_varios(ids, n=80, fn=etoro.velas_ohlc)):
            return 1
    elif a.orden == "descartar":
        cart["planes"][a.mes]["estado"] = "descartado"
    if a.orden != "ver":
        guardar(cart, pf)
    _ver(cart, cat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
