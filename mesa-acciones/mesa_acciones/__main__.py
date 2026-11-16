"""mesa-acciones — mesa de trading SWING de acciones en eToro (cuenta REAL del titular, x1, USD).

SCOUT (Python, gratis) busca rupturas, retrocesos en tendencia y movimientos con volumen en el universo líquido
(EE. UU. + zona euro) → UN comité Claude (NEWS, SENTIMIENTO, CHARTS, ESCÉPTICO, JEFE) revisa toda la cartera y decide
ventas, planes (stop/objetivo = SL/TP en eToro) y 0..N compras → reglas de riesgo en Python → broker eToro → avisos.
Reuniones: revisión diaria (timer), alertas del VIGÍA y posiciones sin plan; solo con la bolsa abierta.

Uso: python -m mesa_acciones {ciclo|revision|estado|scout|universo|sincronizar|informe|evaluar [días]|
                              comite SIM…|alerta SIM… --motivo T|vigia …}
"""
import fcntl
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import broker as brk
from . import comite, cuota, etoro, horario, mercado, universo, vigia
from .comun import DATA, ESTADO, LOG, ahora, app, cfg, de_iso, guardar, iso, leer, log, wa

EMOJI = {"NEWS": "📰", "SENTIMIENTO": "🌡️", "CHARTS": "📈", "ESCEPTICO": "🤨", "JEFE": "👔"}
INFORMES = Path.home() / "work/inversiones/informes"
FRESCURA = {"precio_max_edad_s": 300, "precio_max_seg": 30, "desvio_max_pct": 1.0}


def _estado():
    return leer(ESTADO, {"comites": {}, "enfriamiento": {}})


def _regiones_operables(c, t=None):
    h = c["horario"]
    return {r for r in ("EEUU", "UE") if horario.abierta_region(r, t, h["margen_apertura_min"], h["margen_cierre_min"])}


def _operable(c, p, t=None):
    h = c["horario"]
    return bool(p.get("bolsa_id")) and horario.abierto(p["bolsa_id"], t, h["margen_apertura_min"], h["margen_cierre_min"])


# ---------------------------------------------------------------- Scout

def _senal(m, s):
    """(tipo, puntuación) o (None, 0). Swing: ruptura de máximos de 20 días con tendencia, retroceso a soporte en
    tendencia alcista, o movimiento fuerte del día con volumen."""
    vol = max(m.get("vol_ratio_ultimo_dia") or 0, m.get("vol_hoy_ratio") or 0)
    p = m["precio"]
    if m["ruptura_20d"] and p > m["ema50"] and m["rsi14"] <= s["rsi_max"] and vol >= s["vol_ratio_min"]:
        return "ruptura", 3 + vol
    if (m.get("sma200") and p > m["sma200"] and m["ema20"] > m["ema50"] and m["rsi14"] <= s["retroceso_rsi_max"]
            and (m["dist_max20_pct"] or 0) <= -s["retroceso_pct"]):
        return "retroceso", 2 + (s["retroceso_rsi_max"] - m["rsi14"]) / 10
    if abs(m["mov_1d_pct"] or 0) >= s["mov_1d_min_pct"] and vol >= s["vol_ratio_min"]:
        return "movimiento", abs(m["mov_1d_pct"]) / 2 + vol
    return None, 0


def _costes_info(m, c, importe=100):
    r = c["riesgo"]
    fijo = (r["comision_compra_usd"] + r["comision_venta_usd"]) / importe * 100
    conv = 2 * r["conversion_pct"] if m.get("moneda") != "USD" else 0
    return round(fijo + conv + (m.get("diferencial_pct") or 0), 2)


def scout(c, regiones, excluir=(), api=None):
    """(candidatos ordenados, métricas de todo lo analizado) del universo de las `regiones` abiertas."""
    s = c["scout"]
    insts = [i for i in universo.cargar() if i.get("region") in regiones]
    if not insts:
        return [], []
    px = etoro.precios([i["id"] for i in insts], f=api or etoro.api)
    vivos = []
    for i in insts:
        q = px.get(i["id"])
        if not q:
            continue
        dif = (q["ask"] - q["bid"]) / q["ask"] * 100
        if dif <= s["diferencial_max_pct"]:
            vivos.append(i)
    todos = list(mercado.analizar_varios(vivos, px).values())
    for m in todos:
        m["senal"], m["puntuacion"] = _senal(m, s)
        m["costes_ida_vuelta_pct_100usd"] = _costes_info(m, c)
    cand = sorted((m for m in todos if m["senal"] and m["simbolo"] not in excluir), key=lambda m: -m["puntuacion"])
    log(f"scout: {len(insts)} en universo abierto · {len(vivos)} con diferencial ≤ {s['diferencial_max_pct']} % · "
        f"{len(todos)} analizadas · {len(cand)} con señal")
    return cand[:s["max_candidatos"]], todos


def _panorama(todos, fuera, n=20):
    return [f"{m['simbolo']} ({m.get('sector') or '?'}) 1d {m['mov_1d_pct']:+}% 5d {m['mov_5d_pct']:+}% "
            f"20d {m['mov_20d_pct']:+}% RSI {m['rsi14']}"
            for m in sorted(todos, key=lambda m: -abs(m["mov_1d_pct"] or 0)) if m["simbolo"] not in fuera][:n]


def _amplitud(todos):
    if not todos:
        return None
    sube = sum(1 for m in todos if (m["mov_1d_pct"] or 0) > 0)
    sobre50 = sum(1 for m in todos if m["precio"] > m["ema50"])
    return {"analizadas": len(todos), "suben_hoy_pct": round(sube / len(todos) * 100),
            "sobre_ema50_pct": round(sobre50 / len(todos) * 100)}


# ---------------------------------------------------------------- reglas de riesgo

def validar(d, m, b, c, pendientes=1):
    """Reglas de una compra del comité con el precio fresco. Devuelve (importe USD, None, nota) o (None, motivo, None).
    Stop/objetivo desde el ask; R:R neto con los costes reales (fijo de compra y venta, conversión y diferencial)."""
    r = c["riesgo"]
    px = m.get("ask") or m["precio"]
    if not d.get("stop") or not d.get("objetivo"):
        return None, "sin stop u objetivo", None
    st = (px - d["stop"]) / px * 100
    ob = (d["objetivo"] - px) / px * 100
    if not r["stop_min_pct"] <= st <= r["stop_max_pct"]:
        return None, f"stop a {st:.1f} % (permitido {r['stop_min_pct']}-{r['stop_max_pct']} %)", None
    if ob < r["objetivo_min_pct"]:
        return None, f"objetivo a {ob:.1f} % (< {r['objetivo_min_pct']} %)", None
    minimo = max(r["importe_min_usd"], m.get("minimo_usd") or 10)
    libre = math.floor(b.efectivo() * 100) / 100
    if libre < minimo + r["comision_compra_usd"]:
        return None, f"efectivo disponible {libre:.2f} $ por debajo del mínimo ({minimo:g} $ + comisión)", None
    pide = d.get("importe_usd")
    if isinstance(pide, (int, float)) and pide > 0:
        importe, nota = float(pide), None
    else:
        importe = libre / max(1, pendientes)
        nota = f"sin importe propuesto: {importe:.2f} $ (efectivo / {max(1, pendientes)})"
    tope = libre - r["comision_compra_usd"]
    if r.get("max_pct_cartera") and importe > b.patrimonio() * r["max_pct_cartera"] / 100:
        importe = b.patrimonio() * r["max_pct_cartera"] / 100
        nota = f"limitado al {r['max_pct_cartera']} % de la cartera"
    if importe > tope:
        nota = f"pedía {importe:.2f} $; limitado al efectivo disponible {tope:.2f} $ (tras la comisión)"
        importe = tope
    elif importe < minimo:
        nota = f"pedía {importe:.2f} $; subido al mínimo {minimo:g} $"
        importe = minimo
    importe = math.floor(importe * 100) / 100
    costes = b.costes_pct(importe, m.get("moneda", "USD")) + (m.get("diferencial_pct") or 0)
    rr = (ob - costes) / (st + costes)
    if rr < r["rr_min"]:
        return None, f"R:R neto {rr:.2f} (< {r['rr_min']}) con costes del {costes:.2f} % sobre {importe:.2f} $", None
    return importe, None, nota


def frescura(m, c, b):
    """Gate de cada COMPRA: bolsa abierta, cotización reciente y ask fresco que no se haya movido más de desvio_max_pct
    respecto al que vio el comité. Devuelve (candidato con precio fresco, None) o (None, motivo)."""
    f = {**FRESCURA, **(c.get("frescura") or {})}
    if not _operable(c, m):
        return None, "su bolsa no está en sesión ahora"
    t0 = time.time()
    try:
        q = etoro.precios([m["id"]], f=b.f).get(int(m["id"]))
    except (etoro.ErrorAPI, OSError, ValueError, KeyError) as e:
        return None, f"datos viejos: no pude re-consultar el precio ({str(e)[:80]})"
    if not q:
        return None, "datos viejos: eToro no devolvió cotización"
    if time.time() - t0 > f["precio_max_seg"]:
        return None, f"datos viejos: precio re-consultado en {time.time() - t0:.0f} s"
    edad = mercado.edad_precio_s(q)
    if edad is not None and edad > f["precio_max_edad_s"]:
        return None, f"datos viejos: última cotización hace {edad / 60:.0f} min"
    vio = m.get("ask") or m["precio"]
    mov = (q["ask"] / vio - 1) * 100
    if abs(mov) > f["desvio_max_pct"]:
        return None, f"precio movido {mov:+.2f} % desde que lo vio el comité (> {f['desvio_max_pct']} %)"
    dif = (q["ask"] - q["bid"]) / q["ask"] * 100
    if dif > c["scout"]["diferencial_max_pct"] * 2:
        return None, f"diferencial {dif:.2f} % demasiado ancho ahora"
    return {**m, "bid": q["bid"], "ask": q["ask"], "precio": q["bid"], "diferencial_pct": round(dif, 3)}, None


def _plan(d, p, info, px, r):
    """Ajusta el plan del comité a las reglas. El stop nunca baja."""
    notas = []
    lo, hi = px * (1 - r["stop_max_pct"] / 100), px * (1 - r["stop_min_pct"] / 100)
    st = d.get("stop")
    if not st:
        st = p.get("stop") or px * (1 - r["stop_defecto_pct"] / 100)
        notas.append("sin stop propuesto")
    if not lo <= st <= hi:
        st2 = min(max(st, lo), hi)
        notas.append(f"stop ajustado {st:.6g}→{st2:.6g} (debe quedar {r['stop_min_pct']}-{r['stop_max_pct']} % bajo el precio)")
        st = st2
    if p.get("stop") and st < p["stop"] and p["stop"] < px:
        notas.append(f"el stop no se baja: sigue en {p['stop']:.6g}")
        st = p["stop"]
    piso = px * (1 + r["objetivo_min_pct"] / 200)        # posiciones adoptadas en pérdidas: basta con superar el precio
    if not p.get("adoptada"):
        piso = max(piso, info["precio_equilibrio"])
    ob = d.get("objetivo")
    if not ob or ob <= piso:
        ob2 = p["objetivo"] if p.get("objetivo") and p["objetivo"] > piso else piso * 1.03
        notas.append(f"objetivo {ob} no supera precio/equilibrio {piso:.6g}: uso {ob2:.6g}")
        ob = ob2
    st, ob = brk._r(st), brk._r(ob)
    if p.get("stop") and st < p["stop"] < px:
        st = p["stop"]
    if st >= px:                                          # el stop de una larga siempre por debajo del bid
        st = brk._r(px * (1 - r["stop_min_pct"] / 100))
        notas.append("stop por encima del precio: bajado al mínimo permitido")
    return st, ob, notas


# ---------------------------------------------------------------- memoria del comité

def _historial(c, simbolos, dias=5):
    n, lim, out = c["comite"].get("historial_por_simbolo", 3), iso(ahora() - timedelta(days=dias)).replace(":", ""), {}
    simbolos = set(simbolos)
    for f in sorted((DATA / "comites").glob("*.json"), reverse=True):
        if f.stem < lim:
            break
        try:
            acta = json.loads(f.read_text())
            hace = round((ahora() - de_iso(f.stem[:13] + ":" + f.stem[13:15] + ":" + f.stem[15:17])).total_seconds() / 3600, 1)
        except (OSError, ValueError):
            continue
        dat = acta.get("datos") or {}
        precios = {m.get("simbolo"): m.get("precio") for m in dat.get("candidatos") or []}
        for p in dat.get("posiciones_abiertas") or []:
            precios.setdefault(p.get("simbolo"), p.get("precio_venta"))
        ds = comite.decisiones(acta.get("salida"))
        por_sim = {}
        for d in ds:
            por_sim.setdefault(str(d.get("simbolo", "")).upper(), d)
        pasar = next((d for d in ds if d.get("accion") == "pasar"), None)
        for sim in precios:
            d = por_sim.get(sim) or (pasar and {**pasar, "accion": "pasar (implícito)"})
            if sim not in simbolos or not d or len(out.setdefault(sim, [])) >= n:
                continue
            out[sim].append({"hace_h": hace, "accion": d.get("accion"), "confianza": d.get("confianza"),
                             "precio_entonces": precios[sim], "stop": d.get("stop"), "objetivo": d.get("objetivo"),
                             "motivo": str(d.get("motivo", ""))[:220]})
    return {k: v for k, v in out.items() if v}


def _recientes(b, n=6):
    return [{k: p.get(k) for k in ("simbolo", "abierta", "cerrada", "entrada", "stop", "objetivo", "salida", "pnl",
                                     "pnl_pct", "motivo")} for p in b.s.get("cerradas", [])[-n:]][::-1]


def informe_semanal(c, max_dias=8):
    """Resumen del último informe semanal de inversiones del titular (una opinión más para el comité)."""
    if not c["comite"].get("informe_semanal", True):
        return None
    fs = sorted(INFORMES.glob("*.json"))
    if not fs:
        return None
    try:
        d = json.loads(fs[-1].read_text())
        gen = datetime.fromisoformat(d["generado"])
    except (OSError, ValueError, KeyError):
        return None
    if (datetime.now(timezone.utc) - gen).days > max_dias:
        return None
    plan = d.get("plan") or {}
    return {"generado": d["generado"], "modelo": d.get("modelo"),
            "contexto_mercado": str((d.get("detalle") or {}).get("resumen_mercado", ""))[:1500],
            "ordenes_propuestas": [{k: o.get(k) for k in ("tipo", "simbolo", "pct", "importe_usd", "tp", "sl")}
                                   | {"motivo": str(o.get("motivo", ""))[:300]} for o in plan.get("ordenes", [])][:20],
            "picks": [{k: x.get(k) for k in ("simbolo", "conviccion", "probabilidad_subida_pct", "objetivo_pct", "stop_pct")}
                      for x in d.get("picks", [])][:12],
            "nota": "Plan del informe semanal (análisis de más largo plazo). El ejecutor automático está pausado: "
                    "solo la mesa opera. Usadlo como opinión, no como orden."}


# ---------------------------------------------------------------- avisos

def avisar(c, texto, asunto, operacion=False):
    """Con avisos.solo_operaciones: app y WhatsApp SOLO si hay compra/venta; el resto queda en log y actas."""
    if not operacion and c["avisos"].get("solo_operaciones", True):
        return
    if c["avisos"].get("app", True):
        app(asunto, texto)
    if operacion and c["avisos"].get("whatsapp"):
        wa(texto)


def _chat(sal):
    return "\n".join(f"{EMOJI.get(x.get('agente'), '•')} {x.get('agente')}: {x.get('mensaje')}"
                     for x in (sal.get("chat") or []) if isinstance(x, dict))


def _bloqueo(c, est):
    hoy = ahora().strftime("%Y-%m-%d")
    if est["comites"].get(hoy, 0) >= c["comite"]["max_por_dia"]:
        return "cupo diario de comités agotado"
    q = cuota.uso()
    if q and (q["h5"] >= c["comite"]["cuota_5h_max"] or q["semana"] >= c["comite"]["cuota_semanal_max"]):
        return f"cuota Claude alta ({q})"
    return None


def _apuntar(est):
    hoy = ahora().strftime("%Y-%m-%d")
    est["comites"][hoy] = est["comites"].get(hoy, 0) + 1
    est["comites"] = {k: v for k, v in est["comites"].items() if k >= hoy[:8]}
    est["ultimo_comite"] = iso()
    guardar(ESTADO, est)


def _bloqueo_compras(c, b, est):
    dd, cap = c["riesgo"].get("drawdown_max_pct"), b.capital
    if dd and b.patrimonio() < cap * (1 - dd / 100):
        if est.get("aviso_dd") != ahora().strftime("%Y-%m-%d"):
            est["aviso_dd"] = ahora().strftime("%Y-%m-%d")
            guardar(ESTADO, est)
        log("freno por pérdidas máximas: no se abren operaciones")
        return f"freno: la cartera vale menos del {100 - dd:g} % de {cap:.2f} $ al arrancar"
    if b.efectivo() < c["riesgo"]["importe_min_usd"] + c["riesgo"]["comision_compra_usd"]:
        return f"sin efectivo suficiente ({b.efectivo():.2f} $ disponibles)"
    return None


def _dias(p):
    return round((ahora() - de_iso(p["abierta"])).total_seconds() / 86400, 1)


def _accion_txt(d):
    return f"{d['accion'].upper()} {d.get('simbolo', '')} ({d.get('confianza', '?')}/10)"


# ---------------------------------------------------------------- reunión

def reunion(c, b, est, posiciones, cand, bloqueo_compras=None, panorama=None, convocatoria=None, amplitud=None):
    """Comité ÚNICO: revisa todas las posiciones y estudia comprar entre `cand`. Ejecuta ventas (como mucho
    comite.max_ventas_por_reunion), planes y compras por confianza. Solo actúa sobre bolsas en sesión."""
    r = c["riesgo"]
    infos, univ = {}, universo.por_simbolo()
    for p in posiciones:
        try:
            info = b.info_posicion(p)
        except (etoro.ErrorAPI, OSError) as e:
            log(f"comité: sin datos de {p['simbolo']} ({e})")
            info = {"simbolo": p["simbolo"], "entrada": p["entrada"], "importe_usd": p["importe"],
                    "precio_venta": None, "precio_equilibrio": b.equilibrio(p)}
        info.update(stop_actual=p.get("stop"), objetivo_actual=p.get("objetivo"), abierta=p["abierta"],
                    dias_abierta=_dias(p), mercado_abierto=_operable(c, p))
        try:
            inst = univ.get(p["simbolo"]) or {"id": p["instrumento"], "simbolo": p["simbolo"],
                                               "region": p.get("region"), "moneda": p.get("moneda")}
            info["mercado"] = mercado.analizar(inst)
        except (OSError, ValueError, IndexError, KeyError) as e:
            log(f"comité: sin datos de mercado de {p['simbolo']} ({e})")
        infos[p["simbolo"]] = info
    for m in cand:
        est["enfriamiento"][m["simbolo"]] = iso()
    _apuntar(est)
    datos = {"fecha_utc": iso(), "horario": horario.estado_txt(), "cuenta": "eToro real (USD)",
             "posiciones_abiertas": list(infos.values()), "candidatos": cand,
             "efectivo_disponible_usd": round(b.efectivo(), 2), "valor_cartera_usd": round(b.patrimonio(), 2),
             "importe_min_usd": r["importe_min_usd"],
             "reglas": {k: r[k] for k in ("stop_min_pct", "stop_max_pct", "objetivo_min_pct", "rr_min",
                                           "comision_compra_usd", "comision_venta_usd", "conversion_pct")}}
    for k, v in (("compras_bloqueadas", bloqueo_compras), ("panorama_universo", panorama),
                 ("convocatoria", convocatoria), ("amplitud", amplitud), ("informe_semanal", informe_semanal(c)),
                 ("historial_comite", _historial(c, [m["simbolo"] for m in cand] + list(infos))),
                 ("operaciones_recientes", _recientes(b))):
        if v:
            datos[k] = v
    cab = (f"🤖 Mesa acciones [{b.nombre}] · efectivo {datos['efectivo_disponible_usd']:.2f} $ · cartera "
           f"{datos['valor_cartera_usd']:.2f} $"
           + (f"\n🔍 SCOUT: {', '.join(m['simbolo'] + ' (' + str(m.get('senal')) + ')' for m in cand)}" if cand else "")
           + (f"\n🚨 VIGÍA: {convocatoria}" if convocatoria else "")
           + (f"\n🛑 Compras bloqueadas: {bloqueo_compras}" if bloqueo_compras else "") + "\n")
    try:
        sal, consumo = comite.reunir(datos, c)
    except Exception as e:  # noqa: BLE001 — un fallo del comité no tumba el ciclo
        log(f"comité falló: {e}")
        return avisar(c, f"{cab}⚠️ El comité falló ({str(e)[:200]}); se mantienen los planes.", "Mesa acciones: fallo del comité")
    ds = comite.decisiones(sal)
    for d in ds:
        d["simbolo"] = str(d.get("simbolo", "")).upper()
    log(f"comité: {len(ds)} decisión(es): {'; '.join(_accion_txt(d) for d in ds) or 'ninguna'} · consumo {consumo}")
    for d in ds:
        log(f"comité decide: {d}")
    res, operacion, asunto = [], False, []
    pos = {p["simbolo"]: p for p in posiciones}
    dec_pos = {}
    for d in ds:
        if d["simbolo"] in pos and d["accion"] in ("mantener", "vender"):
            dec_pos.setdefault(d["simbolo"], d)

    # 1) ventas (solo con su bolsa abierta; como mucho max_ventas_por_reunion, las de más confianza primero)
    vendidas, vmax = set(), c["comite"]["max_ventas_por_reunion"]
    ventas = sorted((s for s, d in dec_pos.items() if d["accion"] == "vender"), key=lambda s: -(dec_pos[s].get("confianza") or 0))
    for k, s in enumerate(ventas):
        d = dec_pos[s]
        if not _operable(c, pos[s]):
            res.append(f"⏸️ {s}: el comité quiere vender pero su bolsa está cerrada; se mantiene el plan actual")
            continue
        if k >= vmax:
            log(f"venta {s} aplazada: ya hay {vmax} ventas en esta reunión (comite.max_ventas_por_reunion)")
            res.append(f"⏸️ {s}: venta aplazada (máximo {vmax} ventas por reunión); sigue el plan actual")
            pos[s].update(venta_aplazada=iso(), plan_comite=bool(pos[s].get("stop") and pos[s].get("objetivo")))
            b._guardar()
            continue
        try:
            x = b.cerrar(pos[s], f"comité: {str(d.get('motivo', ''))[:120]}")
            vendidas.add(s)
            operacion = True
            log(f"cierre {s} comité: {x.get('pnl')} $")
            res.append(f"💸 VENDIDA {s} a {x.get('salida')}: {x.get('pnl')} $ ({x.get('pnl_pct')} %) · "
                       f"{str(d.get('motivo', ''))[:150]}")
            asunto.append(f"VENDE {s}")
        except (brk.ErrorBroker, etoro.ErrorAPI, OSError) as e:
            log(f"venta {s} falló: {e}")
            res.append(f"⚠️ El comité quiere VENDER {s} pero falló: {str(e)[:200]}")
            asunto.append(f"fallo al vender {s}")

    # 2) planes de las que siguen
    for s, p in pos.items():
        d = dec_pos.get(s)
        if s in vendidas or (d and d["accion"] == "vender") or p not in b.abiertas():
            continue
        # El SL/TP se fija también con la bolsa cerrada: eToro acepta la modificación fuera de sesión
        # (comprobado con un PATCH en MSFT a las 23:00) y así ninguna posición se queda sin red hasta la apertura.
        if d is None:
            if p.get("plan_comite"):
                res.append(f"ℹ️ {s}: el comité no decidió; sigue stop {p['stop']} · objetivo {p['objetivo']}")
                continue
            d = {"accion": "mantener", "confianza": 0, "motivo": "sin decisión del comité"}
        info = infos[s]
        antes = (p.get("stop"), p.get("objetivo"))
        try:
            px = info.get("precio_venta") or b.precio_venta(p)
            st, ob, notas = _plan(d, p, info, px, r)
            b.fijar_plan(p, st, ob)
        except (brk.ErrorBroker, etoro.ErrorAPI, OSError, TypeError) as e:
            log(f"plan {s}: {e}")
            res.append(f"⚠️ {s}: {str(e)[:180]}")
            continue
        cambio_txt = "sin cambios" if antes == (st, ob) else f"antes stop {antes[0]} · obj {antes[1]}"
        log(f"plan {s}: stop {st} objetivo {ob} ({cambio_txt}){' · ' + '; '.join(notas) if notas else ''}")
        res.append(f"📌 MANTENER {s} ({d.get('confianza', '?')}/10): stop {st} · objetivo {ob} ({cambio_txt})"
                   + (f" · ℹ️ {'; '.join(notas)}" if notas else ""))

    # 3) compras por confianza mientras quede efectivo
    compras = sorted((d for d in ds if d["accion"] == "comprar"), key=lambda d: -(d.get("confianza") or 0))
    cand_sim = {m["simbolo"]: m for m in cand}
    for i, d in enumerate(compras):
        s = d["simbolo"]
        b.cartera(fresca=True)
        if bloqueo_compras:
            error = f"compras bloqueadas ({bloqueo_compras})"
        elif s in {q["simbolo"] for q in b.abiertas()} or s in vendidas:
            error = "símbolo ya abierto o vendido en esta reunión (una posición por símbolo)"
        elif s not in cand_sim:
            error = "no era candidato"
        else:
            fresco, error = frescura(cand_sim[s], c, b)
            if not error:
                importe, error, nota = validar(d, fresco, b, c, pendientes=len(compras) - i)
        if error:
            log(f"compra {s} rechazada: {error}")
            res.append(f"🛑 Compra de {s} RECHAZADA: {error}")
            continue
        inst = {**(univ.get(s) or {}), **{k: fresco[k] for k in ("id", "simbolo", "region", "moneda") if k in fresco}}
        try:
            p = b.abrir(inst, importe, d["stop"], d["objetivo"])
        except (brk.ErrorBroker, etoro.ErrorAPI, OSError) as e:          # NUNCA se reintenta una compra
            duda = isinstance(e, brk.ErrorBroker) and not e.definitivo
            log(f"compra {s} falló: {e}")
            res.append(f"⚠️ Falló la COMPRA de {s} ({importe:.2f} $): {str(e)[:200]}"
                       + (" · el ciclo comprobará si se ejecutó" if duda else " · no se reintenta"))
            asunto.append(f"fallo al comprar {s}")
            continue
        operacion = True
        log(f"apertura {s} {p['importe']} $ a {p['entrada']} ({p['unidades']} u.) stop {p['stop']} objetivo "
            f"{p['objetivo']} · SL/TP en eToro {p.get('sl_etoro')} / {p.get('tp_etoro')}{' · ' + nota if nota else ''}")
        res.append(f"✅ COMPRA {s} {p['importe']:.2f} $ a {p['entrada']} · stop {p['stop']} · objetivo {p['objetivo']} "
                   f"({d.get('confianza', '?')}/10)" + (f" · ℹ️ {nota}" if nota else ""))
        asunto.append(f"COMPRA {s} {p['importe']:.0f} $")
    if not compras and cand:
        p_ = next((d for d in ds if d["accion"] == "pasar"), None)
        res.append(f"➡️ PASAMOS ({p_.get('confianza', '?')}/10): {p_.get('motivo', '')}" if p_ else "➡️ Ninguna compra.")
    if not asunto:
        asunto.append("revisión" if posiciones else "PASAMOS")
    avisar(c, f"{cab}{chr(10).join(res)}\n\n{_chat(sal)}", f"Mesa acciones: {', '.join(asunto)}", operacion=operacion)


# ---------------------------------------------------------------- ciclos

def _sincronizar(c, b, est):
    try:
        cierres = b.revisar()
    except Exception as e:  # noqa: BLE001 — un fallo de la API no tumba el ciclo
        log(f"⚠️ error al revisar la cartera de eToro: {str(e)[:200]}")
        return None
    for p in cierres:
        log(f"cierre {p['simbolo']} {p['motivo']}: {p.get('pnl_pct')} % ({p.get('pnl')} $)")
        if "comité" not in str(p.get("motivo")):
            avisar(c, f"🔔 Mesa acciones CIERRE {p['simbolo']} por {p['motivo']}: {p.get('pnl')} $ ({p.get('pnl_pct')} %) · "
                      f"entrada {p['entrada']} → salida {p.get('salida')}",
                   f"Mesa acciones: CIERRE {p['simbolo']} ({p['motivo']})", operacion=True)
    return cierres


def ciclo(c):
    """Cada 15 min (timer): sincroniza con eToro (cierres por SL/TP, adopciones, compras inciertas, SL de respaldo)
    y convoca al comité si hay posiciones sin plan cuya bolsa está en sesión."""
    b = brk.broker(c)
    est = _estado()
    if _sincronizar(c, b, est) is None:
        return
    urg = [p for p in b.sin_plan() if _operable(c, p)]
    if not urg:
        return
    motivo = _bloqueo(c, est)
    if motivo:
        return log(f"comité (posición sin plan: {', '.join(p['simbolo'] for p in urg)}): {motivo}")
    log(f"comité convocado: posición sin plan ({', '.join(p['simbolo'] for p in urg)})")
    revisables = [p for p in b.abiertas() if p.get("estado") == "abierta"]
    reunion(c, b, est, revisables, [], _bloqueo_compras(c, b, est), convocatoria=None)


def revision(c):
    """Revisión diaria (timer): sincroniza, pasa el Scout por las bolsas en sesión y reúne al comité con TODA la
    cartera, los candidatos y el informe semanal."""
    b = brk.broker(c)
    est = _estado()
    if _sincronizar(c, b, est) is None:
        return
    regiones = _regiones_operables(c)
    if not regiones:
        return log(f"revisión: ninguna bolsa en sesión ({horario.estado_txt()}); no hay reunión")
    motivo = _bloqueo(c, est)
    if motivo:
        return log(f"revisión: {motivo}")
    abiertas = {p["simbolo"] for p in b.abiertas()}
    enfr = {s for s, t in est["enfriamiento"].items()
            if (ahora() - de_iso(t)).total_seconds() < c["comite"]["enfriamiento_horas"] * 3600}
    bloqueo = _bloqueo_compras(c, b, est)
    try:
        cand, todos = scout(c, regiones, excluir=abiertas | enfr, api=b.f)
    except (etoro.ErrorAPI, OSError, ValueError) as e:
        log(f"scout falló: {e}")
        cand, todos = [], []
    if bloqueo:
        cand = []
    revisables = [p for p in b.abiertas() if p.get("estado") == "abierta"]
    if not revisables and not cand:
        return log(f"revisión: nada que revisar ni comprar{f' ({bloqueo})' if bloqueo else ''}")
    log(f"comité convocado: revisión diaria ({len(revisables)} posiciones, {len(cand)} candidatos)")
    reunion(c, b, est, revisables, cand, bloqueo, _panorama(todos, {m["simbolo"] for m in cand} | abiertas),
            amplitud=_amplitud(todos))


def alerta(c, sims, convocatoria=None):
    """Reunión extraordinaria (vigía o a mano) sobre `sims`: candidatos si no están en cartera."""
    b = brk.broker(c)
    est = _estado()
    if _sincronizar(c, b, est) is None:
        return
    motivo = _bloqueo(c, est)
    if motivo:
        return log(f"comité ({convocatoria or 'manual'}): {motivo}; no me reúno")
    u = est.get("ultimo_comite")
    if convocatoria and u and (ahora() - de_iso(u)).total_seconds() < c["comite"]["min_entre_alertas_min"] * 60:
        return log(f"comité (vigía): última reunión hace < {c['comite']['min_entre_alertas_min']} min; no me reúno por {convocatoria}")
    revisables = [p for p in b.abiertas() if p.get("estado") == "abierta"]
    abiertas = {p["simbolo"] for p in b.abiertas()}
    bloqueo = _bloqueo_compras(c, b, est)
    univ = universo.por_simbolo()
    cand = []
    ids = [univ[s]["id"] for s in sims if s in univ and s not in abiertas]
    px = etoro.precios(ids, f=b.f) if ids and not bloqueo else {}
    for s in ([] if bloqueo else sims):
        if s in abiertas:
            continue
        if s not in univ:
            log(f"comité: {s} no está en el universo; no se estudia su compra")
            continue
        if not _operable(c, univ[s]):
            log(f"comité: {s} con su bolsa cerrada; no se estudia")
            continue
        try:
            m = mercado.analizar(univ[s], px.get(univ[s]["id"]))
            m["senal"] = "vigia" if convocatoria else "manual"
            m["costes_ida_vuelta_pct_100usd"] = _costes_info(m, c)
            cand.append(m)
        except (OSError, ValueError, IndexError, KeyError) as e:
            log(f"comité: {s} sin datos ({e})")
    if not cand and not [p for p in revisables if p["simbolo"] in sims or _operable(c, p)]:
        return log(f"comité ({convocatoria or 'manual'}): nada que estudiar con la bolsa abierta")
    reunion(c, b, est, revisables, cand, bloqueo, convocatoria=convocatoria)


# ---------------------------------------------------------------- consulta

def resumen(c, imprimir=True):
    b = brk.broker(c)
    pat, cap = b.patrimonio(), b.capital
    lineas = [f"📊 Mesa acciones [{b.nombre}] {ahora().strftime('%Y-%m-%d %H:%M')}Z · {horario.estado_txt()}",
              f"Cartera: {pat:,.2f} $ ({(pat / cap - 1) * 100:+.2f} % sobre {cap:.2f} $ al arrancar) · efectivo "
              f"disponible {b.efectivo():.2f} $ (credit {b.cartera()['credit']:.2f} $)"]
    for p in b.abiertas():
        if p.get("estado") == "incierta":
            lineas.append(f"• {p['simbolo']} {p['importe']:.2f} $ · compra incierta (pendiente de confirmar)")
            continue
        i = b.info_posicion(p)
        plan = (f"stop {p['stop']} · obj {p['objetivo']}" if p.get("stop") and p.get("objetivo") else "sin stop/objetivo")
        lineas.append(f"• {p['simbolo']:6} {p['importe']:8.2f} $ entrada {p['entrada']} → {i.get('precio_venta')} "
                      f"({i.get('pnl_pct')} %) · {plan}{'' if p.get('plan_comite') else ' (sin plan del comité)'} · "
                      f"SL/TP eToro {p.get('sl_etoro') or '—'} / {p.get('tp_etoro') or '—'}")
    tot = b.s.get("cerradas", [])
    if tot:
        lineas.append(f"Cerradas: {len(tot)} ({sum(1 for p in tot if (p.get('pnl') or 0) > 0)} ganadoras, "
                      f"{sum(p.get('pnl') or 0 for p in tot):+.2f} $)")
    est, q = _estado(), cuota.uso()
    lineas.append(f"Comités hoy: {est['comites'].get(ahora().strftime('%Y-%m-%d'), 0)}/{c['comite']['max_por_dia']}"
                  + (f" · cuota Claude 5h {q['h5']:.0f} %, semana {q['semana']:.0f} %" if q else ""))
    txt = "\n".join(lineas)
    if imprimir:
        print(txt)
    return txt


def informe(c):
    """Resumen diario: a la app SOLO si en 24 h hubo compras o ventas (avisos.solo_operaciones)."""
    t0 = iso(ahora() - timedelta(hours=24))
    ops = [l for l in (LOG.read_text().splitlines() if LOG.exists() else [])
           if l[:20] >= t0 and any(k in l for k in ("apertura ", "cierre ", "falló", "freno"))]
    txt = resumen(c, imprimir=False) + "\n\n== Operaciones e incidencias (24 h) ==\n" + ("\n".join(ops) or "- Ninguna.")
    print(txt)
    if any(k in l for l in ops for k in ("apertura ", "cierre ")) or not c["avisos"].get("solo_operaciones", True):
        app(f"Mesa acciones — informe {ahora().strftime('%d/%m')}", txt)


def evaluar(c, dias=10.0, horizonte=10):
    """Contrafactual de los «pasar»: qué habría pasado comprando cada candidato descartado con un plan tipo
    (stop 2 ATR diarios, objetivo con el R:R mínimo) durante `horizonte` días. Solo velas públicas."""
    r = c["riesgo"]
    lim = iso(ahora() - timedelta(days=dias)).replace(":", "")
    vistos, filas = set(), []
    for f in sorted((DATA / "comites").glob("*.json")):
        if f.stem < lim:
            continue
        acta = leer(f, {})
        compradas = {str(d.get("simbolo", "")).upper() for d in comite.decisiones(acta.get("salida")) if d.get("accion") == "comprar"}
        for m in (acta.get("datos") or {}).get("candidatos") or []:
            s = m.get("simbolo")
            if not s or s in compradas or s in vistos:
                continue
            vistos.add(s)
            ent = m.get("ask") or m["precio"]
            st = min(max(2 * (m.get("atr14_pct") or 2), r["stop_min_pct"]), r["stop_max_pct"])
            ob = max(r["objetivo_min_pct"], st * r["rr_min"] + 2)
            try:
                v = [x for x in etoro.velas(m["id"], "OneDay", 40) if x[0][:10] > f.stem[:10]][:horizonte]
            except (OSError, ValueError, KeyError):
                continue
            res = "abierta"
            for x in v:
                if (x[3] / ent - 1) * 100 <= -st:
                    res = "stop"
                    break
                if (x[2] / ent - 1) * 100 >= ob:
                    res = "objetivo"
                    break
            pnl = -st if res == "stop" else ob if res == "objetivo" else ((v[-1][4] / ent - 1) * 100 if v else 0)
            filas.append((f.stem[:10], s, res, round(pnl - 2, 2)))
    for x in filas:
        print(f"{x[0]} {x[1]:7} {x[2]:9} {x[3]:+6.2f} %")
    if filas:
        print(f"\n{len(filas)} descartes · P/L medio hipotético {sum(x[3] for x in filas) / len(filas):+.2f} % (con ~2 % de costes)")
    else:
        print("evaluar: no hay candidatos descartados en el periodo")


def main():
    args = sys.argv[1:] or ["estado"]
    if args[0] == "vigia":
        return vigia.vigilar() if args[1:2] == ["servicio"] else vigia.cli(args[1:])
    c = cfg()
    if os.environ.get("MESA_LECTURA"):
        c["modo"] = "lectura"
    if args[0] == "evaluar":
        return evaluar(c, float(args[1]) if len(args) > 1 else 10.0)
    if args[0] == "universo":
        u = universo.construir(c)
        return print(f"universo: {len(u)} acciones ({sum(1 for i in u if i['region'] == 'EEUU')} EE. UU., "
                     f"{sum(1 for i in u if i['region'] == 'UE')} zona euro)")
    DATA.mkdir(exist_ok=True)
    with open(DATA / ".lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | (0 if args[0] in ("alerta", "revision", "informe") else fcntl.LOCK_NB))
        except BlockingIOError:
            return log("otro ciclo en marcha; salgo")
        if args[0] == "ciclo":
            ciclo(c)
        elif args[0] == "revision":
            revision(c)
        elif args[0] == "alerta" and "--motivo" in args[2:]:
            i = args.index("--motivo")
            alerta(c, [a.upper() for a in args[1:i]], " ".join(args[i + 1:]) or "alerta del vigía")
        elif args[0] == "comite" and len(args) > 1:
            alerta(c, [a.upper() for a in args[1:]])
        elif args[0] == "scout":
            cand, todos = scout(c, {"EEUU", "UE"})
            for m in sorted(todos, key=lambda m: -(m["puntuacion"] or 0))[:40]:
                print(f"{m['simbolo']:8} {m['region']:4} 1d {m['mov_1d_pct']:+6}% 5d {m['mov_5d_pct']:+6}% RSI {m['rsi14']:5} "
                      f"vol {m.get('vol_ratio_ultimo_dia')}x dif {m.get('diferencial_pct')} %  {m['senal'] or ''}")
        elif args[0] == "sincronizar":
            b = brk.broker(c)
            _sincronizar(c, b, _estado())
            resumen(c)
        elif args[0] == "informe":
            informe(c)
        elif args[0] == "estado":
            resumen(c)
        else:
            print(__doc__)


if __name__ == "__main__":
    main()
