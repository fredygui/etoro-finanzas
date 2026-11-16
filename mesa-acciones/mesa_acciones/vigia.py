"""VIGÍA — proceso permanente sin Claude: vigila precios de eToro y convoca la mesa cuando hay motivo.

Solo con la bolsa del instrumento en sesión (horario.py, con margen de apertura/cierre). Cada `vigia.cada_segundos`
pide en UNA llamada (market-data/rates) los bid/ask del universo de las regiones abiertas y de las posiciones, descarta
cotizaciones viejas (> `precio_max_edad_s`) y comprueba:
1. Universo (automático): sube o baja `auto_pct` % en ≤ `auto_minutos` (enfriamiento `auto_enfriamiento_min`).
2. Alertas manuales de data/vigia.json (sube/baja % en N min, persistentes; encima/debajo de un precio, de un uso).
3. Posiciones con plan: bid a ≤ `pos_cerca_pct` % del stop o del objetivo, o movimiento ≥ `pos_mov_pct` % desde la
   última reunión (enfriamiento `pos_enfriamiento_min`).
Si salta algo lanza `python -m mesa_acciones alerta SIM… --motivo …` (respeta el cupo diario de comités y la cuota de
Claude; una reunión a la vez; lo que salta entretanto va agrupado a la siguiente).
"""
import subprocess
import sys
import time
from collections import deque

from . import broker, etoro, horario, mercado, universo
from .comun import DATA, ESTADO, RAIZ, ahora, cfg, de_iso, guardar, iso, leer, log

ALERTAS = DATA / "vigia.json"
DEF = {"cada_segundos": 60, "minutos": 60, "enfriamiento_min": 240, "universo_auto": True,
       "auto_pct": 3.0, "auto_minutos": 60, "auto_enfriamiento_min": 240, "precio_max_edad_s": 600,
       "pos_cerca_pct": 1.0, "pos_mov_pct": 4.0, "pos_enfriamiento_min": 120}


def _conf(c=None):
    return {**DEF, **(c or cfg()).get("vigia", {})}


def efectivas(manuales, univ, conf, ultimo):
    al = {}
    if conf["universo_auto"]:
        for sim in univ:
            al[sim] = {"sube": conf["auto_pct"], "baja": conf["auto_pct"], "minutos": conf["auto_minutos"],
                       "enfriamiento": conf["auto_enfriamiento_min"], "auto": True}
    for sim, a in manuales.items():
        al[sim] = {**al.get(sim, {}), **a}
    for sim, a in al.items():
        if ultimo.get(sim) and (not a.get("disparada") or ultimo[sim] > a["disparada"]):
            a["disparada"] = ultimo[sim]
    return al


def comprobar(alertas, hist, px, t, conf):
    """[(sim, motivo, clave)]; clave = 'encima'/'debajo' (de un uso) o None (sube/baja, con enfriamiento)."""
    salta = []
    for sim, a in alertas.items():
        p = px.get(sim)
        if p is None:
            continue
        ventana = a.get("minutos", conf["minutos"]) * 60
        h = hist.setdefault(sim, deque())
        h.append((t, p))
        while t - h[0][0] > ventana:
            h.popleft()
        if a.get("encima") and p >= a["encima"]:
            salta.append((sim, f"{sim} {p:.6g} ≥ {a['encima']:.6g}", "encima"))
        if a.get("debajo") and p <= a["debajo"]:
            salta.append((sim, f"{sim} {p:.6g} ≤ {a['debajo']:.6g}", "debajo"))
        enfr = a.get("disparada")
        if enfr and (ahora() - de_iso(enfr)).total_seconds() < a.get("enfriamiento", conf["enfriamiento_min"]) * 60:
            continue
        mn, mx, mins = min(x for _, x in h), max(x for _, x in h), round(ventana / 60)
        if a.get("sube") and p >= mn * (1 + a["sube"] / 100):
            salta.append((sim, f"{sim} +{(p / mn - 1) * 100:.2f} % en ≤{mins} min ({mn:.6g} → {p:.6g})", None))
        elif a.get("baja") and p <= mx * (1 - a["baja"] / 100):
            salta.append((sim, f"{sim} {(p / mx - 1) * 100:.2f} % en ≤{mins} min ({mx:.6g} → {p:.6g})", None))
        else:
            continue
        h.clear()
    return salta


def posiciones():
    return [p for p in leer(broker.ESTADO_ET, {}).get("abiertas", [])
            if p.get("estado") == "abierta" and p.get("stop") and p.get("objetivo") and p.get("plan_comite")]


def comprobar_pos(pos, px, ref, ultimo, conf):
    salta = []
    for p in pos:
        sim, b = p["simbolo"], px.get(p["simbolo"])
        if b is None:
            continue
        ref.setdefault(sim, b)
        u = ultimo.get(sim)
        if u and (ahora() - de_iso(u)).total_seconds() < conf["pos_enfriamiento_min"] * 60:
            continue
        cerca, mov = conf["pos_cerca_pct"] / 100, (b / ref[sim] - 1) * 100
        if b <= p["stop"] * (1 + cerca):
            salta.append((sim, f"posición {sim} {b:.6g} cerca del stop {p['stop']:.6g}"))
        elif b >= p["objetivo"] * (1 - cerca):
            salta.append((sim, f"posición {sim} {b:.6g} cerca del objetivo {p['objetivo']:.6g}"))
        elif abs(mov) >= conf["pos_mov_pct"]:
            salta.append((sim, f"posición {sim} {mov:+.2f} % desde la última reunión ({ref[sim]:.6g} → {b:.6g})"))
    return salta


def anotar(salta):
    alertas = leer(ALERTAS, {})
    for sim, _, clave in salta:
        a = alertas.get(sim)
        if a is None:
            continue
        if clave:
            a.pop(clave, None)
        else:
            a["disparada"] = iso()
        if not any(a.get(k) for k in ("sube", "baja", "encima", "debajo")):
            alertas.pop(sim)
    guardar(ALERTAS, alertas)


def convocar(sims, motivo):
    return subprocess.Popen([sys.executable, "-m", "mesa_acciones", "alerta", *sims, "--motivo", motivo], cwd=RAIZ,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def vigilar(api=None, dormir=time.sleep, vueltas=None, convocar_fn=None):
    """Bucle principal. `api`, `dormir`, `vueltas` y `convocar_fn` permiten probarlo con simuladores."""
    c = cfg()
    conf, h = _conf(c), c["horario"]
    api = api or etoro.api
    convocar_fn = convocar_fn or convocar
    hist, ultimo, ref, pendientes = {}, {}, {}, {}
    proc, fallos, ult_comite, n = None, 0, None, 0
    log(f"vigía: arranco (cada {conf['cada_segundos']} s; universo ±{conf['auto_pct']:g} % en {conf['auto_minutos']:g} min)")
    while vueltas is None or n < vueltas:
        n += 1
        t = time.time()
        abiertas = {r for r in ("EEUU", "UE") if horario.abierta_region(r, None, h["margen_apertura_min"], h["margen_cierre_min"])}
        if not abiertas:
            hist.clear()
            dormir(300)
            continue
        univ = {i["simbolo"]: i for i in universo.cargar() if i.get("region") in abiertas}
        pos = [p for p in posiciones() if p.get("region") in abiertas]
        manuales = leer(ALERTAS, {})
        ids = {i["simbolo"]: i["id"] for i in univ.values()}
        ids.update({p["simbolo"]: p["instrumento"] for p in pos})
        for sim in manuales:
            if sim not in ids and sim in universo.por_simbolo():
                i = universo.por_simbolo()[sim]
                if i.get("region") in abiertas:
                    ids[sim] = i["id"]
        alertas = efectivas({s: a for s, a in manuales.items() if s in ids}, univ if conf["universo_auto"] else {},
                            conf, ultimo)
        uc = leer(ESTADO, {}).get("ultimo_comite")
        if uc != ult_comite:
            ref, ult_comite = {}, uc
        try:
            raw, fallos = etoro.precios(ids.values(), f=api), 0
        except (etoro.ErrorAPI, OSError, ValueError, KeyError) as e:
            raw, fallos = {}, fallos + 1
            if fallos in (1, 20, 200):
                log(f"vigía: sin precios de eToro ({fallos} seguidos; {str(e)[:120]})")
        inv = {v: k for k, v in ids.items()}
        px = {}
        for iid, q in raw.items():
            edad = mercado.edad_precio_s(q, t)
            if iid in inv and (edad is None or edad <= conf["precio_max_edad_s"]):
                px[inv[iid]] = q["bid"]
        salta = comprobar(alertas, hist, px, t, conf)
        if any(s in manuales for s, _, _ in salta):
            anotar([x for x in salta if x[0] in manuales])
        for sim, motivo in [(s, m) for s, m, _ in salta] + comprobar_pos(pos, px, ref, ultimo, conf):
            log(f"vigía: alerta {motivo}")
            ultimo[sim] = iso()
            pendientes[sim] = motivo if sim not in pendientes else f"{pendientes[sim]}; {motivo}"
        if proc and proc.poll() is not None:
            proc = None
        if pendientes and not proc:
            proc, pendientes = convocar_fn(list(pendientes), " · ".join(pendientes.values())), {}
        dormir(conf["cada_segundos"])


def cli(args):
    """vigia [lista] | vigia SIM [--sube %] [--baja %] [--en min] [--encima P] [--debajo P] | vigia quitar SIM…"""
    alertas = leer(ALERTAS, {})
    if not args or args[0] == "lista":
        conf = _conf()
        print(f"Automático: universo líquido ±{conf['auto_pct']:g} % en {conf['auto_minutos']:g} min · posiciones a "
              f"≤{conf['pos_cerca_pct']:g} % del stop/objetivo o ±{conf['pos_mov_pct']:g} % desde la última reunión")
        print("Manuales: " + (", ".join(f"{s} {a}" for s, a in sorted(alertas.items())) or "ninguna"))
        return
    if args[0] == "quitar":
        for s in args[1:]:
            print(f"{s.upper()}: {'quitada' if alertas.pop(s.upper(), None) is not None else 'no había alerta'}")
        return guardar(ALERTAS, alertas)
    sim, opc = args[0].upper(), args[1:]
    claves = {"--sube": "sube", "--baja": "baja", "--en": "minutos", "--encima": "encima", "--debajo": "debajo",
              "--enfriamiento": "enfriamiento"}
    if len(opc) % 2 or any(o not in claves for o in opc[::2]):
        sys.exit(f"uso: {cli.__doc__}")
    if sim not in universo.por_simbolo() and sim not in {p["simbolo"] for p in posiciones()}:
        sys.exit(f"{sim} no está en el universo ni en la cartera")
    alertas[sim] = {**alertas.get(sim, {}), **{claves[o]: float(v) for o, v in zip(opc[::2], opc[1::2])}}
    guardar(ALERTAS, alertas)
    print(f"{sim}: alerta guardada")
