"""Informe semanal de riesgo y noticias (lunes 06:00, lo lanza n8n): uno por perfil, solo con SUS valores.

python -m inversiones.semanal [--perfil perfil2] [--prueba] [--sin-claude] [--solo-titular]
  --prueba        todo a ~/.local/state/inversiones/prueba/semanal/ y sin enviar
  --sin-claude    ranking heurístico (sin IA, coste 0)
  --solo-titular  envía los correos de los perfiles elegidos, pero todos solo a la lista de errores (el titular), con el perfil en el asunto
Código de salida: 0 ok, 2 con avisos (ranking heurístico, API caída...), 1 si falló algún perfil.
"""
import argparse
import html
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import calendario, cartera as cartera_mod, etoro, informe, noticias, orquestar, perfiles, ranking, riesgo

ESTADO = perfiles.ESTADO / "semanal"
PRUEBA = perfiles.ESTADO / "prueba/semanal"
SEMAFORO = {"verde": "🟢 Verde", "ambar": "🟡 Ámbar", "rojo": "🔴 Rojo"}
ICONO = {"positivo": "▲ positivo", "negativo": "▼ negativo", "neutro": "● neutro"}
MAX_NOTICIAS, MAX_MACRO_MOSTRADAS = 12, 5


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- posiciones

def posiciones(pf, cat):
    """Posiciones agregadas por símbolo desde la API de eToro (solo lectura, sin tocar el fichero de cartera).
    Si la API falla usa la última cartera guardada. Devuelve (lista, efectivo_usd, aviso|None)."""
    idx = {v["id"]: v for v in cat.values()}
    lotes, efectivo, aviso = [], None, None
    try:
        cp = etoro.cuenta(pf.claves)
        for p in cp["positions"]:
            if not p.get("isBuy", True):
                continue
            ins, entrada = idx.get(p["instrumentID"], {}), p["openRate"]
            sl = p.get("stopLossRate") or 0
            lotes.append({"simbolo": ins.get("simbolo", str(p["instrumentID"])), "id": p["instrumentID"], "nombre": ins.get("nombre", ""),
                          "unidades": p["units"], "importe_usd": p["amount"], "apalancamiento": p["leverage"],
                          "sl": sl if entrada and sl > 0.05 * entrada else None})
        efectivo = cp.get("credit")
    except (OSError, ValueError, KeyError) as e:
        aviso = (f"no se pudo leer la cuenta de eToro ({type(e).__name__}); se usan las posiciones de la última "
                 f"sincronización ({pf.cartera.name})")
        cart = cartera_mod.cargar(pf)
        lotes = [{k: l.get(k) for k in ("simbolo", "id", "nombre", "unidades", "importe_usd", "apalancamiento", "sl")}
                 for l in cart["posiciones"]]
        efectivo = cart.get("efectivo_usd")
        if cart.get("sincronizado"):
            aviso += f", del {cart['sincronizado'][:10]}"
    por = {}
    for l in lotes:
        p = por.setdefault(l["simbolo"], {"simbolo": l["simbolo"], "id": l["id"], "nombre": l["nombre"], "unidades": 0.0,
                                         "importe_usd": 0.0, "apalancado": False, "sls": []})
        p["unidades"] += l["unidades"] or 0
        p["importe_usd"] += l["importe_usd"] or 0
        p["apalancado"] |= (l["apalancamiento"] or 1) > 1
        if l["sl"]:
            p["sls"].append(l["sl"])
    return list(por.values()), efectivo, aviso


def valorar(pos, series):
    """Añade valor actual, peso (%) y distancia al SL (%) a cada posición."""
    for p in pos:
        v = series.get(p["simbolo"]) or []
        p["precio"] = v[-1][1] if v else None
        p["valor_usd"] = p["unidades"] * p["precio"] if p["precio"] else p["importe_usd"]
        p["dist_sl"] = round((p["precio"] - max(p["sls"])) / p["precio"] * 100, 1) if p["precio"] and p["sls"] else None
    total = sum(p["valor_usd"] for p in pos) or 1
    for p in pos:
        p["peso"] = round(p["valor_usd"] / total * 100, 1)
    pos.sort(key=lambda p: p["peso"], reverse=True)


# ---------------------------------------------------------------- noticias por perfil

def noticias_perfil(simbolos, todas, rank, ahora):
    """Noticias que afectan a los valores del perfil (más macro), con puntuación; ordenadas por relevancia."""
    s = set(simbolos)
    out = []
    for n in todas:
        r = rank.get(n["id"])
        afecta = [x for x in (r["simbolos"] if r else []) if x in s] or [x for x in n["simbolos"] if x in s]
        if not afecta and not n["macro"]:
            continue
        if n["macro"] and not n["simbolos"] and r and not r["simbolos"] and not r.get("motivo"):
            pass
        out.append({**n, "relevancia": r["relevancia"] if r else 1, "impacto": r["impacto"] if r else "neutro",
                    "motivo": r.get("motivo", "") if r else "", "afecta": afecta})
    out.sort(key=lambda n: (n["relevancia"], n["fecha"]), reverse=True)
    top, macro = [], 0
    for n in out:
        if not n["afecta"]:
            if macro >= MAX_MACRO_MOSTRADAS:
                continue
            macro += 1
        top.append(n)
        if len(top) >= MAX_NOTICIAS:
            break
    return top


# ---------------------------------------------------------------- composición

def _e(x):
    return html.escape(str(x), quote=False)


def _md(x):
    return re.sub(r"([\[\]|])", r"\\\1", str(x))


def _n(x, suf="", nd=1):
    return "–" if x is None else f"{x:.{nd}f}{suf}"


def componer(d):
    """Markdown del informe (nota de Obsidian y base del correo)."""
    pf, mkt, rs = d["perfil"], d["mercado"], d["riesgo"]
    L = [f"# Informe semanal {d['semana']} — {pf['etiqueta']}", "",
         f"_Generado el {d['generado'][:16].replace('T', ' ')} · ranking: {d['ranking_origen']}_", ""]
    for a in d["avisos"]:
        L.append(f"> [!warning] {_md(a)}")
    L += ["", "## Resumen y semáforo de mercado", "", d["mundo"], ""]
    v, s = mkt["vix"], mkt["semana"]
    L += [f"**Semáforo global: {SEMAFORO[mkt['semaforo']]}**" + (f" — {'; '.join(mkt['motivos'])}" if mkt["motivos"] else ""), "",
          "| Indicador | Valor |", "|---|---|",
          f"| VIX | {_n(v.get('valor'))} (hace una semana {_n(v.get('hace_1s'))}) · {v.get('fuente', 'sin dato')} |",
          f"| SPY frente a SMA200 | {_n(mkt['spy'], '', 2)} vs {_n(mkt['sma200'], '', 2)} ({_n(mkt['spy_vs_sma200'], ' %')}) |",
          f"| Semana: SPY / petróleo / oro / BTC | {_n(s['SPY'], ' %')} / {_n(s['petroleo'], ' %')} / {_n(s['oro'], ' %')} / {_n(s['btc'], ' %')} |",
          f"| Cartera (rentab. ponderada) 1 sem / 1 mes | {_n(rs['r1s'], ' %')} / {_n(rs['r1m'], ' %')} |",
          f"| Volatilidad de la cartera (30 d, anual.) | {_n(rs.get('vol30'), ' %')} |",
          f"| Beta frente al SPY (ponderada) | {_n(rs['beta'], '', 2)} |",
          f"| Concentración | máx. {rs['simbolo_max']} {_n(rs['peso_max'], ' %')} · HHI {_n(rs['hhi'], '', 3)} · {rs['n_valores']} valores |", ""]
    L += ["## Alertas", ""]
    L += [f"- {_md(a)}" for a in d["alertas"]] or ["- Sin alertas esta semana."]
    L += ["", "## Ranking de noticias (más relevantes primero)", ""]
    if d["noticias"]:
        L += ["| Rel. | Impacto | Noticia | Afecta | Por qué importa |", "|---|---|---|---|---|"]
        for n in d["noticias"]:
            tit = f"[{_md(n['titulo'])}]({n['enlace']})" if n["enlace"] else _md(n["titulo"])
            L.append(f"| **{n['relevancia']}** | {ICONO[n['impacto']]} | {tit}<br>{_md(n['fuente'])} · {n['fecha'][:10]} | "
                     f"{', '.join(n['afecta']) or 'mercado'} | {_md(n['motivo'])} |")
    else:
        L.append("Sin noticias relevantes esta semana.")
    L += ["", "## Riesgo por valor", "",
          "| Valor | Peso | Precio | 1 sem | 1 mes | Vol. 30d | Caída vs máx. 52s | Beta | Corr. SPY | Dist. SL |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for p in d["posiciones"]:
        m = p["metricas"]
        L.append(f"| **{p['simbolo']}** {_md(p['nombre'])} | {p['peso']:.1f} % | {_n(m.get('precio'), '', 2)} | {_n(m.get('r1s'), ' %')} | "
                 f"{_n(m.get('r1m'), ' %')} | {_n(m.get('vol30'), ' %')} | {_n(m.get('caida52'), ' %')} | {_n(m.get('beta'), '', 2)} | "
                 f"{_n(m.get('corr'), '', 2)} | {_n(p.get('dist_sl'), ' %')} |")
    L += ["", "## Resultados de empresas esta semana", ""]
    L += [f"- **{k}** — {r['fecha']} {r['hora']}" + (f" (BPA previsto {r['eps_previsto']})" if r["eps_previsto"] else "")
          for k, r in sorted(d["resultados"].items(), key=lambda kv: kv[1]["fecha"])] or ["- Ninguna de tus empresas presenta resultados en los próximos 7 días (calendario Nasdaq, solo EE. UU.)."]
    L += ["", f"> [!warning] {informe.AVISO}", ""]
    return "\n".join(L)


def pie(pf):
    base = ('<p style="color:#71717a;font-size:12px">Informe semanal automático (lunes 06:00): posiciones de tu cuenta de eToro, '
            'noticias de RSS públicos (Google News, Yahoo Finance) e indicadores calculados con velas de eToro; el ranking de relevancia '
            'lo hace una IA (o una heurística si falla). No es asesoramiento financiero.')
    if pf["nombre"] == "titular":
        base += f'<br>Nota en Obsidian: Inversiones/Semanal/{{semana}}.md · <a href="{orquestar.WEB}/Semanal">ver en SilverBullet (LAN)</a>'
    return base + "</p>"


def guardar(d, pf, destino):
    """Nota de Obsidian, JSON y HTML. destino = raíz de salida (None = rutas reales)."""
    semana = d["semana"]
    md = componer(d)
    cuerpo = informe.a_html(md) + pie(d["perfil"]).replace("{semana}", semana)
    if destino:
        notas, js = Path(destino) / pf.nombre / "notas", Path(destino) / pf.nombre / "json"
    else:
        notas, js = pf.notas, ESTADO / pf.nombre
    nota = notas / "Semanal" / f"{semana}.md"
    nota.parent.mkdir(parents=True, exist_ok=True)
    front = (f"---\ntags: [inversiones, semanal]\nperfil: {pf.nombre}\nsemana: {semana}\nfecha: {d['generado'][:10]}\n"
             f"semaforo: {d['mercado']['semaforo']}\n---\n\n")
    enlaces = f"Proyecto: [[inversiones]] · [[eToro]] · [[{pf.pref}/Cartera|Cartera]] · [[{pf.pref}/Índice|Índice]]\n\n"
    nota.write_text(front + enlaces + md, encoding="utf-8")
    if not destino:  # el índice del perfil lista también los semanales
        informe._indice(pf)
    js.mkdir(parents=True, exist_ok=True)
    (js / f"{semana}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (js / f"{semana}.html").write_text(cuerpo, encoding="utf-8")
    return nota, cuerpo


def asunto(d, pf, solo_titular):
    emoji = {"verde": "🟢", "ambar": "🟡", "rojo": "🔴"}[d["mercado"]["semaforo"]]
    a = f"Inversiones · semanal {d['semana']} {emoji}: {len(d['alertas'])} alertas, {len(d['noticias'])} noticias"
    return a + (f" [{pf.etiqueta}]" if solo_titular else "")


# ---------------------------------------------------------------- principal

def parser():
    ap = argparse.ArgumentParser(prog="python -m inversiones.semanal", description=__doc__.split("\n")[0])
    ap.add_argument("--perfil", action="append", help="perfil(es): titular, perfil2, perfil3 (repetible o con comas); por defecto todos")
    ap.add_argument("--prueba", action="store_true", help="todo a ~/.local/state/inversiones/prueba/semanal/ y sin enviar")
    ap.add_argument("--sin-claude", action="store_true", help="ranking heurístico, sin IA")
    ap.add_argument("--solo-titular", action="store_true", help="envía solo al titular, con el perfil en el asunto")
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    lista = perfiles.elegir(a.perfil)
    t0, hoy = time.time(), date.today()
    ahora = datetime.now(timezone.utc)
    generado = datetime.now().astimezone().isoformat(timespec="seconds")
    semana = f"{hoy.isocalendar().year}-W{hoy.isocalendar().week:02d}"
    destino = PRUEBA if a.prueba else None
    log(f"semanal {semana}: perfiles {', '.join(p.nombre for p in lista)}" + (" [prueba]" if a.prueba else "")
        + (" [solo titular]" if a.solo_titular else "") + (" [sin claude]" if a.sin_claude else ""))
    cat = etoro.catalogo()
    por_sim = etoro.por_simbolo(cat)

    # 1. posiciones
    fallos, avisos_g, datos = {}, [], {}
    for pf in lista:
        try:
            pos, efectivo, aviso = posiciones(pf, cat)
            if not pos:
                raise RuntimeError("sin posiciones")
            datos[pf.nombre] = {"pf": pf, "pos": pos, "efectivo": efectivo, "avisos": [aviso] if aviso else []}
            log(f"{pf.nombre}: {len(pos)} valores" + (f" (aviso: {aviso})" if aviso else ""))
        except Exception as e:  # noqa: BLE001
            log(f"{pf.nombre}: posiciones fallaron: {e!r}")
            fallos[pf.nombre] = f"posiciones: {type(e).__name__}: {e}"
    if not datos:
        return _fin(a, fallos, avisos_g, 1)

    # 2. velas (una vez por símbolo) y noticias (una vez por símbolo)
    sims = {p["simbolo"]: p["nombre"] for d in datos.values() for p in d["pos"]}
    ids = {p["simbolo"]: p["id"] for d in datos.values() for p in d["pos"]}
    refs = {k: por_sim[s]["id"] for k, s in riesgo.REFERENCIAS.items() if s in por_sim}
    series_ids = {**ids, **refs}
    velas = etoro.velas_varios(sorted(set(series_ids.values())), n=400)
    series = {k: velas.get(i) or [] for k, i in series_ids.items()}
    series.setdefault("SPY", [])
    if not series["SPY"]:
        avisos_g.append("sin velas del SPY: beta, correlación y semáforo incompletos")
    todas, av_n = noticias.descargar(sims)
    avisos_g += av_n
    log(f"noticias: {len(todas)} únicas ({sum(1 for n in todas if n['macro'])} macro) para {len(sims)} valores")
    if not todas:
        avisos_g.append("sin noticias: fallaron todos los RSS")

    # 3. mercado y resultados
    mkt = riesgo.mercado(series, cat)
    resultados, falt = calendario.resultados_rango(hoy, hoy + timedelta(days=6))
    if falt:
        avisos_g.append(f"calendario de resultados incompleto ({len(falt)} días sin datos)")

    # 4. ranking: una sola llamada a claude para todos los perfiles
    rank, mundo, meta_ia, origen = {}, {}, None, "heurístico (sin IA)"
    heur = ranking.heuristico(todas, ahora)
    if not a.sin_claude and todas:
        for intento in (1, 2):
            try:
                t1 = time.time()
                rank, mundo, meta_ia = ranking.clasificar(
                    todas, {n: [p["simbolo"] for p in d["pos"]] for n, d in datos.items()}, hoy.isoformat(), log=log)
                meta_ia["duracion_s"] = round(time.time() - t1)
                origen = f"IA ({meta_ia['modelo']}, {meta_ia['puntuadas']}/{meta_ia['total']} puntuadas, ${meta_ia['coste_usd']:.3f})"
                log(f"ranking listo: {meta_ia}")
                break
            except Exception as e:  # noqa: BLE001
                log(f"ranking con claude falló (intento {intento}): {e}")
                rank, mundo, meta_ia = {}, {}, None
        if meta_ia is None:
            avisos_g.append("claude no respondió: ranking heurístico (fuente, recencia, menciones)")
    for n in todas:           # lo no puntuado por la IA cae a la heurística
        if n["id"] not in rank:
            rank[n["id"]] = heur[n["id"]]
    if a.sin_claude:
        origen = "heurístico (--sin-claude)"

    # 5. informe por perfil
    rcs = 0
    for nombre, dd in datos.items():
        pf, pos = dd["pf"], dd["pos"]
        try:
            valorar(pos, series)
            mets = {p["simbolo"]: riesgo.metricas(series.get(p["simbolo"]) or [], series["SPY"]) for p in pos}
            for p in pos:
                p["metricas"] = mets[p["simbolo"]]
                if not p["metricas"]:
                    dd["avisos"].append(f"{p['simbolo']}: sin velas suficientes en eToro, sin indicadores")
            rs = riesgo.cartera(pos, mets, series)
            res = {p["simbolo"]: resultados[p["simbolo"].upper()] for p in pos if p["simbolo"].upper() in resultados}
            nots = noticias_perfil([p["simbolo"] for p in pos], todas, rank, ahora)
            al = riesgo.alertas(pos, mets, res, mkt, rs)
            texto = mundo.get(nombre) or _resumen_heuristico(nots)
            d = {"semana": semana, "generado": generado, "perfil": {"nombre": nombre, "etiqueta": pf.etiqueta},
                 "avisos": dd["avisos"] + avisos_g, "mercado": mkt, "riesgo": rs, "alertas": al, "noticias": nots,
                 "posiciones": [{k: v for k, v in p.items() if k not in ("sls",)} for p in pos], "resultados": res,
                 "mundo": texto, "ranking_origen": origen, "efectivo_usd": dd["efectivo"], "ia": meta_ia}
            nota, cuerpo = guardar(d, pf, destino)
            log(f"{nombre}: nota {nota} · {len(nots)} noticias · {len(al)} alertas")
            dd["d"], dd["cuerpo"] = d, cuerpo
            if d["avisos"]:
                rcs = max(rcs, 2)
        except Exception as e:  # noqa: BLE001
            log(f"{nombre}: el informe falló: {e!r}")
            fallos[nombre] = f"informe: {type(e).__name__}: {e}"

    # 6. envío
    if not a.prueba:
        for nombre, dd in datos.items():
            if "d" not in dd:
                continue
            try:
                dest = orquestar.destinatarios("error") if a.solo_titular else orquestar.destinatarios_perfil(nombre)
                for p in dest:
                    orquestar.enviar(p, asunto(dd["d"], dd["pf"], a.solo_titular), dd["cuerpo"], forzar_correo=True)
                log(f"{nombre}: enviado a {', '.join(dest)}")
            except Exception as e:  # noqa: BLE001
                log(f"{nombre}: el envío falló: {e!r}")
                fallos[nombre] = f"envío: {type(e).__name__}: {e}"
        avisos_perfil = [f"{dd['pf'].etiqueta}: {x}" for dd in datos.values() for x in dd["avisos"]]
        if (avisos_g or avisos_perfil) and not a.solo_titular:
            try:
                orquestar.enviar_aviso(avisos_g + avisos_perfil, "Inversiones: avisos del informe semanal")
            except Exception as e:  # noqa: BLE001
                log(f"no se pudo enviar el aviso: {e!r}")
    log(f"fin en {(time.time() - t0) / 60:.1f} min")
    return _fin(a, fallos, avisos_g, rcs)


def _resumen_heuristico(nots):
    if not nots:
        return "Sin noticias destacadas en los últimos 7 días para tus valores."
    return "Titulares más relevantes de la semana (ranking heurístico, sin resumen de IA): " + "; ".join(n["titulo"] for n in nots[:3]) + "."


def _fin(a, fallos, avisos, rc):
    if fallos:
        log("fallos: " + json.dumps(fallos, ensure_ascii=False))
        if not a.prueba:
            try:
                cuerpo = "<h3>Inversiones: el informe semanal ha fallado</h3><ul>" + "".join(
                    f"<li><b>{_e(k)}</b>: {_e(v)}</li>" for k, v in fallos.items()) + "</ul>"
                for p in orquestar.destinatarios("error"):
                    orquestar.enviar(p, "Inversiones: el informe semanal ha fallado", cuerpo)
            except Exception as e:  # noqa: BLE001
                log(f"no se pudo enviar el error: {e!r}")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
