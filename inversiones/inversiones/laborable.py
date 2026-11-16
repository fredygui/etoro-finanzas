"""Calendario laboral de Madrid (España) para decidir si hoy toca la operativa semanal.

Toca el PRIMER día laborable de la semana: normalmente el lunes; si el lunes es festivo en Madrid, el martes; etc.
Festivos: nacionales + Comunidad de Madrid (paquete `holidays`, subdiv MD) + locales de Madrid capital (San Isidro y
la Almudena). Fines de semana nunca.

python -m inversiones.laborable [YYYY-MM-DD] [--informe PERFIL]   # sale 0 si ese día (por defecto hoy) toca, 1 si no
  --informe PERFIL   además, no toca si ese perfil ya tiene un informe generado esta semana (p. ej. lanzado a mano)
Lo usan los ExecCondition= de inversiones-titular.service (informe 06:00) e inversiones-ejecutar.service (16:00):
sus timers saltan de lunes a viernes y solo el primer día laborable de la semana pasan el filtro.
"""
import json
import sys
from datetime import date, timedelta
from functools import lru_cache

import holidays

LOCALES = {(5, 15): "San Isidro (Madrid capital)", (11, 9): "Nuestra Señora de la Almudena (Madrid capital)"}


@lru_cache(maxsize=8)
def festivos(anio):
    f = dict(holidays.Spain(subdiv="MD", years=anio, language="es"))
    for (m, d), nombre in LOCALES.items():
        f.setdefault(date(anio, m, d), nombre)
    return f


def motivo_no_laborable(d):
    if d.weekday() >= 5:
        return "fin de semana"
    return festivos(d.year).get(d)


def es_laborable(d):
    return motivo_no_laborable(d) is None


def toca(d):
    """(bool, explicación): True si `d` es el primer día laborable de su semana."""
    m = motivo_no_laborable(d)
    if m:
        return False, f"{d} no es laborable en Madrid ({m})"
    previos = [d - timedelta(days=k) for k in range(1, d.weekday() + 1)]
    hecho = next((p for p in previos if es_laborable(p)), None)
    if hecho:
        return False, f"esta semana ya se operó el {hecho} (primer laborable)"
    saltados = [f"{p} ({motivo_no_laborable(p)})" for p in reversed(previos)]
    return True, f"{d}: primer laborable de la semana" + (f"; se pasó desde {', '.join(saltados)}" if saltados else "")


def informe_esta_semana(perfil, d):
    """Fecha del último informe del perfil si es de la semana de `d` (desde su lunes); si no, None."""
    from . import perfiles
    try:
        g = json.loads(perfiles.PERFILES[perfil].ultimo.read_text()).get("generado", "")[:10]
    except (OSError, ValueError):
        return None
    return g if (d - timedelta(days=d.weekday())).isoformat() <= g <= d.isoformat() else None


def main():
    args = sys.argv[1:]
    perfil = None
    if "--informe" in args:
        i = args.index("--informe")
        perfil = args[i + 1]
        del args[i:i + 2]
    d = date.fromisoformat(args[0]) if args else date.today()
    ok, txt = toca(d)
    if ok and perfil and (g := informe_esta_semana(perfil, d)):
        ok, txt = False, f"{txt}, pero el informe de {perfil} de esta semana ya se generó el {g}"
    print(txt)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — 255: systemd lo trata como fallo (1-254 sería «se omite» en silencio)
        print(f"error comprobando el calendario: {e!r}")
        sys.exit(255)
