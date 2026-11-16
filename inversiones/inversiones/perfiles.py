"""Perfiles de inversor (titular, perfil2, perfil3): cada uno con su cartera, claves de eToro, notas, informes y correo.

La investigación (catálogo, cribado, calendario y `claude -p`) es compartida; lo propio de cada perfil es la cartera
real (API de eToro con sus claves), el plan de órdenes, el informe y el correo.

el titular conserva sus rutas históricas (cartera.json, etoro.env, Inversiones/, informes/, ultimo.json).
"""
from dataclasses import dataclass, field, replace
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
VAULT = Path.home() / "Obsidian/Proyectos"
CONFIG_DIR = Path.home() / ".config/inversiones"
ESTADO = Path.home() / ".local/state/inversiones"


@dataclass(frozen=True)
class Perfil:
    nombre: str
    etiqueta: str
    cartera: Path          # base de datos de la cartera (versionada)
    claves: Path           # ETORO_USER_KEY / ETORO_API_KEY (permiso 600)
    notas: Path            # carpeta de notas en Obsidian
    pref: str              # prefijo de los enlaces [[...]] de las notas
    informes: Path         # JSON mensual versionado
    ultimo: Path           # HTML del último correo
    config: dict = field(default_factory=dict)   # valores por defecto que se mezclan con `config` de la cartera


# Perfil2 y Perfil3: la aportación varía (100-200 €/mes); el presupuesto es el efectivo real en eToro el día del cálculo.
# Si la API falla (o no hay claves) se usa `aportacion_eur` (150 €) y el correo avisa de que se escala en proporción.
_CFG_VARIABLE = {"aportacion_eur": 150, "aportacion_modo": "efectivo_real", "aportacion_min_eur": 100,
                 "aportacion_max_eur": 200}


def _otro(nombre, etiqueta):
    return Perfil(nombre, etiqueta, RAIZ / f"cartera-{nombre}.json", CONFIG_DIR / f"{nombre}.env",
                  VAULT / "Inversiones" / etiqueta, f"Inversiones/{etiqueta}", RAIZ / "informes" / nombre,
                  ESTADO / f"ultimo-{nombre}.json", dict(_CFG_VARIABLE))


PERFILES = {
    "titular": Perfil("titular", "el titular", RAIZ / "cartera.json", CONFIG_DIR / "etoro.env", VAULT / "Inversiones",
                      "Inversiones", RAIZ / "informes", ESTADO / "ultimo.json"),
    "perfil2": _otro("perfil2", "Perfil2"),
    "perfil3": _otro("perfil3", "Perfil3"),
}


def elegir(nombres=None):
    """Lista de perfiles a partir de --perfil (repetible o separado por comas); None = todos."""
    if not nombres:
        return list(PERFILES.values())
    out = []
    for n in ",".join(nombres).split(","):
        n = n.strip().lower()
        if n and n not in PERFILES:
            raise SystemExit(f"perfil desconocido: {n} (válidos: {', '.join(PERFILES)})")
        if n and PERFILES[n] not in out:
            out.append(PERFILES[n])
    return out


def para_prueba(p, carpeta):
    """Copia del perfil con las salidas (notas, informes, ultimo) redirigidas a `carpeta` (modo --prueba)."""
    d = Path(carpeta) / p.nombre
    return replace(p, notas=d / "notas", informes=d / "informes", ultimo=d / "ultimo.json")
