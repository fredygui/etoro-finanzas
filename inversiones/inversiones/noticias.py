"""Noticias gratuitas por RSS (Google News y Yahoo Finance) de los últimos 7 días: por valor y contexto macro.

Sin claude ni navegación. Se descargan una sola vez los valores que comparten varios perfiles y se deduplican por titular.
"""
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept-Encoding": "identity"}
GOOGLE = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={s}&region=US&lang=en-US"
MAX_VALOR = 8        # noticias por valor que se conservan
MAX_MACRO = 15       # noticias macro que se conservan
MACRO = [            # (consulta, idioma)
    ("Federal Reserve interest rates inflation", "en"),
    ("ECB Banco Central Europeo tipos de interés", "es"),
    ("stock market Wall Street week S&P 500", "en"),
    ("oil prices OPEC crude", "en"),
    ("geopolitics tariffs trade war markets", "en"),
    ("US economy jobs report recession", "en"),
    ("bolsa mercados inflación economía mundial", "es"),
]
SUFIJOS = re.compile(r"\b(inc|inc\.|corp|corporation|ltd|plc|co|group|holdings?|company|class [a-c]|adr|ag|se|sa|nv|c)\b\.?", re.I)


def _descargar(url, intentos=2):
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return r.read()
        except OSError:
            if i == intentos - 1:
                raise
            time.sleep(2)


def _rss(xml, fuente_defecto):
    """Elementos del RSS: título (sin « - fuente»), fuente, fecha (UTC) y enlace."""
    out = []
    for it in ET.fromstring(xml).iter("item"):
        titulo = (it.findtext("title") or "").strip()
        fuente = (it.findtext("source") or "").strip()
        if not fuente and " - " in titulo:
            titulo, fuente = titulo.rsplit(" - ", 1)
        elif fuente and titulo.endswith(" - " + fuente):
            titulo = titulo[: -len(fuente) - 3]
        try:
            fecha = parsedate_to_datetime(it.findtext("pubDate"))
            fecha = fecha.astimezone(timezone.utc) if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if titulo:
            out.append({"titulo": titulo, "fuente": fuente or fuente_defecto, "fecha": fecha,
                        "enlace": (it.findtext("link") or "").strip()})
    return out


def limpiar_nombre(nombre):
    n = SUFIJOS.sub(" ", nombre.replace("-", " "))
    return re.sub(r"\s+", " ", n).strip() or nombre


def _google(q, idioma="en"):
    hl, gl, ceid = ("es", "ES", "ES:es") if idioma == "es" else ("en-US", "US", "US:en")
    url = GOOGLE.format(q=urllib.parse.quote(f"{q} when:7d"), hl=hl, gl=gl, ceid=ceid)
    return _rss(_descargar(url), "Google News")


def _valor(simbolo, nombre):
    items = []
    q = f'"{limpiar_nombre(nombre)}" stock' if nombre else f"{simbolo} stock"
    items += _google(q)[:10]
    if "." not in simbolo:      # Yahoo solo cubre bien los tickers de EE. UU.
        try:
            claves = {simbolo.lower(), (limpiar_nombre(nombre).split() or [""])[0].lower()} - {""}
            # el feed de Yahoo mezcla noticias de otras empresas: solo las que citan el ticker o el nombre
            items += [i for i in _rss(_descargar(YAHOO.format(s=urllib.parse.quote(simbolo))), "Yahoo Finance")
                      if any(re.search(rf"\b{re.escape(c)}\b", i["titulo"].lower()) for c in claves)][:5]
        except (OSError, ET.ParseError):
            pass
    return items


def _clave(titulo):
    return re.sub(r"[^a-z0-9]+", "", titulo.lower())[:70]


def descargar(valores, hoy=None):
    """valores = {simbolo: nombre}. Devuelve (lista global de noticias deduplicadas, avisos).
    Cada noticia: id, titulo, fuente, fecha (ISO UTC), enlace, simbolos (origen), macro (bool), menciones."""
    desde = (hoy or datetime.now(timezone.utc)) - timedelta(days=7)
    avisos, tareas = [], [("v", s, lambda s=s, n=n: _valor(s, n)) for s, n in sorted(valores.items())]
    tareas += [("m", q, lambda q=q, i=i: _google(q, i)) for q, i in MACRO]

    def una(t):
        try:
            return t, t[2]()
        except (OSError, ET.ParseError) as e:
            return t, e

    por_clave, ordenadas = {}, []
    with ThreadPoolExecutor(6) as ex:
        resultados = list(ex.map(una, tareas))
    macro_n = {}
    for (tipo, nombre, _), items in resultados:
        if isinstance(items, Exception):
            avisos.append(f"noticias: sin RSS para «{nombre}» ({type(items).__name__})")
            continue
        items = sorted((i for i in items if i["fecha"] >= desde), key=lambda i: i["fecha"], reverse=True)
        items = items[:MAX_VALOR] if tipo == "v" else items[:6]
        for it in items:
            k = _clave(it["titulo"])
            n = por_clave.get(k)
            if n is None:
                n = por_clave[k] = {"titulo": it["titulo"], "fuente": it["fuente"], "fecha": it["fecha"].isoformat(timespec="minutes"),
                                    "enlace": it["enlace"], "simbolos": [], "macro": False, "menciones": 0}
                ordenadas.append(n)
            n["menciones"] += 1
            if tipo == "v" and nombre not in n["simbolos"]:
                n["simbolos"].append(nombre)
            if tipo == "m":
                n["macro"] = True
    # recorte: máx. MAX_VALOR por símbolo de origen y MAX_MACRO macro (las más recientes / más mencionadas)
    ordenadas.sort(key=lambda n: (n["menciones"], n["fecha"]), reverse=True)
    cuenta, macro_c, final = {}, 0, []
    for n in ordenadas:
        if n["simbolos"]:
            if all(cuenta.get(s, 0) >= MAX_VALOR for s in n["simbolos"]):
                continue
            for s in n["simbolos"]:
                cuenta[s] = cuenta.get(s, 0) + 1
        elif n["macro"]:
            if macro_c >= MAX_MACRO:
                continue
            macro_c += 1
        final.append(n)
    final.sort(key=lambda n: n["fecha"], reverse=True)
    for i, n in enumerate(final, 1):
        n["id"] = f"n{i}"
    return final, avisos
