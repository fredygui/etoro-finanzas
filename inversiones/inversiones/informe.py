"""Evaluación de informes anteriores y generación del informe (nota de Obsidian en markdown + HTML para el correo)."""
import calendar
import json
import re
from datetime import date
from pathlib import Path

import markdown

from . import etoro

BASE = Path(__file__).resolve().parent.parent
INFORMES = BASE / "informes"                       # versionado: un JSON por mes
VAULT = Path.home() / "Obsidian/Proyectos"
NOTAS = VAULT / "Inversiones"
ESTADO = Path.home() / ".local/state/inversiones"  # ultimo.json: HTML del correo y metadatos del último informe
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
AVISO = ("Informe generado automáticamente con datos públicos e IA. No es asesoramiento financiero: las "
         "rentabilidades pasadas y los catalizadores no garantizan subidas. Invierte solo lo que puedas perder y usa stops.")


def nombre_mes(mes):
    a, m = map(int, mes.split("-"))
    return f"{MESES[m - 1]} {a}"


def _cierre(v, hasta_iso):
    previos = [c for f, c, _ in v if f <= hasta_iso]
    return previos[-1] if previos else None


def evaluar(mes_actual, cat, n=6):
    """Rentabilidad de las ideas de los últimos n informes: a fin de su mes y hasta hoy, frente al SPY."""
    previos = sorted(p for p in INFORMES.glob("????-??.json") if p.stem < mes_actual)[-n:]
    if not previos:
        return []
    informes = [json.loads(p.read_text()) for p in previos]
    spy = etoro.por_simbolo(cat).get("SPY")
    ids = {pk["id"] for inf in informes for pk in inf.get("picks", []) if pk.get("id")} | ({spy["id"]} if spy else set())
    velas = etoro.velas_varios(sorted(ids))
    out = []
    for inf in informes:
        a, m = map(int, inf["mes"].split("-"))
        fin = date(a, m, calendar.monthrange(a, m)[1]).isoformat()
        ref = inf["generado"][:10]

        def rent(iid, precio_ref=None):
            v = velas.get(iid) or []
            p0 = precio_ref or _cierre(v, ref)
            pf, ph = _cierre(v, fin), (v[-1][1] if v else None)
            pct = lambda p: round((p / p0 - 1) * 100, 1) if p and p0 else None
            return pct(pf), pct(ph)

        filas = []
        for pk in inf.get("picks", []):
            if not pk.get("id"):
                continue
            r_mes, r_hoy = rent(pk["id"], pk.get("precio_ref"))
            filas.append({"simbolo": pk["simbolo"], "conviccion": pk.get("conviccion"),
                          "objetivo_pct": pk.get("objetivo_pct"), "r_mes": r_mes, "r_hoy": r_hoy})
        validas = [f["r_mes"] for f in filas if f["r_mes"] is not None]
        spy_mes = rent(spy["id"])[0] if spy else None
        out.append({
            "mes": inf["mes"], "picks": filas, "spy_mes": spy_mes,
            "media_mes": round(sum(validas) / len(validas), 1) if validas else None,
            "positivas": sum(r > 0 for r in validas), "total": len(validas),
            "en_objetivo": sum(1 for f in filas if f["r_mes"] is not None and f["objetivo_pct"] is not None
                               and f["r_mes"] >= f["objetivo_pct"]),
        })
    return out


def _n(x, suf="%"):
    return "–" if x is None else f"{x:+.1f}{suf}" if suf == "%" else f"{x}{suf}"


def _tabla(cab, filas):
    lineas = ["| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
    lineas += ["| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in f) + " |" for f in filas]
    return "\n".join(lineas)


def _usd(x):
    return "–" if x is None else f"{x:,.2f} $".replace(",", " ")


def _tpsl_actual(o):
    if not (o.get("tp_actual") or o.get("sl_actual")):
        return "**sin TP/SL**"
    return f"TP {o.get('tp_actual') or '–'} · SL {o.get('sl_actual') or '–'}"


def _plan(mes, c):
    """Órdenes del día 1 y estado de la cartera (c = {plan, posiciones, cierres, cart})."""
    plan, (pos, total), cart = c.get("plan"), c["posiciones"], c["cart"]
    p = []
    if plan:
        cfg = cart["config"]
        modo = plan.get("modo_aportacion", "fija")
        if modo == "efectivo_real":
            dinero = (f"efectivo real disponible en tu cuenta de eToro {_usd(plan['efectivo_previo_usd'])}"
                      + (f" + ventas {_usd(plan['ventas_usd'])}" if plan["ventas_usd"] else ""))
        else:
            dinero = (f"aportación {cfg['aportacion_eur']} € ≈ {_usd(plan['aportacion_usd'])} (EUR/USD {plan['eurusd']}, "
                      f"−{cfg['comision_cambio_pct']}% de cambio estimado) + efectivo {_usd(plan['efectivo_previo_usd'])}"
                      + (f" + ventas {_usd(plan['ventas_usd'])}" if plan["ventas_usd"] else ""))
        if plan.get("colchon_usd") is not None:
            dinero += (f", − colchón {_usd(plan['colchon_usd'])} por si el precio se mueve antes de ejecutar (las ventas "
                       "ya descuentan spread y comisión de cierre)")
        auto = plan.get("ejecucion_automatica")
        if auto:
            p += ["## Plan de hoy: lo ejecuto yo en eToro a las 16:00", "",
                  f"**Ejecución automática hoy {plan['fecha_ejecucion']} a las 16:00** (bolsa de EE. UU. abierta): primero "
                  "ventas, luego ajustes de TP/SL y por último compras x1. Nunca se usa más que el efectivo real de la "
                  "cuenta: si al ejecutar hay menos, las compras se reducen en proporción. Después recibirás el informe "
                  "de lo ejecutado.", "",
                  f"Dinero para compras: **{_usd(plan['presupuesto_usd'])}** = {dinero}. Comisiones de compra previstas: "
                  f"≈ {_usd(plan.get('comisiones_usd'))} (1 $ por operación + spread).", ""]
        else:
            p += [f"## Plan del día 1: qué hacer en eToro", "",
                  f"Ejecución: **{plan['fecha_ejecucion']}** (si es festivo o fin de semana, las órdenes se ejecutan en la "
                  f"siguiente apertura). Dinero disponible: **{_usd(plan['presupuesto_usd'])}** = {dinero}.", ""]
        if modo.startswith("estimada_"):
            p += [f"> [!warning] No se ha podido leer tu efectivo real en eToro: **los importes están calculados para una "
                  f"aportación de {cfg['aportacion_eur']} €**. Si este mes aportas otra cantidad (entre "
                  f"{cfg.get('aportacion_min_eur', 100)} y {cfg.get('aportacion_max_eur', 200)} €), escala los importes de "
                  "compra en la misma proporción (los porcentajes de TP/SL no cambian).", ""]
        ventas = [o for o in plan["ordenes"] if o["tipo"] == "vender"]
        compras = [o for o in plan["ordenes"] if o["tipo"] == "comprar"]
        ajustes = [o for o in plan["ordenes"] if o["tipo"] == "ajustar"]
        if ventas:
            p += ["**1. Vender primero**", "", _tabla(["Activo", "Vender", "≈ Importe", "Motivo"], [
                [o["simbolo"], f"{o['pct']:.0f}%", _usd(o["importe_usd"]), o["motivo"]] for o in ventas]), ""]
        p += [f"**{2 if ventas else 1}. Comprar** (orden a mercado, " + ("TP y SL sobre el precio real de compra)"
              if auto else "con TP y SL en la misma orden)"), ""]
        p.append(_tabla(["Activo", "Importe", "Apalanc.", "≈ Unidades", "Precio ref.", "Take profit", "Stop loss",
                         "Pérdida si SL", "Coste nocturno/mes"], [
            [f"{o['simbolo']}" + (" (refuerzo)" if o["refuerzo"] else ""), f"**{_usd(o['importe_usd'])}** "
             f"(≈ {o['importe_eur']:.0f} €)", f"x{o['apalancamiento']}" + (" (CFD)" if o["apalancamiento"] > 1 else ""),
             f"{o['unidades']:.4f}", o["precio_ref"], f"**{o['tp']}** (+{o['tp_pct']:.0f}%)",
             f"**{o['sl']}** (−{o['sl_pct']:.0f}%)", _usd(o["perdida_sl_usd"]),
             _usd(o["coste_mes_usd"]) if o["coste_mes_usd"] else "–"] for o in compras]) if compras
             else "No hay compras este mes: el dinero queda como efectivo.")
        if ajustes:
            p += ["", f"**{3 if ventas else 2}. Poner/editar TP y SL de lo que ya tienes** (en cada posición del "
                  "activo, x1)", "", _tabla(["Activo", "Ahora", "Take profit", "Stop loss", "Motivo"], [
                      [o["simbolo"], _tpsl_actual(o), f"{o['tp']} (+{o['tp_pct']:.0f}%)",
                       f"{o['sl']} (−{o['sl_pct']:.0f}%)", o["motivo"]] for o in ajustes])]
        p += ["", "> Los precios TP/SL están calculados sobre el precio de referencia; " + (
              "al ejecutar se recalculan con los mismos porcentajes sobre el precio real." if auto else
              "si al ejecutar el precio difiere mucho, mantén los porcentajes. El próximo informe leerá tu cartera real "
              "de eToro; solo si la API falla dará este plan por ejecutado a la apertura y comprobará con las velas "
              "diarias qué TP/SL saltaron."), ""]
    p += ["## Tu cartera", ""]
    if cart.get("origen_cartera") == "simulada":
        if "FileNotFoundError" in cart.get("error_api", ""):
            p += ["> [!warning] Todavía no hay conexión con tu cuenta de eToro (faltan las claves de la API): la cartera "
                  "se **estima** con el plan anterior y las velas diarias.", ""]
        else:
            p += [f"> [!warning] No se pudo leer tu cuenta de eToro ({cart.get('error_api', '')}): cartera **estimada** "
                  "con el plan anterior y las velas diarias. Si la clave ha caducado, crea otra (solo lectura) y guárdala "
                  f"en `{str(c['perfil'].claves).replace(str(Path.home()), '~')}`.", ""]
    p += [f"Valor de las posiciones: **{_usd(total)}** · efectivo libre: {_usd(cart['efectivo_usd'])}"
          + (f" · reservado en órdenes: {_usd(cart['reservado_usd'])}" if cart.get("reservado_usd") else ""), ""]
    p.append(_tabla(["Activo", "Unidades", "Precio", "Valor", "Peso", "P/L", "1m", "3m", "TP", "SL"], [
        [f"{g['simbolo']} {g['nombre'][:24]}" + ("" if g["id"] else " ⚠ no está en eToro"), f"{g['unidades']:.5f}",
         g["precio"] or "–", _usd(g["valor_usd"]), f"{g['peso_pct'] or 0:.0f}%", _n(g["pl_pct"]), _n(g.get("r1m")),
         _n(g.get("r3m")), g["tp"] or "–", g["sl"] or "–"] for g in pos]) if pos else "(sin posiciones)")
    if cart.get("ordenes_etoro"):
        p += ["", "**Órdenes pendientes en eToro:** " + ", ".join(
            f"{o['simbolo']} {_usd(o['importe_usd'])} x{o['apalancamiento']}" for o in cart["ordenes_etoro"])]
    if c.get("cierres"):
        p += ["", "**Cerradas desde el último informe:**", ""]
        p += [f"- {x['simbolo']}: {x['motivo']}" + (f" el {x['fecha']} a {x['precio']} ({_usd(x['importe_usd'])})"
                                                    if x["precio"] is not None else "") for x in c["cierres"]]
    return p + [""]


def componer(mes, generado, inv, cribado, evaluacion, meta, c=None):
    """Markdown del informe (sin frontmatter). inv puede ser None si la investigación falló."""
    p = [f"# Inversiones · {nombre_mes(mes)}", "", f"> [!warning] {AVISO}", "",
         f"Generado {generado[:16].replace('T', ' ')} · universo cribado: {cribado['universo']} instrumentos de eToro "
         f"· datos de precios al {cribado['fecha']}" + (f" · {meta}" if meta else ""), ""]
    if c:
        p += _plan(mes, c)
    if inv:
        p += ["## Resumen de mercado", "", inv["resumen_mercado"], "", "## Ideas del mes", ""]
        p.append(_tabla(["#", "Activo", "Conv.", "P(subida)", "Objetivo", "Stop", "Peso del mes", "Apalanc.",
                         "Precio ref."], [
            [i, f"[{pk['simbolo']}]({pk.get('url', '')}) {pk['nombre']}", "★" * pk["conviccion"],
             f"{pk['probabilidad_subida_pct']:.0f}%", f"+{pk['objetivo_pct']:.0f}%", f"-{abs(pk['stop_pct']):.0f}%",
             f"{pk['peso_pct']:.0f}%" if pk["peso_pct"] else "vigilar", f"x{pk.get('apalancamiento', 1)}",
             pk.get("precio_ref", "–")]
            for i, pk in enumerate(inv["picks"], 1)]))
        for i, pk in enumerate(inv["picks"], 1):
            c = pk.get("cribado") or {}
            p += ["", f"### {i}. {pk['simbolo']} · {pk['nombre']}", ""]
            if pk.get("aviso"):
                p += [f"> ⚠ {pk['aviso']}", ""]
            p += [pk["tesis"], "", "**Catalizadores:**"]
            p += [f"- {c_['fecha']}: {c_['evento']}" for c_ in pk["catalizadores"]] or ["- (ninguno fechado)"]
            p += ["", f"**Riesgos:** {pk['riesgos']}", ""]
            if c:
                p += [f"*Cribado:* 1m {_n(c.get('r1m'))} · 3m {_n(c.get('r3m'))} · 12m {_n(c.get('r12m'))} · "
                      f"{c.get('cerca_max', '–')}% del máx. 52s · RSI {c.get('rsi14', '–')} · "
                      f"{c.get('inversores', 0):,} inversores en eToro".replace(",", "."), ""]
            if pk["fuentes"]:
                p.append("*Fuentes:* " + " · ".join(f"[{j}]({u})" for j, u in enumerate(pk["fuentes"][:8], 1)))
        p += ["", "## Calendario del mes", "",
              _tabla(["Fecha", "Evento", "Impacto"], [[e["fecha"], e["evento"], e["impacto"]] for e in inv["calendario"]]),
              "", "## Temas con viento a favor", ""] + [f"- {t}" for t in inv["temas"]]
        p += ["", "## Evitar este mes", ""] + [f"- **{e['simbolo']}**: {e['motivo']}" for e in inv["evitar"]]
        p += ["", "## Estrategia en eToro", "", inv["estrategia"]]
        if inv.get("cartera"):
            p += ["", "## Decisiones sobre la cartera", ""] + [
                f"- **{d['simbolo']}**: {d['accion']}" + (f" {d['pct_vender']:.0f}%" if d["accion"] == "reducir" else "")
                + f" · {d['motivo']}" for d in inv["cartera"]]
    else:
        p += ["## ⚠ Investigación no disponible", "",
              "La investigación con IA falló este mes; solo se incluye el cribado cuantitativo (sin catalizadores "
              "verificados: úsalo como lista de vigilancia, no como recomendación).", ""]

    p += ["", "## Evaluación de meses anteriores", ""]
    if evaluacion:
        p.append(_tabla(["Mes", "Media ideas (mes)", "SPY (mes)", "Positivas", "Llegaron al objetivo"], [
            [nombre_mes(e["mes"]), _n(e["media_mes"]), _n(e["spy_mes"]), f"{e['positivas']}/{e['total']}",
             f"{e['en_objetivo']}/{e['total']}"] for e in evaluacion]))
        ult = evaluacion[-1]
        p += ["", f"Detalle de {nombre_mes(ult['mes'])}:", "",
              _tabla(["Activo", "Conv.", "Objetivo", "A fin de mes", "Hasta hoy"], [
                  [f["simbolo"], f["conviccion"], _n(f["objetivo_pct"]), _n(f["r_mes"]), _n(f["r_hoy"])]
                  for f in ult["picks"]])]
    else:
        p.append("Primer informe: aún no hay histórico que evaluar.")
    if inv and inv.get("lecciones"):
        p += ["", f"**Lecciones aplicadas:** {inv['lecciones']}"]

    cab = ["Activo", "Tipo", "Punt.", "1m", "3m", "12m", "% máx", "RSI", "Vol 20/120", "Inversores"]
    fila = lambda f: [f"{f['simbolo']} {f['nombre'][:28]}", f["tipo"], f.get("puntuacion", "–"), _n(f["r1m"]),
                      _n(f["r3m"]), _n(f["r12m"]), f["cerca_max"], f["rsi14"], f["vol_ratio"], f["inversores"]]
    p += ["", "## Anexo: cribado cuantitativo", "", "### Momentum (mejores por tipo)", "",
          _tabla(cab, [fila(f) for f in cribado["momentum"][:40]]), "", "### Caídas fuertes en valores populares", "",
          _tabla(cab, [fila(f) for f in cribado["rebote"]]) if cribado["rebote"] else "(ninguno)", "",
          "### Referencias", "",
          _tabla(["Activo", "1m", "3m", "12m", "% máx", "RSI"], [
              [f"{r['simbolo']} {r['nombre'][:28]}", _n(r["r1m"]), _n(r["r3m"]), _n(r["r12m"]), r["cerca_max"],
               r["rsi14"]] for r in cribado["referencias"]])]
    return "\n".join(p) + "\n"


def a_html(md):
    cuerpo = markdown.markdown(re.sub(r"^> \[!warning\] ", "> ", md, flags=re.M), extensions=["tables"])
    estilos = {"<table>": '<table style="border-collapse:collapse;font-size:13px;margin:8px 0">',
               "<th>": '<th style="border:1px solid #d4d4d8;padding:4px 6px;background:#f4f4f5;text-align:left">',
               "<td>": '<td style="border:1px solid #d4d4d8;padding:4px 6px">',
               "<blockquote>": '<blockquote style="border-left:4px solid #f59e0b;margin:8px 0;padding:4px 10px;'
                               'background:#fffbeb;color:#713f12">'}
    for a, b in estilos.items():
        cuerpo = cuerpo.replace(a, b)
    return f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;line-height:1.45">{cuerpo}</div>'


def guardar(mes, generado, inv, cribado, evaluacion, meta, extra, c=None):
    """`c["perfil"]` = Perfil dueño del informe (rutas de notas, JSON y último correo)."""
    pf = c["perfil"]
    pf.informes.mkdir(parents=True, exist_ok=True)
    pf.notas.mkdir(parents=True, exist_ok=True)
    ESTADO.mkdir(parents=True, exist_ok=True)
    md = componer(mes, generado, inv, cribado, evaluacion, meta, c)
    plan = (c or {}).get("plan")
    picks = (inv or {}).get("picks", [])
    fm = ["---", "tags: [inversiones, informe]", f"mes: {mes}", f"generado: {generado}",
          f"investigacion: {'si' if inv else 'no'}",
          "ideas: [" + ", ".join(pk["simbolo"] for pk in picks) + "]", "---", "",
          f"Proyecto: [[inversiones]] · [[eToro]] · [[{pf.pref}/Cartera|Cartera]]", ""]
    nota = pf.notas / f"{mes}.md"
    nota.write_text("\n".join(fm) + md)
    _indice(pf)
    if c:
        nota_cartera(c)

    (pf.informes / f"{mes}.json").write_text(json.dumps({
        "mes": mes, "generado": generado, "investigacion": bool(inv), **extra,
        "picks": [{k: pk.get(k) for k in ("simbolo", "id", "nombre", "tipo", "conviccion", "probabilidad_subida_pct",
                                          "objetivo_pct", "stop_pct", "peso_pct", "precio_ref")} for pk in picks],
        "detalle": inv, "evaluacion": evaluacion, "plan": plan,
    }, ensure_ascii=False, indent=1))

    compras = [f"{o['simbolo']} {o['importe_usd']:.0f}$" for o in (plan or {}).get("ordenes", []) if o["tipo"] == "comprar"]
    ideas = ", ".join(compras) if compras else ", ".join(pk["simbolo"] for pk in picks[:6])
    asunto = (f"📈 Inversiones {nombre_mes(mes)}: {ideas}" if inv
              else f"⚠ Inversiones {nombre_mes(mes)}: solo cribado (falló la investigación)")
    if (c or {}).get("cart", {}).get("origen_cartera") == "simulada":
        asunto = "⚠ " + asunto.lstrip("⚠📈 ") + " · cartera estimada (falla la API de eToro)"
    pf.ultimo.parent.mkdir(parents=True, exist_ok=True)
    ultimo = {"perfil": pf.nombre, "mes": mes, "generado": generado, "investigacion": bool(inv), "asunto": asunto,
              "html": a_html(md), "nota": str(nota)}
    pf.ultimo.write_text(json.dumps(ultimo, ensure_ascii=False))
    return nota


def _indice(pf):
    filas = sorted(pf.notas.glob("????-??.md"), reverse=True)
    txt = ["---", "tags: [inversiones]", "---", "", "# Inversiones: informes mensuales", "",
           "Generados el día 1 de cada mes por [[inversiones]] (lanzado por un timer de systemd). " + AVISO, ""]
    txt += [f"- [[{pf.pref}/{f.stem}|{nombre_mes(f.stem).capitalize()}]]" for f in filas]
    semanas = sorted((pf.notas / "Semanal").glob("????-W??.md"), reverse=True)
    if semanas:
        txt += ["", "## Informes semanales", ""] + [f"- [[{pf.pref}/Semanal/{f.stem}|Semana {f.stem}]]" for f in semanas]
    (pf.notas / "Índice.md").write_text("\n".join(txt) + "\n")


def nota_cartera(c):
    """Nota Inversiones/Cartera.md: posiciones actuales y últimos movimientos (fuente: cartera.json)."""
    cart, pf = c["cart"], c["perfil"]
    txt = ["---", "tags: [inversiones, cartera]", "---", "", "# Cartera en eToro" + ("" if pf.nombre == "titular" else f" · {pf.etiqueta}"), "",
           f"Base de datos: `~/work/inversiones/{pf.cartera.name}` (la actualiza [[inversiones]] cada día 1; corrección: "
           f"`python -m inversiones.cartera --perfil {pf.nombre}`).", ""]
    txt += _plan("", {**c, "plan": None})
    movs = cart["movimientos"][-40:][::-1]
    txt += ["## Últimos movimientos", "", _tabla(["Fecha", "Tipo", "Activo", "Unidades", "Precio", "Importe", "Nota"], [
        [m["fecha"], m["tipo"], m["simbolo"], m["unidades"] or "", m["precio"] or "", _usd(m["importe_usd"])
         if m["importe_usd"] is not None else "", m["nota"]] for m in movs]) if movs else "(ninguno)"]
    planes = sorted(cart["planes"].items(), reverse=True)
    if planes:
        txt += ["", "## Planes", ""] + [f"- [[{pf.pref}/{m}|{nombre_mes(m)}]]: {pl['estado']}, "
                                        f"{_usd(pl['presupuesto_usd'])}" for m, pl in planes]
    (pf.notas / "Cartera.md").write_text("\n".join(txt) + "\n")
