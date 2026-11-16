"""Ejecución automática del plan en la cuenta REAL de eToro (solo perfiles con config.ejecucion_automatica).

Lo lanza inversiones-ejecutar.timer los lunes a las 16:00 (bolsa de EE. UU. abierta), tras el informe completo de las
06:00, que es el informe «antes». Orden: ventas → ajustes de TP/SL → compras x1. Al terminar sincroniza la cartera,
guarda la nota Inversiones/Ejecuciones/<fecha>.md y envía el informe «después» por correo.

Salvaguardas:
- Solo ejecuta un plan «pendiente» generado HOY a partir de la cuenta real (no de una cartera simulada).
- Nunca ingresa dinero: gasta como mucho el efectivo real («credit») menos un colchón; si no llega, las compras se
  reducen en proporción y se descartan las que queden por debajo del mínimo de eToro.
- Solo compras x1 de acciones reales (nada de CFD, cortos ni apalancamiento).
- Marca el plan «ejecutando» antes de la primera orden: un plan nunca se ejecuta dos veces.
- Pausa: si existe ~/.config/inversiones/pausa (o pausa-<perfil>) no opera.

python -m inversiones.ejecutar [--perfil titular] [--simular] [--forzar] [--sin-correo]
  --simular     no envía órdenes: consulta costes y muestra lo que haría (no toca la cartera ni envía correo)
  --forzar      acepta un plan pendiente aunque no sea de hoy
Salida: 0 ok o nada que hacer, 2 ejecutado con incidencias, 1 error.
"""
import argparse
import json
import math
import sys
import time
import uuid
from datetime import date, datetime

from . import cartera, etoro, informe, orquestar, perfiles

ESPERA_S, PASO_S = 240, 10           # cuánto se espera a que eToro ejecute cada tanda de órdenes
MIN_USD = 10                         # mínimo por posición en eToro
EN_CURSO, HECHA = {1, 2, 11, 12}, {3, 5}
ESTADOS = {1: "recibida", 2: "enviada", 3: "ejecutada", 4: "rechazada", 5: "ejecutada en parte", 6: "cancelándose",
           7: "cancelada", 8: "caducada", 9: "cancelada (parcial)", 10: "rechazada (parcial)",
           11: "esperando a que abra el mercado", 12: "esperando precio"}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _abajo(x, d=2):
    return math.floor(x * 10 ** d + 1e-9) / 10 ** d


def _precio(x):
    return round(x, 2 if x >= 10 else 4)


def _pnl(p):
    return p.get("unrealizedPnL") or {}


def foto(cp, nombres):
    """Efectivo, valor de posiciones y detalle por activo a partir de la respuesta de /pnl."""
    grupos = {}
    for p in cp["positions"]:
        g = grupos.setdefault(p["instrumentID"], {"simbolo": nombres.get(p["instrumentID"], str(p["instrumentID"])),
                                                  "unidades": 0.0, "invertido": 0.0, "valor": 0.0, "precio": None,
                                                  "tp": set(), "sl": set()})
        g["unidades"] += p["units"]
        g["invertido"] += p["amount"]
        g["valor"] += p["amount"] + (_pnl(p).get("pnL") or 0)
        g["precio"] = _pnl(p).get("closeRate")
        if p.get("takeProfitRate") and not p.get("isNoTakeProfit") and p["takeProfitRate"] < 5 * p["openRate"]:
            g["tp"].add(p["takeProfitRate"])
        if p.get("stopLossRate") and not p.get("isNoStopLoss") and p["stopLossRate"] > 0.05 * p["openRate"]:
            g["sl"].add(p["stopLossRate"])
    valor = sum(g["valor"] for g in grupos.values())
    return {"efectivo": round(cp["credit"], 2), "posiciones": round(valor, 2), "total": round(cp["credit"] + valor, 2),
            "detalle": sorted(({**g, "tp": sorted(g["tp"]), "sl": sorted(g["sl"])} for g in grupos.values()),
                              key=lambda g: -g["valor"])}


def plan_pendiente(cart, forzar):
    hoy = date.today().isoformat()
    pend = sorted((m, p) for m, p in cart.get("planes", {}).items() if p.get("estado") == "pendiente")
    if not pend:
        return None, None, "no hay ningún plan pendiente"
    mes, plan = pend[-1]
    if not forzar and plan.get("generado", "")[:10] != hoy:
        return None, None, f"el plan pendiente ({mes}) es del {plan.get('generado', '?')[:10]}, no de hoy"
    return mes, plan, None


class Ejecucion:
    def __init__(self, pf, simular):
        self.pf, self.simular = pf, simular
        self.ops, self.ajustes, self.incidencias = [], [], []
        self.cobro_simulado = 0.0          # --simular: lo que entraría por las ventas

    def _c(self, *a, **k):
        return k.pop("fn")(*a, claves=self.pf.claves, **k)

    # ------------------------------------------------------------ ventas
    def ventas(self, plan, cp):
        enviadas = []
        for o in (o for o in plan["ordenes"] if o["tipo"] == "vender"):
            lotes = [p for p in cp["positions"] if p["instrumentID"] == o["id"] and p.get("isBuy", True)]
            if not lotes:
                self.incidencias.append(f"{o['simbolo']}: no hay posición que vender (¿ya cerrada?)")
                continue
            frac = min(o["pct"], 100) / 100
            for p in lotes:
                valor = p["amount"] + (_pnl(p).get("pnL") or 0)
                uds = None if frac >= 1 else _abajo(p["units"] * frac, 6)
                if uds is not None and valor * frac < MIN_USD:
                    self.incidencias.append(f"{o['simbolo']}: la parte a vender de la posición {p['positionID']} "
                                            f"({valor * frac:.2f} $) no llega al mínimo; no se toca")
                    continue
                if uds is not None and valor * (1 - frac) < MIN_USD:
                    uds = None                 # lo que quedaría es menos del mínimo: se cierra entera
                op = {"tipo": "venta", "simbolo": o["simbolo"], "position_id": p["positionID"],
                      "unidades": uds or p["units"], "importe_previsto": round(valor * (frac if uds else 1), 2),
                      "pct": o["pct"], "estado": "simulada"}
                try:
                    op["costes"] = self._c({"action": "close", "positionIds": [p["positionID"]]}, fn=etoro.costes)
                    if self.simular:
                        self.cobro_simulado += op["importe_previsto"] - sum(op["costes"].values())
                    else:
                        r = self._c(p["positionID"], p["instrumentID"], uds, fn=etoro.cerrar, ref=str(uuid.uuid4()))
                        op.update(order_id=r["orderID"], estado="enviada")
                        enviadas.append(op)
                        log(f"venta {o['simbolo']} pos {p['positionID']} uds {uds or 'todas'}: orden {r['orderID']}")
                except Exception as e:  # noqa: BLE001
                    op.update(estado="error", error=str(e)[:300])
                    self.incidencias.append(f"venta de {o['simbolo']}: {e}")
                self.ops.append(op)
        self._esperar_cierres(enviadas)

    def _esperar_cierres(self, enviadas):
        fin = time.time() + ESPERA_S
        pendientes = list(enviadas)
        while pendientes and time.time() < fin:
            time.sleep(PASO_S)
            for op in list(pendientes):
                try:
                    r = self._c(op["order_id"], fn=etoro.consultar_cierre)
                except etoro.ErrorAPI as e:
                    if e.codigo == 404:
                        continue              # todavía no registrada
                    raise
                if r.get("errorCode"):
                    op.update(estado="rechazada", error=r.get("errorMessage") or f"código {r['errorCode']}")
                    self.incidencias.append(f"venta de {op['simbolo']} rechazada: {op['error']}")
                    pendientes.remove(op)
                elif r.get("positions"):
                    ps = r["positions"]
                    uds = sum(x.get("units") or 0 for x in ps)
                    op.update(estado="ejecutada", importe=round(r.get("proceeds") or sum(x.get("amount") or 0 for x in ps), 2),
                              precio=ps[0].get("rate"), unidades=uds or op["unidades"])
                    pendientes.remove(op)
        for op in pendientes:
            op["estado"] = "sin confirmar"
            self.incidencias.append(f"venta de {op['simbolo']} (orden {op['order_id']}) sin confirmar tras "
                                    f"{ESPERA_S // 60} min: ¿mercado cerrado? eToro la ejecutará en la apertura")

    # ------------------------------------------------------------ TP/SL de lo que ya hay
    def ajustar(self, plan, cp):
        for o in (o for o in plan["ordenes"] if o["tipo"] == "ajustar"):
            for p in (p for p in cp["positions"] if p["instrumentID"] == o["id"] and p.get("leverage", 1) == 1):
                precio = _pnl(p).get("closeRate") or o["precio_ref"]
                tp, sl = _precio(precio * (1 + o["tp_pct"] / 100)), _precio(precio * (1 - o["sl_pct"] / 100))
                aj = {"simbolo": o["simbolo"], "position_id": p["positionID"], "precio": precio, "tp": tp, "sl": sl,
                      "tp_antes": p.get("takeProfitRate"), "sl_antes": p.get("stopLossRate"), "estado": "simulado"}
                if not self.simular:
                    try:
                        self._c(p["positionID"], tp, sl, fn=etoro.fijar_tp_sl)
                        aj["estado"] = "hecho"
                    except Exception as e:  # noqa: BLE001
                        aj.update(estado="error", error=str(e)[:300])
                        self.incidencias.append(f"TP/SL de {o['simbolo']} ({p['positionID']}): {e}")
                self.ajustes.append(aj)

    # ------------------------------------------------------------ compras
    def compras(self, plan, cp, cat):
        compras = []
        for o in (o for o in plan["ordenes"] if o["tipo"] == "comprar"):
            if not cat.get(o["id"], {}).get("real"):
                self.incidencias.append(f"{o['simbolo']}: en eToro solo existe como CFD; no se compra")
                continue
            compras.append(dict(o))
        if not compras:
            return
        efectivo = cp["credit"]
        reserva = max(1.0, efectivo * 0.005)
        disponible = efectivo - reserva

        def coste(c):
            try:
                k = self._c({"action": "open", "transaction": "buy", "instrumentId": c["id"], "settlementType": "real",
                             "orderType": "mkt", "leverage": 1, "amount": max(c["importe_usd"], MIN_USD),
                             "orderCurrency": "usd"}, fn=etoro.costes)
                return round(sum(v for t, v in k.items() if t in ("transactionFee", "markup", "marketSpread", "sdrt")), 2)
            except Exception:  # noqa: BLE001 — sin dato: estimación prudente
                return round(1.0 + c["importe_usd"] * 0.003, 2)

        while compras:
            for c in compras:
                c["coste"] = coste(c)
            fijo = sum(c["coste"] for c in compras)
            total = sum(c["importe_usd"] for c in compras)
            factor = min(1.0, max(disponible - fijo, 0) / total) if total else 0
            for c in compras:
                c["importe"] = _abajo(c["importe_usd"] * factor)
            peor = min(compras, key=lambda c: c["importe"])
            if peor["importe"] >= max(MIN_USD, etoro.condiciones(peor["id"])["minimo_usd"]):
                break
            self.incidencias.append(f"{peor['simbolo']}: con el efectivo real ({efectivo:.2f} $) quedaba en "
                                    f"{peor['importe']:.2f} $, por debajo del mínimo; se reparte entre las demás")
            compras.remove(peor)
            for c in compras:                  # el dinero liberado se reparte en proporción al plan
                c["importe_usd"] = c["importe_usd"] * (1 + peor["importe_usd"] / sum(x["importe_usd"] for x in compras))
        if factor < 1 and compras:
            self.incidencias.append(f"efectivo real {efectivo:.2f} $ (colchón {reserva:.2f} $): compras reducidas al "
                                    f"{factor * 100:.0f}% de lo planeado")
        restante = disponible
        enviadas = []
        for c in compras:
            op = {"tipo": "compra", "simbolo": c["simbolo"], "importe_previsto": c["importe"],
                  "coste_previsto": c["coste"], "tp_pct": c["tp_pct"], "sl_pct": c["sl_pct"], "estado": "simulada"}
            if c["importe"] + c["coste"] > restante + 0.01:
                op.update(estado="omitida", error="no queda efectivo suficiente")
                self.incidencias.append(f"compra de {c['simbolo']} omitida: no queda efectivo")
            elif not self.simular:
                try:
                    r = self._c(c["id"], c["importe"], fn=etoro.abrir, ref=str(uuid.uuid4()))
                    op.update(order_id=r["orderId"], estado="enviada")
                    restante -= c["importe"] + c["coste"]
                    enviadas.append(op)
                    log(f"compra {c['simbolo']} {c['importe']:.2f} $: orden {r['orderId']}")
                except Exception as e:  # noqa: BLE001
                    op.update(estado="error", error=str(e)[:300])
                    self.incidencias.append(f"compra de {c['simbolo']}: {e}")
            else:
                restante -= c["importe"] + c["coste"]
            self.ops.append(op)
        self._esperar_compras(enviadas)

    def _esperar_compras(self, enviadas):
        fin = time.time() + ESPERA_S
        pendientes = list(enviadas)
        while pendientes and time.time() < fin:
            time.sleep(PASO_S)
            for op in list(pendientes):
                try:
                    r = self._c(op["order_id"], fn=etoro.consultar_orden)
                except etoro.ErrorAPI as e:
                    if e.codigo == 404:
                        continue
                    raise
                st = (r.get("status") or {}).get("id")
                if st in EN_CURSO:
                    continue
                pendientes.remove(op)
                if st not in HECHA:
                    op.update(estado=ESTADOS.get(st, f"estado {st}"),
                              error=(r.get("status") or {}).get("errorMessage") or "")
                    self.incidencias.append(f"compra de {op['simbolo']}: {op['estado']} {op['error']}".strip())
                    continue
                pe = (r.get("positionExecutions") or [{}])[0]
                od = pe.get("openingData") or {}
                op.update(estado="ejecutada", position_id=pe.get("positionId"), precio=od.get("avgPrice"),
                          unidades=od.get("units"), importe=pe.get("investedAmountCurrency") or op["importe_previsto"],
                          comisiones=round((od.get("fees") or 0) + (od.get("taxes") or 0) + (od.get("markup") or 0), 2),
                          coste_total=r.get("totalCosts"))
                if op["precio"] and op["position_id"]:
                    tp = _precio(op["precio"] * (1 + op["tp_pct"] / 100))
                    sl = _precio(op["precio"] * (1 - op["sl_pct"] / 100))
                    try:
                        self._c(op["position_id"], tp, sl, fn=etoro.fijar_tp_sl)
                        op.update(tp=tp, sl=sl)
                    except Exception as e:  # noqa: BLE001
                        self.incidencias.append(f"{op['simbolo']}: comprada, pero no se pudo poner TP/SL: {e}")
        for op in pendientes:                  # festivo o mercado cerrado: no se deja nada colgando
            try:
                self._c(op["order_id"], fn=etoro.cancelar_orden)
                op["estado"] = "cancelada (sin ejecutar a tiempo)"
            except Exception as e:  # noqa: BLE001
                op["estado"] = f"sin confirmar (no se pudo cancelar: {e})"
            self.incidencias.append(f"compra de {op['simbolo']}: {op['estado']}; ¿mercado cerrado?")


# ---------------------------------------------------------------- informe «después»

def _usd(x):
    return "–" if x is None else f"{x:,.2f} $".replace(",", " ")


def componer(fecha, mes, pf, antes, despues, ej, simular):
    fila = lambda t, k: f"| {t} | {_usd(antes[k])} | {_usd(despues[k]) if despues else '–'} | " + (
        f"{despues[k] - antes[k]:+.2f} $ |" if despues else "– |")
    md = [f"# {'Simulación' if simular else 'Ejecución'} en eToro · {fecha}", "",
          f"Plan del informe de hoy: [[{pf.pref}/{mes}|{mes}]] · perfil {pf.etiqueta}", "",
          "| | Antes | Después | Diferencia |", "|---|---|---|---|",
          fila("Efectivo", "efectivo"), fila("Posiciones", "posiciones"), fila("**Total**", "total"), ""]
    hechas = [o for o in ej.ops if o["estado"] == "ejecutada"]
    com = sum((o.get("comisiones") or 0) if o["tipo"] == "compra" else
              sum((o.get("costes") or {}).get(t, 0) for t in ("transactionFee", "markup", "marketSpread")) for o in hechas)
    md += [f"Operaciones ejecutadas: **{len(hechas)} de {len(ej.ops)}** · comisiones y spread ≈ {_usd(com)}. "
           "La diferencia del total incluye además lo que se han movido los precios durante el día.", ""]
    if ej.ops:
        md += ["## Operaciones", "", "| Operación | Activo | Importe | Unidades | Precio | TP / SL | Estado |",
               "|---|---|---|---|---|---|---|"]
        for o in ej.ops:
            imp = o.get("importe") if o.get("importe") is not None else o.get("importe_previsto")
            tpsl = f"{o['tp']} / {o['sl']}" if o.get("tp") else "–"
            md.append(f"| {o['tipo']}{' ' + str(int(o['pct'])) + '%' if o.get('pct') else ''} | {o['simbolo']} | "
                      f"{_usd(imp)} | {o.get('unidades') or '–'} | {o.get('precio') or '–'} | {tpsl} | "
                      f"{o['estado']}{': ' + o['error'][:80] if o.get('error') else ''} |")
        md.append("")
    if ej.ajustes:
        md += ["## TP/SL ajustados", "", "| Activo | Posición | Precio | Antes TP / SL | Ahora TP / SL | Estado |",
               "|---|---|---|---|---|---|"]
        md += [f"| {a['simbolo']} | {a['position_id']} | {a['precio']} | {a['tp_antes'] or '–'} / {a['sl_antes'] or '–'} | "
               f"{a['tp']} / {a['sl']} | {a['estado']} |" for a in ej.ajustes]
        md.append("")
    if ej.incidencias:
        md += ["## Incidencias", ""] + [f"- {i}" for i in ej.incidencias] + [""]
    if despues:
        md += ["## Cartera después", "", "| Activo | Unidades | Precio | Valor | P/L | TP | SL |", "|---|---|---|---|---|---|---|"]
        md += [f"| {g['simbolo']} | {g['unidades']:.5f} | {g['precio'] or '–'} | {_usd(g['valor'])} | "
               f"{(g['valor'] / g['invertido'] - 1) * 100:+.1f}% | {', '.join(map(str, g['tp'])) or '–'} | "
               f"{', '.join(map(str, g['sl'])) or '–'} |" for g in despues["detalle"]]
        md.append("")
    md += ["> Ejecutado automáticamente por [[inversiones]] (ejecutar.py). Solo acciones x1 y nunca más dinero del que "
           "hay en la cuenta. Para pausarlo: `touch ~/.config/inversiones/pausa`. No es asesoramiento financiero.", ""]
    return "\n".join(md)


def ejecutar(pf, a):
    cfg_cart = cartera.cargar(pf)
    if not cfg_cart["config"].get("ejecucion_automatica"):
        log(f"{pf.nombre}: ejecución automática desactivada en su cartera")
        return 0
    pausa = [f for f in (perfiles.CONFIG_DIR / "pausa", perfiles.CONFIG_DIR / f"pausa-{pf.nombre}") if f.exists()]
    if pausa and not a.simular:
        log(f"{pf.nombre}: en pausa ({pausa[0]}); no se opera")
        return 0
    cart = cfg_cart
    mes, plan, motivo = plan_pendiente(cart, a.forzar)
    if not plan:
        log(f"{pf.nombre}: nada que ejecutar ({motivo})")
        return 0
    if cart.get("origen_cartera") != "eToro":
        log(f"{pf.nombre}: el plan se calculó con una cartera simulada; no se ejecuta")
        return 0
    cat = etoro.catalogo()
    nombres = {v["id"]: v["simbolo"] for v in cat.values()}
    hoy = date.today().isoformat()
    cp = etoro.cuenta(pf.claves)
    antes = foto(cp, nombres)
    log(f"{pf.nombre}: plan {mes} ({len(plan['ordenes'])} órdenes); antes: efectivo {antes['efectivo']} $, "
        f"posiciones {antes['posiciones']} $")
    if not a.simular:
        plan["estado"] = "ejecutando"
        plan["ejecucion"] = {"inicio": datetime.now().astimezone().isoformat(timespec="seconds")}
        cartera.guardar(cart, pf)

    ej, despues, fallo = Ejecucion(pf, a.simular), None, None
    try:
        ej.ventas(plan, cp)
        cp = etoro.cuenta(pf.claves)
        cp["credit"] += ej.cobro_simulado
        ej.ajustar(plan, cp)
        ej.compras(plan, cp, cat)
    except Exception as e:  # noqa: BLE001 — se informa de lo hecho hasta el fallo
        fallo = f"{type(e).__name__}: {e}"
        ej.incidencias.append(f"la ejecución se detuvo: {fallo}")
        log(f"error: {fallo}")
    try:
        cp = etoro.cuenta(pf.claves)
        despues = foto(cp, nombres)
        if not a.simular:
            cartera.sincronizar(cart, cp, cat, hoy, mes=mes, log=log)
    except Exception as e:  # noqa: BLE001
        ej.incidencias.append(f"no se pudo leer la cuenta al terminar: {e}")

    md = componer(hoy, mes, pf, antes, despues, ej, a.simular)
    if a.simular:
        print(md)
        return 0
    rc = 2 if (fallo or ej.incidencias) else 0
    plan["estado"] = "ejecutado (eToro)" if rc == 0 else "ejecutado con incidencias (eToro)"
    plan["ejecucion"].update(fin=datetime.now().astimezone().isoformat(timespec="seconds"), antes={
        k: antes[k] for k in ("efectivo", "posiciones", "total")}, despues=despues and {
        k: despues[k] for k in ("efectivo", "posiciones", "total")}, operaciones=ej.ops, ajustes=ej.ajustes,
        incidencias=ej.incidencias)
    cartera.guardar(cart, pf)

    carpeta = pf.notas / "Ejecuciones"
    carpeta.mkdir(parents=True, exist_ok=True)
    nota = carpeta / f"{hoy}.md"
    nota.write_text("\n".join(["---", "tags: [inversiones, ejecucion]", f"fecha: {hoy}", f"plan: {mes}", "---", "",
                               f"Proyecto: [[inversiones]] · [[eToro]] · [[{pf.pref}/Cartera|Cartera]]", ""]) + md)
    (pf.informes / "ejecuciones").mkdir(parents=True, exist_ok=True)
    (pf.informes / "ejecuciones" / f"{hoy}.json").write_text(json.dumps(
        {"fecha": hoy, "plan": mes, **plan["ejecucion"]}, ensure_ascii=False, indent=1))
    log(f"informe de la ejecución en {nota}")

    if not a.sin_correo:
        n = sum(o["estado"] == "ejecutada" for o in ej.ops)
        dif = f" · total {despues['total'] - antes['total']:+.2f} $" if despues else ""
        asunto = (f"{'🤖' if rc == 0 else '⚠'} Inversiones: ejecutadas {n}/{len(ej.ops)} órdenes en eToro ({hoy}){dif}")
        for d in orquestar.destinatarios_perfil(pf.nombre):
            orquestar.enviar(d, asunto, informe.a_html(md), forzar_correo=True)
        log(f"correo enviado a {', '.join(orquestar.destinatarios_perfil(pf.nombre))}")
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--perfil", action="append", help="por defecto titular")
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--forzar", action="store_true")
    ap.add_argument("--sin-correo", action="store_true")
    a = ap.parse_args()
    rc = 0
    for pf in perfiles.elegir(a.perfil or ["titular"]):
        try:
            rc = max(rc, ejecutar(pf, a))
        except Exception as e:  # noqa: BLE001
            log(f"{pf.nombre}: error: {e!r}")
            if not (a.simular or a.sin_correo):
                try:
                    orquestar.enviar_error(f"{type(e).__name__}: {e}", "Inversiones: la ejecución automática ha fallado")
                except Exception as e2:  # noqa: BLE001
                    log(f"tampoco se pudo avisar: {e2}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
