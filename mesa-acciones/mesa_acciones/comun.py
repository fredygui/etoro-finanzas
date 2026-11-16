"""Rutas, configuración, estado persistente, log y avisos (app app-movil vía `aviso` y WhatsApp vía `wa`)."""
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
CONFIG = RAIZ / "config.json"
ESTADO = DATA / "estado.json"
LOG = DATA / "mesa.log"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mesa-acciones/1.0", "Accept": "application/json",
      "Accept-Encoding": "identity"}
TOOLBOX = Path.home() / "work/toolbox/bin"


def cfg():
    c = json.loads(CONFIG.read_text())
    return c


def ahora():
    return datetime.now(timezone.utc)


def iso(t=None):
    return (t or ahora()).strftime("%Y-%m-%dT%H:%M:%SZ")


def de_iso(s):
    """Acepta 'YYYY-MM-DDTHH:MM:SSZ' y las variantes de eToro (fracciones, sin Z, +00:00)."""
    s = str(s).replace("Z", "").split("+")[0]
    if "." in s:
        s = s.split(".")[0]
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def log(msg):
    DATA.mkdir(exist_ok=True)
    linea = f"{iso()} {msg}"
    print(linea, flush=True)
    with LOG.open("a") as f:
        f.write(linea + "\n")


def leer(ruta, defecto):
    try:
        return json.loads(Path(ruta).read_text())
    except (OSError, ValueError):
        return defecto


def guardar(ruta, datos):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False))
    os.replace(tmp, ruta)


def get_json(url, headers=None, timeout=30, intentos=3):
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (OSError, ValueError):
            if i == intentos - 1:
                raise
            time.sleep(2 * (i + 1))


def _silencio():
    return bool(os.environ.get("MESA_SIN_WA"))


def wa(texto):
    """WhatsApp al usuario (CallMeBot limita longitud: se recorta)."""
    if not cfg()["avisos"].get("whatsapp") or _silencio():
        print(f"[WhatsApp omitido]\n{texto}")
        return
    texto = texto if len(texto) <= 900 else texto[:880] + "\n…(ver data/mesa.log)"
    for intento in range(3):     # CallMeBot falla a ratos: reintenta
        try:
            subprocess.run([str(TOOLBOX / "wa"), texto], check=True, capture_output=True, timeout=60)
            return
        except (OSError, subprocess.SubprocessError) as e:
            if intento == 2:
                log(f"aviso WhatsApp falló: {str(e)[:150]}")
            else:
                time.sleep(20 * (intento + 1))


def app(titulo, cuerpo):
    """Notificación en app-movil (buzón de menu-web) con el texto completo como cuerpo."""
    if not cfg()["avisos"].get("app", True) or _silencio():
        print(f"[aviso app omitido] {titulo}")
        return
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write(cuerpo + "\n\n— mesa-acciones (servidor). Actas: ~/work/mesa-acciones/data/comites/\n")
        subprocess.run([str(TOOLBOX / "aviso"), titulo[:120], "-c", tf.name], check=True, capture_output=True,
                       timeout=60)
        os.unlink(tf.name)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"aviso a la app falló: {str(e)[:150]}")
