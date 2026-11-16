"""Ranking de relevancia de las noticias: UNA llamada a `claude -p` (sonnet, esfuerzo bajo, sin herramientas)
o, si falla, un ranking heurístico (fuente, recencia, nº de menciones)."""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CLAUDE = Path.home() / ".local/bin/claude"
MODELO = os.environ.get("INVERSIONES_SEMANAL_MODELO", "sonnet")
ESFUERZO = os.environ.get("INVERSIONES_SEMANAL_ESFUERZO", "low")
TIMEOUT = int(os.environ.get("INVERSIONES_SEMANAL_TIMEOUT", 20 * 60))
RUN_DIR = Path.home() / ".local/state/inversiones/run"

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["noticias", "mundo"],
    "properties": {
        "noticias": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "relevancia", "impacto", "motivo", "simbolos"],
            "properties": {"id": {"type": "string"},
                           "relevancia": {"type": "integer", "minimum": 1, "maximum": 10},
                           "impacto": {"type": "string", "enum": ["positivo", "negativo", "neutro"]},
                           "motivo": {"type": "string", "description": "Una frase (máx. 25 palabras) de por qué importa a una cartera"},
                           "simbolos": {"type": "array", "items": {"type": "string"}}}}},
        "mundo": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["perfil", "resumen"],
            "properties": {"perfil": {"type": "string"},
                           "resumen": {"type": "string", "description": "Un párrafo breve (60-110 palabras) sobre qué pasa en el mundo y cómo afecta a SUS valores"}}}},
    },
}
SISTEMA = """Eres un analista financiero. Hoy es {hoy}. Recibes por la entrada estándar titulares de la última semana (id|fecha|fuente|\
símbolos de origen|titular) y las carteras de varios inversores (solo acciones). Trabaja ÚNICAMENTE con esos datos: no busques \
nada ni inventes hechos que no se desprendan del titular.
Para CADA noticia devuelve: relevancia 1-10 (10 = mueve de forma material el precio o el riesgo de los valores afectados; 1 = ruido, \
reseña de analista genérica, listas "mejores acciones", noticias sin efecto en el precio), impacto probable en los valores afectados \
(positivo/negativo/neutro), una frase de por qué importa y los símbolos afectados (solo de los que aparecen en las carteras; \
las noticias macro afectan a los que corresponda, o a ninguno). Sé exigente: la mayoría de noticias deben quedar entre 1 y 5.
Además, para cada perfil, un párrafo breve en español «qué está pasando en el mundo» que afecta a SUS valores (solo los suyos; \
macro solo si les afecta). Sin recomendaciones de compra/venta. Responde en español."""

FUENTES_FUERTES = ("reuters", "bloomberg", "wall street journal", "wsj", "financial times", "cnbc", "barron", "associated press",
                   "ap news", "marketwatch", "expansión", "cinco días", "the economist", "yahoo finance")
NEG = re.compile(r"\b(fall|falls|drop|drops|plunge|slump|sink|cut|cuts|downgrade|probe|lawsuit|miss|misses|warn|loss|ban|recall|"
                 r"caen?|cae|desplome|recorta|rebaja|multa|pérdidas?)\b", re.I)
POS = re.compile(r"\b(surge|soar|jump|rally|beat|beats|record|upgrade|raises?|approval|wins?|gain|sube|suben|récord|dispara|"
                 r"supera|mejora|beneficios)\b", re.I)


def _lineas(noticias):
    return "\n".join(f"{n['id']}|{n['fecha'][:10]}|{n['fuente']}|{','.join(n['simbolos']) or ('MACRO' if n['macro'] else '-')}|"
                     f"{n['titulo'][:200]}" for n in noticias)


def heuristico(noticias, ahora=None):
    """Puntuación 1-10 sin IA: fuente (0-3), recencia (0-4), menciones (0-3); impacto por palabras clave."""
    ahora = ahora or datetime.now(timezone.utc)
    out = {}
    for n in noticias:
        dias = max(0.0, (ahora - datetime.fromisoformat(n["fecha"])).total_seconds() / 86400)
        fuente = 3 if any(f in n["fuente"].lower() for f in FUENTES_FUERTES) else 1
        pts = fuente + max(0, 4 - dias * 0.6) + min(3, (n["menciones"] - 1) * 1.5)
        neg, pos = bool(NEG.search(n["titulo"])), bool(POS.search(n["titulo"]))
        out[n["id"]] = {"id": n["id"], "relevancia": max(1, min(10, round(pts))),
                        "impacto": "negativo" if neg and not pos else "positivo" if pos and not neg else "neutro",
                        "motivo": "", "simbolos": list(n["simbolos"])}
    return out


def clasificar(noticias, perfiles, hoy, log=print):
    """perfiles = {nombre: [símbolos]}. Devuelve (ranking {id: {...}}, mundo {perfil: texto}, meta) o lanza RuntimeError."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    cartera = "\n".join(f"{p}: {', '.join(s)}" for p, s in perfiles.items())
    entrada = f"CARTERAS:\n{cartera}\n\nNOTICIAS:\n{_lineas(noticias)}\n"
    cmd = [str(CLAUDE), "-p", "Puntúa todas las noticias y redacta el párrafo de cada perfil.",
           "--model", MODELO, "--effort", ESFUERZO, "--tools", "", "--restricted", "--strict-mcp-config",
           "--no-session-persistence", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--system-prompt", SISTEMA.format(hoy=hoy)]
    log(f"ranking: claude -p ({MODELO}, esfuerzo {ESFUERZO}, {len(noticias)} noticias, límite {TIMEOUT // 60} min)")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=RUN_DIR, input=entrada)
    try:
        out = json.loads(res.stdout or "{}")
    except ValueError:
        out = {}
    if res.returncode or out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude falló (rc={res.returncode}): {(res.stdout or res.stderr)[-500:]}")
    so = out["structured_output"]
    ids = {n["id"] for n in noticias}
    todos = {s for ss in perfiles.values() for s in ss}
    rank = {r["id"]: {**r, "simbolos": [s for s in r["simbolos"] if s in todos]} for r in so["noticias"] if r["id"] in ids}
    meta = {"coste_usd": out.get("total_cost_usd", 0), "turnos": out.get("num_turns", 0), "modelo": MODELO,
            "duracion_s": round(out.get("duration_ms", 0) / 1000), "puntuadas": len(rank), "total": len(ids)}
    return rank, {m["perfil"].lower(): m["resumen"] for m in so["mundo"]}, meta
