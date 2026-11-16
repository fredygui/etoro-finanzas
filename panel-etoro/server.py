"""Panel eToro: web LAN (solo lectura salvo el botón de pausa) sobre la mesa de acciones.

Uso: .venv/bin/python server.py [puerto]   (por defecto 8777)
Lee: eToro (solo GET, etoro_ro.py), ~/work/mesa-acciones/data (etoro.json, estado.json, comites/) y config.json.
Escribe: data/historial.json (propio) y, solo con POST /api/modo, la clave "modo" de config.json de la mesa.
"""
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import etoro_ro

AQUI = Path(__file__).resolve().parent
STATIC = AQUI / "static"
HIST = Path(os.environ.get("PANEL_HISTORIAL", AQUI / "data/historial.json"))
MESA = Path(os.environ.get("PANEL_MESA", Path.home() / "work/mesa-acciones"))
CONFIG = Path(os.environ.get("PANEL_CONFIG", MESA / "config.json"))
DATA = MESA / "data"
ID_OK = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")
INTERVALOS = {"OneHour", "FourHours", "OneDay"}
MODOS = {"lectura", "real"}
CADA_SNAP = 900

# horario.py de la mesa: módulo puro (festivos y sesiones), se carga por ruta y solo se consulta
_sp = importlib.util.spec_from_file_location("horario_mesa", MESA / "mesa_acciones/horario.py")
horario = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(horario)


def leer(p, defecto=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return defecto


def guardar(p, datos):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def ahora_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ actas del comité
_ACTAS = {}


def _acta(f):
    m = f.stat().st_mtime
    c = _ACTAS.get(f.name)
    if c and c[0] == m:
        return c[1]
    a = leer(f, {}) or {}
    _ACTAS[f.name] = (m, a)
    return a


def _decs(a):
    return [d for d in ((a.get("salida") or {}).get("decisiones") or []) if isinstance(d, dict)]


def _tipo(decs, sal):
    acc = {d.get("accion") for d in decs}
    if not sal:
        return "fallo"
    if "comprar" in acc and "vender" in acc:
        return "mixta"
    return "compra" if "comprar" in acc else "venta" if "vender" in acc else "mantener"


def lista_actas(n=15):
    out = []
    for f in sorted((DATA / "comites").glob("*.json"), reverse=True)[:n]:
        a = _acta(f)
        dat = a.get("datos") or {}
        decs = _decs(a)
        chat = (a.get("salida") or {}).get("chat") or []
        jefe = next((c.get("mensaje") for c in reversed(chat) if isinstance(c, dict) and c.get("agente") == "JEFE"), "")
        out.append({
            "id": f.stem, "fecha": dat.get("fecha_utc") or f.stem, "motivo": str(dat.get("convocatoria") or "revisión")[:160],
            "tipo": _tipo(decs, a.get("salida")), "resumen": (jefe or "")[:420],
            "decisiones": [{k: d.get(k) for k in ("accion", "simbolo", "importe_usd", "stop", "objetivo", "confianza")}
                           | {"motivo": str(d.get("motivo") or "")[:260]} for d in decs],
            "coste": (a.get("consumo") or {}).get("coste_usd_equiv"),
        })
    return out


def acta_completa(id_):
    if not ID_OK.match(id_) or not (DATA / "comites" / f"{id_}.json").exists():
        return None
    a = _acta(DATA / "comites" / f"{id_}.json")
    dat = a.get("datos") or {}
    return {"id": id_, "fecha": dat.get("fecha_utc"), "motivo": dat.get("convocatoria"), "horario": dat.get("horario"),
            "chat": (a.get("salida") or {}).get("chat") or [], "decisiones": _decs(a)}


def tesis_por_simbolo(simbolos):
    """Última decisión del comité (mantener/comprar) de cada símbolo, mirando las 40 actas más recientes."""
    out, pend = {}, set(simbolos)
    for f in sorted((DATA / "comites").glob("*.json"), reverse=True)[:40]:
        if not pend:
            break
        a = _acta(f)
        fecha = (a.get("datos") or {}).get("fecha_utc") or f.stem
        for d in _decs(a):
            s = d.get("simbolo")
            if s in pend and d.get("accion") in ("mantener", "comprar", "vender"):
                out[s] = {k: d.get(k) for k in ("accion", "confianza", "motivo", "alcista", "bajista", "stop", "objetivo")} | {
                    "fecha": fecha, "acta": f.stem}
                pend.discard(s)
    return out


# ------------------------------------------------------------------ cuota Claude (solo lectura, caché 5 min)
_Q = {"t": 0.0, "v": None}


def cuota():
    if time.time() - _Q["t"] < 300:
        return _Q["v"]
    _Q["t"] = time.time()
    try:
        tok = json.loads((Path.home() / ".claude/.credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20", "User-Agent": "panel-etoro/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        _Q["v"] = {"h5": d["five_hour"]["utilization"], "semana": d["seven_day"]["utilization"]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _Q["v"]


# ------------------------------------------------------------------ cartera
def sl_real(v):
    sl, ent = v.get("stopLossRate") or 0, v.get("openRate") or 0
    return sl if (not v.get("isNoStopLoss") and ent and sl > 0.05 * ent) else None


def tp_real(v):
    tp, ent = v.get("takeProfitRate") or 0, v.get("openRate") or 0
    return tp if (not v.get("isNoTakeProfit") and ent and 0 < tp < 5 * ent) else None


def mercados():
    t = datetime.now(horario.MADRID)
    out = []
    for nombre, b in (("EE. UU.", 5), ("Zona euro", 6)):
        ab = horario.abierta_region("EEUU" if b == 5 else "UE", t)
        prox = None if ab else horario.proxima_apertura(b, t)
        if nombre == "Zona euro" and not ab:           # la UE abre si cualquiera de sus bolsas abre antes
            c = [x for x in (horario.proxima_apertura(i, t) for i in (6, 9, 10, 11)) if x]
            prox = min(c) if c else prox
        out.append({"nombre": nombre, "abierta": ab,
                    "proxima": prox.astimezone(horario.MADRID).strftime("%Y-%m-%dT%H:%M:%S%z") if prox else None})
    return out


def panel():
    cfg = leer(CONFIG, {}) or {}
    st = leer(DATA / "etoro.json", {}) or {}
    est = leer(DATA / "estado.json", {}) or {}
    cap = st.get("capital_inicial_usd")
    mesa = {p["id"]: p for p in st.get("abiertas", [])}
    aviso = None
    try:
        cp, edad = etoro_ro.cartera()
    except (OSError, ValueError, KeyError) as e:
        return {"error": f"No se pudo leer eToro ({type(e).__name__}). Reintenta en un minuto.", "modo": cfg.get("modo")}
    pos = []
    for v in cp.get("positions", []):
        if v.get("mirrorID") or not v.get("isBuy", True):
            continue
        m = mesa.get(str(v["positionID"]), {})
        u = v.get("unrealizedPnL") or {}
        imp, pnl = v.get("amount") or 0, u.get("pnL")
        pos.append({
            "id": str(v["positionID"]), "simbolo": m.get("simbolo") or f"#{v['instrumentID']}", "instrumento": v["instrumentID"],
            "importe": round(imp, 2), "entrada": v.get("openRate"), "precio": u.get("closeRate"),
            "pnl": pnl, "pnl_pct": round(pnl / imp * 100, 2) if pnl is not None and imp else None,
            "valor": round(imp + (pnl or 0), 2), "abierta": v.get("openDateTime"),
            "sl_etoro": sl_real(v), "tp_etoro": tp_real(v), "stop": m.get("stop"), "objetivo": m.get("objetivo"),
            "plan_comite": bool(m.get("plan_comite")), "adoptada": bool(m.get("adoptada")), "region": m.get("region"),
            "moneda": m.get("moneda", "USD")})
    credit = cp.get("credit") or 0
    try:  # EUR/USD (instrumento 1 de eToro) para mostrar importes en euros
        fx = etoro_ro.precios([1])[0].get(1)
        eurusd = round((fx["bid"] + fx["ask"]) / 2, 5) if fx else None
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        eurusd = None
    valor = round(credit + sum(p["valor"] for p in pos), 2)
    freno_pct = (cfg.get("riesgo") or {}).get("drawdown_max_pct") or 25
    limite = round(cap * (1 - freno_pct / 100), 2) if cap else None
    t = tesis_por_simbolo({p["simbolo"] for p in pos})
    for p in pos:
        p["tesis"] = t.get(p["simbolo"])
    pos.sort(key=lambda p: -p["valor"])
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cm = cfg.get("comite") or {}
    return {
        "ahora": ahora_iso(), "edad_datos_s": edad, "modo": cfg.get("modo"),
        "valor": valor, "efectivo": round(credit, 2), "capital": cap, "capital_desde": st.get("capital_desde"),
        "pl": round(valor - cap, 2) if cap else None, "pl_pct": round((valor / cap - 1) * 100, 2) if cap else None,
        "freno_pct": freno_pct, "freno_limite": limite,
        "freno_margen": round(valor - limite, 2) if limite else None,
        "freno_margen_pct": round((valor / limite - 1) * 100, 1) if limite else None,
        "invertido": round(sum(p["valor"] for p in pos), 2), "posiciones": pos, "mercados": mercados(),
        "comites_hoy": (est.get("comites") or {}).get(hoy, 0), "max_comites": cm.get("max_por_dia"),
        "ultimo_comite": est.get("ultimo_comite"), "cuota": cuota(),
        "cuota_max": {"h5": cm.get("cuota_5h_max"), "semana": cm.get("cuota_semanal_max")}, "aviso": aviso, "eurusd": eurusd,
    }


def operaciones():
    st = leer(DATA / "etoro.json", {}) or {}
    ev = []
    for p in st.get("abiertas", []) + st.get("cerradas", []):
        ev.append({"tipo": "adopción" if p.get("adoptada") else "compra", "simbolo": p.get("simbolo"),
                   "fecha": p.get("abierta"), "importe": p.get("importe"), "precio": p.get("entrada"),
                   "estado": p.get("estado")})
    for p in st.get("cerradas", []):
        ev.append({"tipo": "cierre", "simbolo": p.get("simbolo"), "fecha": p.get("cerrada"), "importe": p.get("importe"),
                   "precio": p.get("salida"), "entrada": p.get("entrada"), "pnl": p.get("pnl"), "pnl_pct": p.get("pnl_pct"),
                   "motivo": p.get("motivo")})
    desde = st.get("capital_desde") or ""
    ev.sort(key=lambda e: e.get("fecha") or "", reverse=True)
    cer = st.get("cerradas", [])
    return {"desde": desde, "eventos": ev[:200], "n_cerradas": len(cer),
            "ganadoras": sum(1 for p in cer if (p.get("pnl") or 0) > 0),
            "pnl_realizado": round(sum(p.get("pnl") or 0 for p in cer), 2)}


# ------------------------------------------------------------------ historial del valor
def instantanea():
    try:
        d = panel()
        if d.get("error"):
            return
        h = leer(HIST, []) or []
        h.append({"t": d["ahora"], "valor": d["valor"], "efectivo": d["efectivo"]})
        guardar(HIST, h[-4000:])               # ~41 días a 15 min
    except Exception as e:  # noqa: BLE001  (el hilo no debe morir)
        print("snapshot falló:", type(e).__name__, e, flush=True)


def hilo_historial():
    while True:
        h = leer(HIST, []) or []
        if not h or (datetime.now(timezone.utc) - datetime.strptime(h[-1]["t"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)) >= timedelta(seconds=CADA_SNAP - 30):
            instantanea()
        time.sleep(60)


# ------------------------------------------------------------------ control: pausar / reanudar
def poner_modo(modo):
    """Cambia SOLO la clave "modo" de config.json (lectura/real), conservando el resto, de forma atómica."""
    if modo not in MODOS:
        raise ValueError("modo inválido")
    texto = CONFIG.read_text()
    cfg = json.loads(texto)
    previo = cfg.get("modo")
    cfg["modo"] = modo
    tmp = CONFIG.with_name(CONFIG.name + ".panel.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, CONFIG.stat().st_mode & 0o777)
    os.replace(tmp, CONFIG)
    print(f"{ahora_iso()} modo {previo} -> {modo} ({CONFIG})", flush=True)
    return previo


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p, _, q = self.path.partition("?")
        try:
            if p == "/api/panel":
                return self._json(panel())
            if p == "/api/comite":
                return self._json(lista_actas())
            if p.startswith("/api/acta/"):
                a = acta_completa(p.rsplit("/", 1)[1])
                return self._json(a) if a else self._json({"error": "no existe"}, 404)
            if p == "/api/operaciones":
                return self._json(operaciones())
            if p == "/api/historial":
                return self._json(leer(HIST, []))
            if p.startswith("/api/velas/"):
                iid = p.rsplit("/", 1)[1]
                inter = dict(x.split("=", 1) for x in q.split("&") if "=" in x).get("i", "OneHour")
                if not iid.isdigit() or inter not in INTERVALOS:
                    return self._json({"error": "parámetros"}, 400)
                v, _e = etoro_ro.velas(int(iid), inter, 90 if inter == "OneDay" else 120)
                return self._json(v)
        except (OSError, ValueError, KeyError) as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 502)
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/modo":
            return self._json({"error": "no existe"}, 404)
        if self.headers.get("X-Panel") != "1":
            return self._json({"error": "falta cabecera"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            modo = json.loads(self.rfile.read(min(n, 1000)) or b"{}").get("modo")
            previo = poner_modo(modo)
        except (OSError, ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"ok": True, "antes": previo, "modo": modo})


if __name__ == "__main__":
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    threading.Thread(target=hilo_historial, daemon=True).start()
    print(f"Panel eToro en http://0.0.0.0:{puerto}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", puerto), H).serve_forever()
