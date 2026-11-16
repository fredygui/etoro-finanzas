"""Broker eToro (cuenta REAL del titular, en USD): la mesa gestiona TODA la cartera de acciones x1 de la cuenta.

- Estado propio en data/etoro.json (abiertas, cerradas, ignoradas, capital_inicial_usd).
- Adopta toda posición larga x1 de acciones que aparezca en la cuenta (las de antes de la mesa y las abiertas a
  mano). Si su SL/TP de eToro es real (no el 0,0001 / ~11× de «sin SL/TP»), lo toma como plan provisional; la
  posición queda `plan_comite=False` hasta que el comité le ponga plan (eso convoca la primera reunión).
- Compras: a mercado x1, settlementType real, por importe en USD, con stopLossRate y takeProfitRate EN LA MISMA
  ORDEN. x-request-id propio: la posición se guarda «incierta» ANTES de enviar y nunca se reintenta; si la respuesta
  se pierde, el ciclo siguiente la busca por referenceId (orders:lookup) y la resuelve o la descarta a los 30 min.
- Plan (stop/objetivo) = SL/TP en eToro, que es la red de seguridad ante huecos y caídas de servidor. Tras cada cambio
  se relee la cartera; si el SL de eToro no coincide, se reintenta (como mucho cada `sl_reintento_min`). Si el bid
  cae bajo el stop con el mercado abierto y eToro no la cerró, la mesa la cierra a mercado.
- Cierres: a mercado por la posición entera; el resultado real (closeRate, netProfit) se toma del historial.
- modo «lectura»: cualquier POST/PATCH/DELETE (salvo el what-if de costes) lanza PermissionError.
"""
import time
import uuid

from . import etoro, horario, universo
from .comun import DATA, ahora, de_iso, guardar, iso, leer, log

ESTADO_ET = DATA / "etoro.json"
ESPERA_S, PASO_S = 90, 5


class ErrorBroker(Exception):
    def __init__(self, msg, definitivo=True):
        super().__init__(msg)
        self.definitivo = definitivo


def _r(x):
    """Redondeo de precios para eToro (2 decimales ≥ 10; 4 por debajo)."""
    return round(float(x), 2 if x >= 10 else 4)


def sl_real(v):
    sl, ent = v.get("stopLossRate") or 0, v.get("openRate") or 0
    return sl if (not v.get("isNoStopLoss") and ent and sl > 0.05 * ent) else None


def tp_real(v):
    tp, ent = v.get("takeProfitRate") or 0, v.get("openRate") or 0
    return tp if (not v.get("isNoTakeProfit") and ent and 0 < tp < 5 * ent) else None


def solo_lectura(f):
    def g(metodo, ruta, cuerpo=None, ref=None, **k):
        if metodo != "GET" and ruta != "/api/v2/trading/info/costs":
            raise PermissionError(f"modo lectura: bloqueado {metodo} {ruta}")
        return f(metodo, ruta, cuerpo, ref=ref, **k)
    return g


class Etoro:
    def __init__(self, c, api=None, ruta=None):
        self.c, self.r = c, c["riesgo"]
        self.nombre = "eToro" if c["modo"] == "real" else f"eToro-{c['modo']}"
        f = api or etoro.api
        self.f = f if c["modo"] == "real" else solo_lectura(f)
        self.ruta = ruta or ESTADO_ET
        self.s = leer(self.ruta, {"abiertas": [], "cerradas": [], "ignoradas": [], "capital_inicial_usd": None})
        self._cp, self._hist = None, None

    # ------------------------------------------------------------ estado y lectura
    def _guardar(self):
        guardar(self.ruta, self.s)

    def cartera(self, fresca=False):
        if self._cp is None or fresca:
            self._cp = etoro.cartera(f=self.f)
        return self._cp

    def vivas(self, fresca=False):
        return {str(v["positionID"]): v for v in self.cartera(fresca)["positions"]}

    @property
    def capital(self):
        return self.s.get("capital_inicial_usd") or self.patrimonio()

    def abiertas(self):
        return self.s["abiertas"]

    def sin_plan(self):
        return [p for p in self.s["abiertas"] if p.get("estado") == "abierta" and not p.get("plan_comite")]

    def colchon(self):
        cr = self.cartera()["credit"]
        return max(self.r.get("colchon_min_usd", 1.0), cr * self.r.get("colchon_pct", 0.5) / 100)

    def efectivo(self):
        """Efectivo real disponible («credit», ya sin lo reservado por órdenes pendientes) menos el colchón y lo
        comprometido en compras inciertas. NUNCA se ingresa dinero: es el tope de cualquier compra."""
        inc = sum(p["importe"] + self.r["comision_compra_usd"] for p in self.s["abiertas"] if p.get("estado") == "incierta")
        return max(0.0, self.cartera()["credit"] - self.colchon() - inc)

    def patrimonio(self):
        cp = self.cartera()
        inc = sum(p["importe"] for p in self.s["abiertas"] if p.get("estado") == "incierta")
        return cp["credit"] + inc + sum(v["amount"] + ((v.get("unrealizedPnL") or {}).get("pnL") or 0)
                                        for v in cp["positions"] if not v.get("mirrorID"))

    def costes_pct(self, importe, moneda):
        """Costes de ida y vuelta en % del importe (sin el diferencial, que se mide aparte con bid/ask)."""
        r = self.r
        fijo = (r["comision_compra_usd"] + r["comision_venta_usd"]) / max(importe, 1) * 100
        return fijo + (2 * r["conversion_pct"] if moneda != "USD" else 0)

    def equilibrio(self, p):
        """Bid al que vender recupera lo invertido con las comisiones de compra y venta (y conversión)."""
        return _r(p["entrada"] * (1 + self.costes_pct(p["importe"], p.get("moneda", "USD")) / 100))

    def info_posicion(self, p):
        v = self.vivas().get(p["id"]) or {}
        u = v.get("unrealizedPnL") or {}
        return {"simbolo": p["simbolo"], "region": p.get("region"), "moneda": p.get("moneda"),
                "entrada": p["entrada"], "unidades": round(p.get("unidades") or 0, 6), "importe_usd": round(p["importe"], 2),
                "precio_venta": u.get("closeRate"), "pnl_usd": u.get("pnL"),
                "pnl_pct": round(u["pnL"] / p["importe"] * 100, 2) if u.get("pnL") is not None and p["importe"] else None,
                "precio_equilibrio": self.equilibrio(p), "sl_etoro": p.get("sl_etoro"), "tp_etoro": p.get("tp_etoro"),
                "adoptada": bool(p.get("adoptada")), "plan_comite": bool(p.get("plan_comite")),
                **({"venta_aplazada": p["venta_aplazada"]} if p.get("venta_aplazada") else {})}

    def precio_venta(self, p):
        return ((self.vivas().get(p["id"]) or {}).get("unrealizedPnL") or {}).get("closeRate")

    def historial(self, desde):
        if self._hist is None:
            try:
                self._hist = {str(h.get("positionId")): h for h in etoro.historial(desde, f=self.f)}
            except (etoro.ErrorAPI, OSError) as e:
                log(f"etoro: historial no disponible ({str(e)[:120]})")
                return {}
        return self._hist

    # ------------------------------------------------------------ sincronización
    def _instrumento(self, iid):
        i = universo.instrumento(iid)
        if i:
            return i
        return {"id": int(iid), "simbolo": str(iid), "nombre": "", "bolsa_id": None, "region": None, "moneda": "USD"}

    def _adoptar(self, v):
        i = self._instrumento(v["instrumentID"])
        sl, tp = sl_real(v), tp_real(v)
        p = {"id": str(v["positionID"]), "simbolo": i["simbolo"], "instrumento": int(v["instrumentID"]),
             "bolsa_id": i.get("bolsa_id"), "region": i.get("region"), "moneda": i.get("moneda", "USD"),
             "entrada": v["openRate"], "unidades": v["units"], "importe": v["amount"], "abierta": iso(de_iso(v["openDateTime"])),
             "stop": sl, "objetivo": tp, "sl_etoro": sl, "tp_etoro": tp, "adoptada": True, "plan_comite": False,
             "estado": "abierta", "comision_compra": v.get("totalExternalFees")}
        self.s["abiertas"].append(p)
        log(f"etoro: adoptada {p['simbolo']} (pos {p['id']}, {p['importe']:.2f} $ a {p['entrada']}, SL {sl or '—'}, "
            f"TP {tp or '—'}{', región ' + str(p['region']) if p['region'] else ', bolsa no operable por la mesa'})")
        return p

    def _cerrada(self, p, px, motivo, pnl, cuando=None):
        p.update(salida=px, cerrada=cuando or iso(), motivo=motivo, pnl=round(pnl, 2) if pnl is not None else None,
                 pnl_pct=round(pnl / p["importe"] * 100, 2) if pnl is not None and p["importe"] else None, estado="cerrada")
        if p in self.s["abiertas"]:
            self.s["abiertas"].remove(p)
        self.s["cerradas"].append(p)
        self._guardar()
        return p

    def _cierre_externo(self, p):
        """La posición ya no está en la cuenta: SL/TP de eToro, cierre a mano o venta de la mesa sin confirmar."""
        h = self.historial(p["abierta"][:10]).get(p["id"])
        if h:
            px, pnl = h.get("closeRate"), h.get("netProfit")
            motivo = "cerrada en eToro"
            if p.get("cierre"):
                motivo = p["cierre"].get("motivo", "venta de la mesa")
            elif px and p.get("sl_etoro") and px <= p["sl_etoro"] * 1.01:
                motivo = "stop (SL de eToro)"
            elif px and p.get("tp_etoro") and px >= p["tp_etoro"] * 0.99:
                motivo = "objetivo (TP de eToro)"
            return self._cerrada(p, px, motivo, pnl, iso(de_iso(h["closeTimestamp"])) if h.get("closeTimestamp") else None)
        if not p.get("sin_historial"):                    # el historial tarda: se reintenta en el ciclo siguiente
            p["sin_historial"] = iso()
            self._guardar()
            return None
        if (ahora() - de_iso(p["sin_historial"])).total_seconds() < 3600:
            return None
        return self._cerrada(p, None, "cerrada fuera de la mesa (sin datos en el historial)", None)

    def _resolver_incierta(self, p, vivas, conocidas):
        edad = (ahora() - de_iso(p["abierta"])).total_seconds()
        try:
            o = etoro.consultar_orden(order_id=p.get("order_id"), referencia=None if p.get("order_id") else p["ref"],
                                      f=self.f)
        except etoro.ErrorAPI as e:
            o = None
            if e.codigo not in (404, 400):
                log(f"etoro: no pude consultar la compra incierta de {p['simbolo']} ({e})")
                return
        st = ((o or {}).get("status") or {}).get("id")
        if st in etoro.HECHA:
            self._rellenar(p, o)
            log(f"etoro: compra incierta de {p['simbolo']} confirmada (pos {p['id']})")
            return
        if st in etoro.FALLIDA:
            log(f"etoro: compra incierta de {p['simbolo']} no se ejecutó ({(o.get('status') or {}).get('errorMessage')})")
            self.s["abiertas"].remove(p)
            return
        if o is None:                                     # sin rastro: ¿apareció la posición igualmente?
            nuevas = [v for k, v in vivas.items() if k not in conocidas and int(v["instrumentID"]) == p["instrumento"]
                      and de_iso(v["openDateTime"]) >= de_iso(p["abierta"]).replace(second=0)]
            if nuevas:
                v = nuevas[0]
                p.update(id=str(v["positionID"]), entrada=v["openRate"], unidades=v["units"], importe=v["amount"],
                         sl_etoro=sl_real(v), tp_etoro=tp_real(v), estado="abierta")
                log(f"etoro: compra incierta de {p['simbolo']} encontrada en la cartera (pos {p['id']})")
                return
            if edad > 1800:
                log(f"etoro: compra incierta de {p['simbolo']} descartada (sin rastro en 30 min)")
                self.s["abiertas"].remove(p)
            return
        if st in etoro.EN_CURSO and edad > 600 and p.get("order_id"):
            try:
                self.f("DELETE", f"/api/v2/trading/execution/orders/{p['order_id']}")
                log(f"etoro: compra de {p['simbolo']} seguía sin ejecutar a los 10 min; cancelada")
            except (etoro.ErrorAPI, PermissionError) as e:
                log(f"etoro: no pude cancelar la compra pendiente de {p['simbolo']} ({e})")

    def revisar(self, precios=None):
        """Sincroniza con la cuenta: confirma compras inciertas, adopta posiciones nuevas, detecta cierres en eToro,
        actualiza unidades/importe y asegura el SL/TP en eToro. `precios` = {iid: {bid, ask}} (opcional) para el
        cierre de respaldo por stop. Devuelve las posiciones cerradas en este ciclo."""
        vivas = self.vivas(fresca=True)
        self._hist = None
        cierres = []
        primera = self.s.get("capital_inicial_usd") is None
        for p in [q for q in self.s["abiertas"] if q.get("estado") == "incierta"]:
            conocidas = {q["id"] for q in self.s["abiertas"] if q.get("id")}
            self._resolver_incierta(p, vivas, conocidas)
        conocidas = {q["id"] for q in self.s["abiertas"] if q.get("id")} | set(self.s.get("ignoradas", []))
        for k, v in vivas.items():
            if k in conocidas:
                continue
            if v.get("mirrorID") or not v.get("isBuy", True) or v.get("leverage", 1) != 1 or v.get("settlementTypeID", 1) != 1:
                self.s.setdefault("ignoradas", []).append(k)
                log(f"etoro: ignoro la posición {k} (copia, corto, apalancada o CFD)")
                continue
            self._adoptar(v)
        for p in [q for q in self.s["abiertas"] if q.get("estado") == "abierta"]:
            v = vivas.get(p["id"])
            if v is None:
                x = self._cierre_externo(p)
                if x:
                    cierres.append(x)
                continue
            p.update(unidades=v["units"], importe=v["amount"], sl_etoro=sl_real(v), tp_etoro=tp_real(v))
            p.pop("sin_historial", None)
            if p.get("cierre") and (ahora() - de_iso(p["cierre"]["t"])).total_seconds() > 1800:
                log(f"etoro: la venta de {p['simbolo']} no se ejecutó en 30 min; vuelve a estar abierta")
                p.pop("cierre")
        if primera:
            self._guardar()
            self.s["capital_inicial_usd"] = round(self.patrimonio(), 2)
            self.s["capital_desde"] = iso()
            log(f"etoro: capital inicial de la mesa {self.s['capital_inicial_usd']:.2f} $ (valor total de la cuenta)")
        self._guardar()
        for p in [q for q in self.s["abiertas"] if q.get("estado") == "abierta" and q.get("plan_comite")]:
            if not horario.abierto(p.get("bolsa_id")):
                continue
            self.asegurar_sl(p, vivas.get(p["id"]))
            bid = (precios or {}).get(p["instrumento"], {}).get("bid") or self.precio_venta(p)
            if bid and p.get("stop") and bid <= p["stop"] and not p.get("cierre"):
                log(f"etoro: {p['simbolo']} bid {bid} ≤ stop {p['stop']} y sigue abierta: cierre de respaldo")
                try:
                    cierres.append(self.cerrar(p, "stop (respaldo de la mesa)"))
                except (ErrorBroker, etoro.ErrorAPI, PermissionError) as e:
                    log(f"etoro: cierre de respaldo de {p['simbolo']} falló ({e})")
        self._guardar()
        return cierres

    def asegurar_sl(self, p, v=None, forzar=False):
        """Comprueba que el SL/TP de eToro coincide con el plan; si no, lo reenvía (como mucho cada
        `sl_reintento_min`). Devuelve True si coincide."""
        tol = self.r.get("sl_tolerancia_pct", 0.5) / 100
        ok_sl = p.get("sl_etoro") and p.get("stop") and abs(p["sl_etoro"] - p["stop"]) <= p["stop"] * tol
        ok_tp = p.get("tp_etoro") and p.get("objetivo") and abs(p["tp_etoro"] - p["objetivo"]) <= p["objetivo"] * tol
        if ok_sl and ok_tp:
            p.pop("sl_fallos", None)
            return True
        u = p.get("sl_patch_t")
        if not forzar and u and (ahora() - de_iso(u)).total_seconds() < self.r.get("sl_reintento_min", 20) * 60:
            return False
        p["sl_patch_t"] = iso()
        try:
            etoro.fijar_sl_tp(p["id"], p["stop"], p["objetivo"], f=self.f)
            log(f"etoro: SL/TP de {p['simbolo']} enviados a eToro: {p['stop']} / {p['objetivo']} "
                f"(antes {p.get('sl_etoro')} / {p.get('tp_etoro')})")
        except (etoro.ErrorAPI, PermissionError) as e:
            p["sl_fallos"] = p.get("sl_fallos", 0) + 1
            log(f"etoro: no pude poner SL/TP de {p['simbolo']} ({str(e)[:200]}); fallo {p['sl_fallos']}")
        self._guardar()
        return False

    def verificar_sl(self, p, intentos=3):
        """Tras un PATCH (asíncrono) relee la cartera hasta ver el SL/TP aplicados."""
        for _ in range(intentos):
            time.sleep(PASO_S)
            v = self.vivas(fresca=True).get(p["id"])
            if not v:
                return False
            p.update(sl_etoro=sl_real(v), tp_etoro=tp_real(v))
            if self.asegurar_sl(p, v, forzar=False) or (p.get("sl_etoro") and p["sl_etoro"] == p.get("stop")):
                self._guardar()
                return True
        self._guardar()
        return False

    # ------------------------------------------------------------ órdenes
    def fijar_plan(self, p, stop, objetivo):
        cambia = (stop, objetivo) != (p.get("sl_etoro"), p.get("tp_etoro"))
        p.update(stop=_r(stop), objetivo=_r(objetivo), plan_comite=True, plan_desde=iso())
        p.pop("venta_aplazada", None)
        self._guardar()
        if cambia:
            p.pop("sl_patch_t", None)
            self.asegurar_sl(p, forzar=True)
            if not self.verificar_sl(p):
                raise ErrorBroker(f"plan guardado pero eToro aún no muestra SL {p['stop']} / TP {p['objetivo']} "
                                  f"(ve {p.get('sl_etoro')} / {p.get('tp_etoro')}); el ciclo lo reintenta", False)

    def _rellenar(self, p, o):
        pe = (o.get("positionExecutions") or [{}])[0]
        od = pe.get("openingData") or {}
        p.update(id=str(pe.get("positionId")), entrada=od.get("avgPrice"), unidades=od.get("units"),
                 importe=pe.get("investedAmountCurrency") or p["importe"], estado="abierta",
                 comision_compra=round((od.get("fees") or 0) + (od.get("taxes") or 0), 2),
                 sl_etoro=pe.get("stopLossRate"), tp_etoro=pe.get("takeProfitRate"), coste_total=o.get("totalCosts"))

    def abrir(self, inst, importe, stop, objetivo):
        """Compra x1 con SL/TP en la orden. Nunca se reintenta. Devuelve la posición (estado abierta)."""
        if importe > self.efectivo() + 0.01:
            raise ErrorBroker(f"importe {importe:.2f} $ > efectivo disponible {self.efectivo():.2f} $")
        ref = str(uuid.uuid4())
        p = {"id": None, "ref": ref, "estado": "incierta", "simbolo": inst["simbolo"], "instrumento": int(inst["id"]),
             "bolsa_id": inst.get("bolsa_id"), "region": inst.get("region"), "moneda": inst.get("moneda", "USD"),
             "importe": round(importe, 2), "entrada": None, "stop": _r(stop), "objetivo": _r(objetivo),
             "abierta": iso(), "plan_comite": True, "plan_desde": iso(), "adoptada": False}
        self.s["abiertas"].append(p)
        self._guardar()
        try:
            r = etoro.abrir(inst["id"], importe, p["stop"], p["objetivo"], ref, f=self.f)
        except (etoro.ErrorAPI, PermissionError) as e:
            if isinstance(e, PermissionError) or not e.dudosa:
                self.s["abiertas"].remove(p)
                self._guardar()
                raise ErrorBroker(f"eToro rechazó la compra: {e}") from None
            raise ErrorBroker(f"respuesta dudosa de eToro ({e}); se buscará por referencia", False) from None
        p["order_id"] = r.get("orderId")
        self._guardar()
        fin = time.time() + ESPERA_S
        while time.time() < fin:
            time.sleep(PASO_S)
            try:
                o = etoro.consultar_orden(order_id=p["order_id"], f=self.f) if p["order_id"] else \
                    etoro.consultar_orden(referencia=ref, f=self.f)
            except etoro.ErrorAPI as e:
                if e.codigo != 404:
                    log(f"etoro: consulta de la compra de {p['simbolo']} falló ({e})")
                continue
            st = (o.get("status") or {}).get("id")
            if st in etoro.EN_CURSO:
                continue
            if st in etoro.FALLIDA:
                self.s["abiertas"].remove(p)
                self._guardar()
                raise ErrorBroker(f"orden {(o.get('status') or {}).get('name', st)}: "
                                  f"{(o.get('status') or {}).get('errorMessage') or ''}")
            if st in etoro.HECHA:
                self._rellenar(p, o)
                self._guardar()
                break
        else:
            try:
                if p["order_id"]:
                    self.f("DELETE", f"/api/v2/trading/execution/orders/{p['order_id']}")
                    log(f"etoro: compra de {p['simbolo']} sin ejecutar en {ESPERA_S} s; cancelo la orden")
            except (etoro.ErrorAPI, PermissionError) as e:
                log(f"etoro: no pude cancelar la orden de {p['simbolo']} ({e})")
            raise ErrorBroker(f"la compra no se ejecutó en {ESPERA_S} s (¿mercado cerrado?); el ciclo comprobará "
                              "si llegó a ejecutarse", False)
        if not self.verificar_sl(p, intentos=2):
            log(f"etoro: {p['simbolo']} comprada pero eToro aún no muestra el SL/TP del plan; el ciclo lo reintenta")
        return p

    def cerrar(self, p, motivo):
        """Cierre a mercado de la posición entera. Nunca se envía dos veces (marca `cierre` antes de enviar)."""
        if p.get("cierre"):
            raise ErrorBroker(f"{p['simbolo']}: ya hay una venta enviada a las {p['cierre']['t']}; esperando confirmación", False)
        if p["id"] in {str(o.get("positionID")) for o in self.cartera(fresca=True).get("ordersForClose") or []}:
            raise ErrorBroker(f"{p['simbolo']}: eToro ya tiene una orden de cierre pendiente", False)
        ref = str(uuid.uuid4())
        p["cierre"] = {"ref": ref, "t": iso(), "motivo": motivo}
        self._guardar()
        try:
            r = etoro.cerrar(p["id"], p["instrumento"], ref, f=self.f)
        except (etoro.ErrorAPI, PermissionError) as e:
            if isinstance(e, PermissionError) or not e.dudosa:
                p.pop("cierre")
                self._guardar()
                raise ErrorBroker(f"eToro rechazó la venta: {e}") from None
            raise ErrorBroker(f"respuesta dudosa de eToro al vender ({e}); el ciclo lo comprueba", False) from None
        oid = r.get("orderID") or r.get("orderId")
        p["cierre"]["order_id"] = oid
        self._guardar()
        fin = time.time() + ESPERA_S
        while time.time() < fin:
            time.sleep(PASO_S)
            if p["id"] not in self.vivas(fresca=True):
                break
        else:
            raise ErrorBroker(f"venta de {p['simbolo']} enviada (orden {oid}) pero sin ejecutar en {ESPERA_S} s; "
                              "el ciclo lo comprueba", False)
        self._hist = None
        for _ in range(4):
            h = self.historial(p["abierta"][:10]).get(p["id"])
            if h:
                return self._cerrada(p, h.get("closeRate"), motivo, h.get("netProfit"))
            time.sleep(PASO_S)
            self._hist = None
        try:
            c = etoro.consultar_cierre(oid, f=self.f) if oid else {}
            ps = c.get("positions") or []
            px = ps[0].get("rate") if ps else None
            ingreso = c.get("proceeds") or sum(x.get("amount") or 0 for x in ps) or None
            pnl = ingreso - p["importe"] if ingreso else None
        except etoro.ErrorAPI:
            px = pnl = None
        return self._cerrada(p, px, motivo, pnl)


def broker(c, api=None):
    return Etoro(c, api)
