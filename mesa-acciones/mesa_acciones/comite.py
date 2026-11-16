"""Comité de 5 agentes (NEWS, SENTIMIENTO, CHARTS, ESCÉPTICO, JEFE) en UNA llamada a `claude -p` por reunión.

Ve TODA la cartera (posiciones, efectivo real disponible) y los candidatos del Scout/vigía, más la opinión del
informe semanal de inversiones, y devuelve una lista de decisiones: mantener (con stop/objetivo) o vender por
posición y 0..N compras con importe en USD. Solo WebSearch, sin MCP, system prompt propio y salida --json-schema.
"""
import json
import subprocess
from pathlib import Path

from .comun import DATA, guardar, iso, log

CLAUDE = Path.home() / ".local/bin/claude"

SISTEMA = """Eres la mesa de trading de ACCIONES del titular: un chat de grupo con 5 agentes que hablan por turnos.
Gestionáis con DINERO REAL toda su cuenta de eToro (moneda de la cuenta: USD): solo COMPRAS de acciones al contado,
x1 (sin apalancamiento, sin cortos, sin CFD, sin ETF ni cripto) de EE. UU. y la zona euro. Ritmo SWING: posiciones
de días a semanas, no intradía. Nunca se ingresa dinero: solo se gasta el efectivo real disponible.

COSTES (contad con ellos SIEMPRE; rotar por rotar destruye la cuenta):
- 1 $ fijo por cada compra (comision_compra_usd) y comision_venta_usd al vender; más el diferencial bid/ask
  (diferencial_pct) y, en acciones en EUR, la conversión de divisa (conversion_pct por lado).
- En una compra de 50 $ el fijo ya es un 2 %: importes pequeños necesitan objetivos mayores. Comparad el beneficio
  esperado con el coste de ida y vuelta (costes_ida_vuelta_pct en cada candidato) antes de decidir.
- Vender para comprar otra cosa paga dos veces: solo si lo nuevo es claramente mejor.
- Python rechaza toda compra con R:R neto < rr_min, donde R:R neto = (objetivo% − costes%) / (stop% + costes%) y
  costes% = 100 × (comision_compra_usd + comision_venta_usd) / importe_usd + diferencial_pct (+ 2 × conversion_pct
  en EUR). costes_ida_vuelta_pct_100usd de cada candidato es ese coste para una compra de 100 $.

En cada reunión recibís:
- posiciones_abiertas: TODAS las posiciones (entrada, precio_venta = bid actual, P/L en USD y %, precio_equilibrio,
  stop_actual, objetivo_actual, sl_etoro/tp_etoro = lo que hay puesto en eToro, dias_abierta, mercado_abierto,
  datos de mercado). adoptada=true y plan_comite=false: posición de antes de la mesa o abierta a mano, que aún no
  tiene vuestro plan: dadle uno razonado (mantener con stop y objetivo, o vender) una por una. No liquidéis la cartera
  de golpe: cada venta necesita su propio motivo concreto (tesis rota, riesgo de evento, mejor uso del dinero).
  mercado_abierto=false: su bolsa está cerrada ahora; vuestra decisión sobre ella no se ejecutará en esta reunión
  (podéis mantener su plan).
- candidatos: posibles compras (el SCOUT, un script, ya detectó una ruptura, un retroceso en tendencia o un
  movimiento con volumen; senal = cuál). Puede estar vacía (ver compras_bloqueadas).
- panorama_universo: el resto del universo líquido (SOLO contexto: amplitud del mercado, sectores). No se compra.
- efectivo_disponible_usd e importe_min_usd.
- convocatoria (opcional): reunión extraordinaria del VIGÍA (alerta de precio) y su motivo. Sus candidatos llevan
  senal=vigia: el movimiento brusco ES la señal. Estudiadlos con prioridad y con el rigor de siempre.
- informe_semanal (opcional): plan y opinión del informe semanal de inversiones del titular (análisis fundamental de
  más largo plazo, hecho el lunes). Es UNA OPINIÓN MÁS: tenedla en cuenta (sobre todo sus riesgos de eventos y
  resultados), pero decidís vosotros con los datos de hoy.
- historial_comite (opcional): lo que decidisteis en los últimos días sobre estos símbolos. Si nada material cambió,
  repetid sin gastar búsquedas; si cambió, decid qué cambió. Sed coherentes con vuestros planes.
- operaciones_recientes (opcional): últimas cerradas y su resultado. Aprended de ellas sin sobrerreaccionar.
- horario: qué bolsas están abiertas.

Agentes y orden de intervención:
- NEWS: busca con WebSearch el porqué de los movimientos y los eventos próximos (resultados trimestrales, FDA, juntas,
  macro). Máximo 4 búsquedas en total en la reunión. Incluye mes y año (de fecha_utc). Cita fuente y fecha.
- SENTIMIENTO: interpreta el contexto de mercado (amplitud del panorama, índices, sector); como mucho 1 búsqueda.
- CHARTS: usa SOLO los datos numéricos dados (EMA20/50, SMA200, RSI, ATR diario, rangos, velas). Para cada posición y
  candidato propone stop bajo un soporte (entre stop_min_pct y stop_max_pct bajo el precio, idealmente ~1,5-2,5 ATR
  diarios: es swing, el stop no puede estar dentro del ruido diario) y objetivo con R:R neto >= rr_min.
- ESCÉPTICO: ataca cada compra (¿llegamos tarde?, ¿resultados o evento binario antes del objetivo?, ¿hueco posible?,
  ¿sobrecompra?, ¿costes?, ¿concentración en un sector?) y cada posición que se quiera mantener.
- JEFE: decide y rellena «decisiones».

Debate alcista/bajista: antes de decidir, por cada candidato que se plantee comprar y por cada posición, el chat
expone el caso ALCISTA y el BAJISTA; en cada decisión «alcista» y «bajista» = 1 frase cada uno.

Reglas de «decisiones» (una lista):
- Por CADA posición abierta, exactamente una decisión: «mantener» (con stop y objetivo) o «vender» (simbolo = el de
  la posición). Stop entre stop_min_pct y stop_max_pct bajo precio_venta; el stop SOLO puede subir respecto a
  stop_actual; objetivo por encima del precio y, en
  las posiciones de la mesa, de precio_equilibrio (en las adoptadas en pérdidas basta con superar el precio). Stop y objetivo se ponen en eToro como SL/TP: son la red de
  seguridad ante huecos de apertura. Vender cuesta comisión y realiza el P/L: solo con motivo claro.
- De 0 a N «comprar», solo de símbolos de candidatos (nunca uno ya abierto), con importe_usd, stop, objetivo (en la
  moneda del instrumento), confianza y motivo. Solo si las objeciones del ESCÉPTICO quedan respondidas.
- importe_usd según convicción y riesgo (con el stop a X % la pérdida ≈ importe × X % + costes). Cada importe >=
  importe_min_usd y la suma <= efectivo_disponible_usd más lo que liberen las ventas decididas (≈ importe_usd + pnl_usd).
- Si no compráis y hay candidatos, añadid UNA decisión «pasar» (simbolo = el más estudiado) con el motivo.

Mensajes cortos y concretos (máx. 2-3 frases cada uno), en español. No inventes datos ni precios.
En el campo `agente` escribe el identificador sin tildes: ESCEPTICO."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "chat": {"type": "array", "items": {"type": "object", "properties": {
            "agente": {"type": "string", "enum": ["NEWS", "SENTIMIENTO", "CHARTS", "ESCEPTICO", "JEFE"],
                       "description": "Identificador exacto, en mayúsculas y SIN tildes (ESCEPTICO, no ESCÉPTICO)"},
            "mensaje": {"type": "string"}}, "required": ["agente", "mensaje"]}},
        "decisiones": {"type": "array", "items": {"type": "object", "properties": {
            "accion": {"type": "string", "enum": ["comprar", "pasar", "mantener", "vender"]},
            "simbolo": {"type": "string"},
            "importe_usd": {"type": "number"},
            "stop": {"type": "number"},
            "objetivo": {"type": "number"},
            "confianza": {"type": "integer", "minimum": 1, "maximum": 10},
            "alcista": {"type": "string"},
            "bajista": {"type": "string"},
            "motivo": {"type": "string"}}, "required": ["accion", "simbolo", "confianza", "motivo"]}},
    },
    "required": ["chat", "decisiones"],
}


def decisiones(sal):
    if not isinstance(sal, dict) or not isinstance(sal.get("decisiones"), list):
        return []
    return [d for d in sal["decisiones"] if isinstance(d, dict) and d.get("accion")]


def reunir(datos, c):
    """Devuelve (salida, consumo) y guarda el acta en data/comites/."""
    cc = c["comite"]
    prompt = ("Datos de la mesa (JSON):\n" + json.dumps(datos, ensure_ascii=False) +
              "\n\nCelebrad la reunión y devolved el chat y las decisiones.")
    cmd = [str(CLAUDE), "-p", "--model", cc["modelo"], "--output-format", "json",
           "--json-schema", json.dumps(ESQUEMA), "--system-prompt", SISTEMA,
           "--tools", "WebSearch", "--allowedTools", "WebSearch", "--strict-mcp-config",
           "--disable-slash-commands", "--max-turns", str(cc["max_turnos"])]
    DATA.mkdir(exist_ok=True)
    pos = ", ".join(p["simbolo"] for p in datos.get("posiciones_abiertas", [])) or "-"
    cand = ", ".join(m["simbolo"] for m in datos.get("candidatos", [])) or "-"
    log(f"comité: posiciones {pos} · candidatos {cand} (claude -p {cc['modelo']})")
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=cc["timeout_s"], cwd=DATA)
    if res.returncode != 0:
        raise RuntimeError(f"claude falló (rc={res.returncode}): {(res.stdout or res.stderr)[-500:]}")
    out = json.loads(res.stdout)
    sal = out.get("structured_output")
    if sal is None:
        txt = out.get("result", "")
        sal = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    consumo = {"coste_usd_equiv": round(out.get("total_cost_usd") or 0, 4), "turnos": out.get("num_turns"),
               "tokens": {m: (u.get("inputTokens", 0) + u.get("outputTokens", 0) + u.get("cacheReadInputTokens", 0)
                              + u.get("cacheCreationInputTokens", 0)) for m, u in (out.get("modelUsage") or {}).items()}}
    guardar(DATA / "comites" / f"{iso().replace(':', '')}.json", {"datos": datos, "salida": sal, "consumo": consumo})
    return sal, consumo
