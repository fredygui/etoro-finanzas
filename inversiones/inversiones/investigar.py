"""Investigación con `claude -p`: búsqueda web y subagentes en paralelo sobre los datos del cribado.

Sin acceso a ficheros ni comandos (modo --restricted, solo WebSearch/WebFetch/Agent); la salida se valida con
--json-schema y después se comprueba en Python que cada símbolo exista en eToro.
"""
import json
import os
import subprocess
from pathlib import Path

CLAUDE = Path.home() / ".local/bin/claude"
MODELO = os.environ.get("INVERSIONES_MODELO", "opus")            # síntesis y decisión final
SUBMODELO = os.environ.get("INVERSIONES_SUBMODELO", "sonnet")    # subagentes de búsqueda web
ESFUERZO = os.environ.get("INVERSIONES_ESFUERZO", "high")
TIMEOUT = int(os.environ.get("INVERSIONES_TIMEOUT", 2 * 3600))
RUN_DIR = Path.home() / ".local/state/inversiones/run"

_cat = {"type": "object", "additionalProperties": False, "required": ["fecha", "evento"],
        "properties": {"fecha": {"type": "string", "description": "YYYY-MM-DD o 'semana del ...'"},
                       "evento": {"type": "string"}}}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["resumen_mercado", "calendario", "temas", "cartera", "picks", "evitar", "estrategia", "lecciones"],
    "properties": {
        "resumen_mercado": {"type": "string", "description": "Contexto macro y de mercado para el mes (markdown, 150-300 palabras)"},
        "calendario": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["fecha", "evento", "impacto"],
            "properties": {"fecha": {"type": "string"}, "evento": {"type": "string"},
                           "impacto": {"type": "string", "enum": ["alto", "medio", "bajo"]}}}},
        "temas": {"type": "array", "items": {"type": "string"}, "description": "Temas/sectores con viento a favor este mes"},
        "cartera": {"type": "array", "description": "Una decisión por cada SÍMBOLO distinto de las CARTERAS ACTUALES (vale para todos los perfiles que lo tengan)", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["simbolo", "accion", "pct_vender", "tp_pct", "sl_pct", "motivo"],
            "properties": {
                "simbolo": {"type": "string"},
                "accion": {"type": "string", "enum": ["mantener", "reducir", "vender"],
                           "description": "Para AÑADIR a una posición, inclúyela además en picks con su peso"},
                "pct_vender": {"type": "number", "description": "% de la posición a vender (0 si mantener, 100 si vender)"},
                "tp_pct": {"type": "number", "description": "Take profit, % sobre el precio actual (positivo)"},
                "sl_pct": {"type": "number", "description": "Stop loss, % de caída desde el precio actual (positivo)"},
                "motivo": {"type": "string", "description": "1-3 frases con datos"},
            }}},
        "picks": {"type": "array", "minItems": 5, "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["simbolo", "nombre", "tipo", "conviccion", "tesis", "catalizadores", "riesgos",
                         "objetivo_pct", "stop_pct", "peso_pct", "apalancamiento", "probabilidad_subida_pct", "fuentes"],
            "properties": {
                "simbolo": {"type": "string", "description": "Símbolo EXACTO de eToro (de la lista de operables)"},
                "nombre": {"type": "string"},
                "tipo": {"type": "string", "enum": ["Acción"]},
                "conviccion": {"type": "integer", "minimum": 1, "maximum": 5},
                "tesis": {"type": "string", "description": "Por qué puede subir de forma significativa ESTE mes (3-6 frases, con datos)"},
                "catalizadores": {"type": "array", "items": _cat},
                "riesgos": {"type": "string"},
                "objetivo_pct": {"type": "number", "description": "Take profit: % de subida desde el precio actual (positivo)"},
                "stop_pct": {"type": "number", "description": "Stop loss: % de caída desde la entrada (positivo), del precio, no del margen"},
                "peso_pct": {"type": "number", "description": "% del dinero de ESTE mes para comprarla (0 = solo vigilar; los >0 suman 100; máx. 5 con peso > 0)"},
                "apalancamiento": {"type": "integer", "enum": [1, 2, 5], "description": "x1 salvo justificación fuerte"},
                "probabilidad_subida_pct": {"type": "number", "description": "Probabilidad estimada (honesta) de terminar el mes en positivo"},
                "fuentes": {"type": "array", "items": {"type": "string"}, "description": "URLs consultadas"},
            }}},
        "evitar": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["simbolo", "motivo"],
            "properties": {"simbolo": {"type": "string"}, "motivo": {"type": "string"}}}},
        "estrategia": {"type": "string", "description": "Razonamiento del reparto del día 1, de los TP/SL y del apalancamiento elegido (markdown)"},
        "lecciones": {"type": "string", "description": "Qué se aprende de la evaluación de meses anteriores y cómo se ha aplicado"},
    },
}

SISTEMA = """Eres un analista de inversiones cuantitativo y fundamental, riguroso y escéptico. Trabajas para un \
grupo de inversores particulares (perfiles: titular, perfil2, perfil3) que operan en eToro, cada uno con su cartera \
real, y todos con esta operativa FIJA. La investigación es ÚNICA y compartida: las ideas y su reparto en % valen \
para los tres perfiles (cada uno aplica los porcentajes a su propio dinero); las decisiones sobre posiciones \
existentes son por símbolo y se aplican a cada perfil que lo tenga:
- Solo invierten en ACCIONES (nada de ETF, cripto, materias primas ni divisas).
- Aportan al mes: {aportacion}, y SOLO operan el día 1 de cada mes: ese día compra/vende y dejan puestos take profit \
y stop loss, que eToro ejecuta solo. No vigilan ni reaccionan hasta el día 1 del mes siguiente.
- Apalancamiento posible en acciones de eToro: x1 (acción real, sin comisiones), x2 o x5 (CFD: comisión nocturna \
diaria, triple el fin de semana; el stop se mide sobre la exposición, así que con x2 un SL del 10% pierde el 20% del \
dinero puesto). No hay x4. Mínimo 10 $ por posición.
Tu objetivo: decidir qué hacer el día 1 del mes {mes} con su cartera actual y con el dinero del mes, para \
maximizar la probabilidad de una subida significativa de la cartera hasta el día 1 del mes siguiente.

Reglas:
- Solo puedes recomendar símbolos que aparezcan en la lista de OPERABLES de los datos, con el símbolo exacto \
(o posiciones de las CARTERAS ACTUALES).
- La fecha de hoy es {hoy}. Usa información reciente (últimas 2-3 semanas) y verifica fechas de catalizadores \
en fuentes primarias o fiables (web de relación con inversores, SEC, FDA, calendarios oficiales, prensa financiera).
- Ningún análisis garantiza subidas: sé honesto con las probabilidades, no infles convicciones y explica los riesgos. \
Prefiere catalizadores concretos con fecha dentro del mes + momentum/tendencia a favor + sentimiento/revisiones \
positivas. Desconfía de lo que ya ha subido en vertical sin catalizador pendiente.
- Como no puede reaccionar durante el mes, los stops deben quedar fuera del ruido normal (referencia: volatilidad \
anual / 3,5 ≈ 1 desviación mensual) y los TP deben ser alcanzables en un mes; ante un evento binario (resultados, \
FDA), peso pequeño o stop más amplio.
- Con 100-200 € al mes por perfil concentra: 2-5 compras con peso > 0 (mín. ~15 $ cada una). Tiene en cuenta la cartera actual: \
evita sobreconcentrar sectores donde ya está cargado (diversifica: no más de 3 ideas del mismo sector/tema) y \
puedes reforzar posiciones de la cartera incluyéndolas en picks.
- Apalancamiento: por defecto x1. x2 solo con convicción ≥ 4, volatilidad anual ≤ 55% y SL × 2 ≤ 40%; x5 casi \
nunca (convicción 5, volatilidad ≤ 30%, SL ≤ 8%). Justifícalo en la tesis. El programa aplica estas reglas y baja \
el apalancamiento si no se cumplen.
- Escribe en español."""

TAREA = """Haz una investigación exhaustiva para el mes {mes} y devuelve el resultado estructurado.

Proceso recomendado (usa la herramienta Agent para lanzar subagentes EN PARALELO, en un único mensaje, y \
pásales en su prompt los datos que necesiten: no ven este mensaje):
1. Subagente MACRO: calendario del mes (bancos centrales, IPC, empleo, PIB, vencimientos de opciones, \
reequilibrios de índices, temporada de resultados), tendencia de tipos, dólar, petróleo, flujos y estacionalidad \
histórica del mes; qué sectores suelen hacerlo mejor en este contexto.
2. Subagente ACCIONES (momentum): revisa los candidatos de la lista MOMENTUM: noticias recientes, \
resultados del mes (fecha, expectativas, historial de sorpresas), revisiones de analistas, guías, interés en \
corto, actividad de insiders, eventos (lanzamientos, FDA/PDUFA, juicios, inclusión en índices, M&A, días del \
inversor). Devuelve los 10 mejores con catalizadores fechados y fuentes.
3. Subagente REBOTE y OTROS: candidatos REBOTE (caídas fuertes en valores populares: ¿sobrerreacción o deterioro \
real?) y busca 3-5 acciones fuera de las listas pero dentro de OPERABLES con catalizador claro este mes.
4. Subagente CARTERA: para CADA símbolo distinto de las CARTERAS ACTUALES (vienen marcadas por perfil; \
pásale todas las posiciones con su perfil, y no lances un subagente por perfil): noticias, resultados y eventos del mes, valoración \
y tendencia; recomienda mantener, reducir o vender y un take profit y stop loss razonables a un mes.
5. Con todo, contrasta (verifica los datos clave tú mismo si hay dudas) y devuelve: una decisión por símbolo de las \
carteras (compartida por los perfiles que lo tengan); 5-12 ideas ordenadas de mayor a menor convicción (de las que solo 2-5 llevan peso > 0: el reparto del \
dinero del mes); y una lista corta de acciones populares a EVITAR este mes.

Ten en cuenta la EVALUACIÓN de los meses anteriores (si la hay) para corregir sesgos.

DATOS (cribado cuantitativo sobre precios de eToro; r1m/r3m/r12m = rentabilidad %, mom_12_1 = 12 meses sin el \
último, cerca_max = % del máximo de 52 semanas, vol_ratio = volumen 20d / 120d, volatilidad = anual %, \
inversores = nº de usuarios de eToro con posición, inversores_crec = variación reciente de ese número, \
pct_compra = % en largo, resultados = fecha de presentación este mes) y CARTERAS ACTUALES llegan a continuación por \
la entrada estándar."""


def investigar(mes, hoy, datos, aportacion="200 € (titular) y entre 100 y 200 € (perfil2, perfil3)", log=print):
    """Devuelve (resultado_estructurado, coste_usd, turnos). Lanza RuntimeError si falla."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [str(CLAUDE), "-p", TAREA.format(mes=mes),
           "--model", MODELO, "--effort", ESFUERZO,
           "--settings", json.dumps({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": SUBMODELO}}),
           "--tools", "WebSearch,WebFetch,Agent", "--allowedTools", "WebSearch,WebFetch,Agent",
           "--restricted", "--strict-mcp-config", "--no-session-persistence",
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--system-prompt", SISTEMA.format(mes=mes, hoy=hoy, aportacion=aportacion)]
    log(f"investigación: claude -p (modelo {MODELO}, subagentes {SUBMODELO}, esfuerzo {ESFUERZO}, límite {TIMEOUT // 60} min)")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=RUN_DIR, input=datos)
    try:
        out = json.loads(res.stdout or "{}")
    except ValueError:
        out = {}
    if res.returncode or out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude falló (rc={res.returncode}): {(res.stdout or res.stderr)[-800:]}")
    return out["structured_output"], out.get("total_cost_usd", 0), out.get("num_turns", 0)
