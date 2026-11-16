"""Informe mensual de inversiones en eToro: cribado cuantitativo + investigación con claude -p + informe.

python -m inversiones                    # mes en curso (lo lanza inversiones.timer el día 1)
python -m inversiones --mes 2026-11      # otro mes
python -m inversiones --sin-claude       # solo cribado y evaluación (prueba rápida, sin coste)
python -m inversiones --reusar-cribado   # reutiliza cache/cribado-<mes>.json si existe
python -m inversiones --solo-correo      # no ejecuta nada: reenvía ultimo.json solo a titular (prueba)
python -m inversiones --perfil perfil2     # solo ese perfil (repetible o con comas; por defecto titular, perfil2 e perfil3)
python -m inversiones --sin-correo       # genera todo y guarda, pero no envía ningún correo
python -m inversiones --prueba           # ensayo: no envía correo, no toca carteras ni notas/informes reales (salida en
                                         #   ~/.local/state/inversiones/prueba/<perfil>/)

Investigación compartida (catálogo, cribado, calendario y una sola llamada a claude -p con las posiciones de todos los
perfiles); después, por perfil: cartera real (API con sus claves), plan, informe y correo. Si falla un perfil, los demás
siguen y el error va solo a titular. Tras generar cada informe envía el correo (orquestar.py).
Salida 0 = informes generados (con o sin investigación) y correos enviados.
"""
import argparse
import json
import sys
from pathlib import Path
import time
from datetime import date, datetime

from . import calendario, cartera, cribado, etoro, informe, investigar, orquestar, perfiles

CAMPOS = ["simbolo", "nombre", "tipo", "sector", "r1m", "r3m", "r12m", "mom_12_1", "cerca_max", "rsi14",
          "vol_ratio", "volatilidad", "inversores", "inversores_crec", "pct_compra", "real", "resultados"]


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _csv(filas, campos):
    out = [",".join(campos)]
    for f in filas:
        out.append(",".join("" if f.get(c) is None else str(f.get(c)).replace(",", ";") for c in campos))
    return "\n".join(out)


def metricas_de(cat, simbolos, conocidas):
    """Métricas del cribado para símbolos que no estén ya en la lista corta (cartera, ideas de fuera)."""
    idx = etoro.por_simbolo(cat)
    faltan = {s.upper(): idx[s.upper()]["id"] for s in simbolos if s.upper() in idx and s.upper() not in conocidas}
    velas = etoro.velas_varios(sorted(faltan.values())) if faltan else {}
    for s, iid in faltan.items():
        m = cribado.metricas(velas.get(iid, []))
        if m:
            conocidas[s] = {**idx[s], **m}
    return conocidas


def texto_cartera(cart, cat, metr, resultados, perfil=None):
    pos, total = cartera.resumen(cart, cat, metr)
    cfg = cart["config"]
    marca = f"PERFIL {perfil.nombre}: " if perfil else ""
    if cfg.get("aportacion_modo") == "efectivo_real":
        amin, amax = cfg.get("aportacion_min_eur", 100), cfg.get("aportacion_max_eur", 200)
        rango = f"de {amin} €" if amin == amax else f"variable de {amin}-{amax} €"
        aport_txt = f"aportación {rango} (el dinero del mes es el efectivo libre)"
    else:
        aport_txt = f"aportación de este mes {cfg['aportacion_eur']} € ≈ {cfg['aportacion_eur'] * cartera.eurusd(cat):.0f} USD"
    fx = cartera.eurusd(cat)
    filas = [{**g, "resultados": (lambda r: f"{r['fecha']} {r['hora']}".strip() if r else "")(
        resultados.get(g["simbolo"].upper()))} for g in pos]
    campos = ["simbolo", "nombre", "sector", "unidades", "precio", "valor_usd", "peso_pct", "pl_pct", "apalancado",
              "r1m", "r3m", "r12m", "rsi14", "volatilidad", "cerca_max", "resultados", "tp", "sl"]
    sin = [g["simbolo"] for g in pos if not g["id"]]
    return (f"\nCARTERA ACTUAL ({marca}valor {total:.0f} USD; efectivo libre {cart['efectivo_usd']:.2f} USD; "
            f"{aport_txt} a EUR/USD {fx}; "
            f"reservado en órdenes pendientes {cart.get('reservado_usd', 0):.2f} USD; "
            "pl_pct vacío = precio de entrada desconocido; tp/sl vacíos = la posición NO tiene take profit/stop loss):\n"
            + _csv(filas, campos)
            + "".join(f"\nOrden pendiente en eToro: comprar {o['simbolo']} por {o['importe_usd']} USD x{o['apalancamiento']}"
                      for o in cart.get("ordenes_etoro", []))
            + (f"\n(no encontradas en eToro, ignóralas: {', '.join(sin)})" if sin else ""))


def datos_para_claude(mes, hoy, crib, resultados, cat, evaluacion, txt_cartera=""):
    idx = etoro.por_simbolo(cat)
    for lista in (crib["momentum"], crib["rebote"]):
        for f in lista:
            r = resultados.get(f["simbolo"].upper())
            f["resultados"] = f"{r['fecha']} {r['hora']}".strip() if r else ""
    populares = sorted(((s, r) for s, r in resultados.items() if idx.get(s, {}).get("inversores", 0) >= 1000),
                       key=lambda x: x[1]["fecha"])
    partes = [
        f"MES OBJETIVO: {mes} · HOY: {hoy} · precios de eToro al {crib['fecha']}",
        "\nREFERENCIAS (índices, sectores, materias primas, cripto):\n"
        + _csv(crib["referencias"], ["simbolo", "nombre", "precio", "r1m", "r3m", "r12m", "cerca_max", "rsi14"]),
        "\nMOMENTUM (mejor puntuación compuesta por tipo; ordenado):\n" + _csv(crib["momentum"], ["puntuacion"] + CAMPOS),
        "\nREBOTE (caídas >12% en el último mes en valores populares con 12m positivo):\n" + _csv(crib["rebote"], CAMPOS),
        txt_cartera,
        "\nRESULTADOS ESTE MES de valores populares en eToro (>=1000 inversores):\n"
        + "\n".join(f"{s} {r['fecha']} {r['hora']} EPS prev. {r['eps_previsto']}" for s, r in populares),
    ]
    if evaluacion:
        partes.append("\nEVALUACIÓN DE INFORMES ANTERIORES (rentabilidad % a fin de su mes y hasta hoy):\n"
                      + json.dumps(evaluacion, ensure_ascii=False))
    partes.append(f"\nOPERABLES (símbolos eToro con >= {cribado.MIN_INVERSORES} inversores, de más a menos "
                  "popular):\n" + ",".join(crib["operables"]))
    return "\n".join(partes)


def validar(inv, cat, crib):
    """Asocia cada idea a su instrumento de eToro; descarta las que no existan."""
    idx = etoro.por_simbolo(cat)
    metr = {f["simbolo"].upper(): f for f in crib["momentum"] + crib["rebote"]}
    buenos = []
    for pk in inv["picks"]:
        ins = idx.get(pk["simbolo"].upper().strip())
        if not ins:
            log(f"aviso: {pk['simbolo']} no está en el catálogo de eToro; se descarta")
            continue
        if ins["tipo"] != "Acción":
            log(f"aviso: {pk['simbolo']} es {ins['tipo']}, no una acción; se descarta")
            continue
        if not ins.get("precio"):
            log(f"aviso: {pk['simbolo']} sin precio de referencia en el catálogo; se descarta")
            continue
        pk.update(simbolo=ins["simbolo"], id=ins["id"], url=etoro.url_mercado(ins["simbolo"]),
                  precio_ref=ins["precio"], cribado=metr.get(ins["simbolo"].upper()))
        if not ins["real"]:
            pk["aviso"] = "En eToro solo está disponible como CFD (comisiones nocturnas)."
        buenos.append(pk)
    inv["picks"] = buenos
    return inv


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mes", help="YYYY-MM (por defecto, el mes en curso)")
    ap.add_argument("--sin-claude", action="store_true")
    ap.add_argument("--reusar-cribado", action="store_true")
    ap.add_argument("--solo-correo", action="store_true", help="reenvía el último informe solo a titular")
    ap.add_argument("--perfil", action="append", help="titular, perfil2, perfil3 (repetible o con comas; por defecto todos)")
    ap.add_argument("--sin-correo", action="store_true", help="genera y guarda, pero no envía ningún correo")
    ap.add_argument("--prueba", action="store_true",
                    help="ensayo: sin correo, sin tocar carteras ni notas/informes reales (salida en estado/prueba/)")
    return ap


def pipeline(a, lista):
    """Devuelve (rcs, fallos, avisos): rcs[perfil] = 0 informe completo / 2 degradado; fallos[perfil] = motivo."""
    hoy = date.today()
    mes = a.mes or f"{hoy:%Y-%m}"
    t0 = time.time()
    generado = datetime.now().astimezone().isoformat(timespec="seconds")

    log(f"informe de {mes} ({', '.join(p.nombre for p in lista)}): catálogo de eToro")
    cat = etoro.catalogo()
    fcrib = etoro.CACHE / f"cribado-{mes}.json"
    if a.reusar_cribado and fcrib.exists():
        crib = json.loads(fcrib.read_text())
        log(f"cribado reutilizado de {fcrib}")
    else:
        crib = cribado.cribar(cat, log=log)
        fcrib.write_text(json.dumps(crib, ensure_ascii=False))

    an, me = map(int, mes.split("-"))
    resultados, fallidos = calendario.resultados_mes(an, me, desde=hoy if f"{hoy:%Y-%m}" == mes else None)
    log(f"calendario: {len(resultados)} presentaciones de resultados" + (f" ({len(fallidos)} días sin datos)" if fallidos else ""))
    evaluacion = informe.evaluar(mes, cat)
    log(f"evaluación: {len(evaluacion)} informes anteriores")

    # carteras reales de cada perfil (un perfil que falla no bloquea a los demás)
    fallos, avisos, datos_pf = {}, [], {}
    metr = {f["simbolo"].upper(): f for f in crib["momentum"] + crib["rebote"]}
    for pf in lista:
        try:
            cart = cartera.cargar(pf)
            cierres = cartera.actualizar(cart, mes, cat, hoy.isoformat(), log=log, perfil=pf)
            metricas_de(cat, [l["simbolo"] for l in cart["posiciones"]], metr)
            datos_pf[pf.nombre] = {"pf": pf, "cart": cart, "cierres": cierres}
            if cart.get("origen_cartera") == "simulada" and pf.nombre != "titular":
                ruta, err = str(pf.claves).replace(str(Path.home()), "~"), cart.get("error_api", "error de API")
                motivo = (f"faltan las claves de la API ({ruta})" if "FileNotFoundError" in err else
                          f"fichero de claves incompleto ({ruta}: ETORO_USER_KEY / ETORO_API_KEY)" if "KeyError" in err else err)
                avisos.append(f"{pf.etiqueta}: cartera simulada, {motivo}. Importes del plan para "
                              f"{cart['config']['aportacion_eur']} € de aportación.")
        except Exception as e:  # noqa: BLE001
            log(f"perfil {pf.nombre}: no se pudo cargar la cartera: {e!r}")
            fallos[pf.nombre] = f"cartera: {type(e).__name__}: {e}"

    inv, meta, extra = None, "", {}
    if not a.sin_claude and datos_pf:
        txt_carteras = "\nCARTERAS ACTUALES (una por perfil; las decisiones de 'cartera' son por símbolo y valen para todos):" \
            + "".join(texto_cartera(d["cart"], cat, metr, resultados, d["pf"]) for d in datos_pf.values())
        datos = datos_para_claude(mes, hoy.isoformat(), crib, resultados, cat, evaluacion, txt_carteras)
        for intento in (1, 2):
            try:
                inv, coste, turnos = investigar.investigar(mes, hoy.isoformat(), datos, log=log)
                inv = validar(inv, cat, crib)
                meta = f"investigación: {investigar.MODELO}, {turnos} turnos"
                extra = {"coste_usd": coste, "turnos": turnos, "modelo": investigar.MODELO}
                log(f"investigación lista: {len(inv['picks'])} ideas, {turnos} turnos, ${coste:.2f}")
                break
            except Exception as e:  # noqa: BLE001 — cualquier fallo de claude deja el informe en modo degradado
                log(f"investigación falló (intento {intento}): {e}")
                inv = None
    if inv:
        metricas_de(cat, [pk["simbolo"] for pk in inv["picks"]], metr)

    rcs = {}
    for nombre, d in datos_pf.items():
        pf, cart = d["pf"], d["cart"]
        try:
            plan = None
            if inv:
                plan = cartera.calcular_plan(cart, mes, hoy.isoformat(), inv, cat, metr, log=log)
                log(f"plan[{nombre}]: {len(plan['ordenes'])} órdenes, presupuesto {plan['presupuesto_usd']:.2f} USD")
            pos = cartera.resumen(cart, cat, metr)
            if not a.sin_claude and not a.prueba:   # --sin-claude y --prueba no tocan la base de datos de la cartera
                cartera.guardar(cart, pf)
            nota = informe.guardar(mes, generado, inv, crib, evaluacion, meta, extra,
                                   {"plan": plan, "posiciones": pos, "cierres": d["cierres"], "cart": cart, "perfil": pf})
            log(f"informe[{nombre}] guardado en {nota} ({(time.time() - t0) / 60:.1f} min)")
            rcs[nombre] = 0 if inv or a.sin_claude else 2     # 2 = informe degradado (sin investigación)
        except Exception as e:  # noqa: BLE001
            log(f"perfil {nombre}: el informe falló: {e!r}")
            fallos[nombre] = f"informe: {type(e).__name__}: {e}"
    return rcs, fallos, avisos


def main():
    a = parser().parse_args()
    lista = perfiles.elegir(a.perfil)
    if a.prueba:
        lista = [perfiles.para_prueba(p, perfiles.ESTADO / "prueba") for p in lista]
    if a.solo_correo:
        try:
            for pf in (lista if a.perfil else lista[:1]):
                log(f"reenviando el último informe de {pf.nombre} a {', '.join(orquestar.enviar_informe(pf, solo_titular=True))}")
            return 0
        except Exception as e:  # noqa: BLE001
            log(f"error enviando el correo: {e}")
            return 1
    enviar = not (a.sin_correo or a.prueba)
    try:
        rcs, fallos, avisos = pipeline(a, lista)
    except Exception as e:  # noqa: BLE001 — cualquier fallo se avisa por correo y sale con error
        log(f"el informe falló: {e!r}")
        if enviar:
            try:
                orquestar.enviar_error(f"{type(e).__name__}: {e}")
            except Exception as e2:  # noqa: BLE001
                log(f"tampoco se pudo enviar el correo de error: {e2}")
        raise
    por_nombre = {p.nombre: p for p in lista}
    for nombre, rc in rcs.items():
        if not enviar:
            continue
        try:
            dest = orquestar.enviar_informe(por_nombre[nombre])
            log(f"correo de {nombre} enviado a {', '.join(dest)}" + (" (informe degradado)" if rc == 2 else ""))
        except Exception as e:  # noqa: BLE001
            log(f"error enviando el correo de {nombre}: {e}")
            fallos[nombre] = f"correo: {e}"
    if enviar:
        try:
            if fallos:
                orquestar.enviar_error("; ".join(f"{n}: {m}" for n, m in fallos.items()),
                                       "Inversiones: fallo en el informe de " + ", ".join(fallos))
            if avisos:
                orquestar.enviar_aviso(avisos)
        except Exception as e:  # noqa: BLE001
            log(f"tampoco se pudo enviar el aviso: {e}")
    elif avisos or fallos:
        log("avisos: " + " | ".join(avisos) + (" · fallos: " + "; ".join(f"{n}: {m}" for n, m in fallos.items()) if fallos else ""))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
