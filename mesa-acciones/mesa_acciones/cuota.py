"""Consumo de la suscripción Claude (endpoint OAuth no documentado; si falla, no bloquea)."""
import json
from pathlib import Path

from .comun import get_json


def uso():
    """{'h5': %, 'semana': %} o None si no se puede leer."""
    try:
        tok = json.loads((Path.home() / ".claude/.credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        d = get_json("https://api.anthropic.com/api/oauth/usage", intentos=1,
                     headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"})
        return {"h5": d["five_hour"]["utilization"], "semana": d["seven_day"]["utilization"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None
