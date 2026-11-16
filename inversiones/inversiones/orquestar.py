"""Envío del correo del informe mensual (sustituye al flujo de n8n): usa el script de la caja `correo`."""
import html
import json
import os
import subprocess
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINATARIOS = RAIZ / "destinatarios.json"
CORREO = Path.home() / "work/toolbox/bin/correo"
WEB = "http://127.0.0.1:3000/Inversiones"


def destinatarios(clave):
    """Destinatarios de errores y avisos (solo el titular)."""
    return json.loads(DESTINATARIOS.read_text())[clave]


def destinatarios_perfil(nombre):
    return json.loads(DESTINATARIOS.read_text())["perfiles"][nombre]


def enviar(para, asunto, cuerpo_html, forzar_correo=False):
    """Un envío con `correo -p` (con forzar_correo, `-m`: siempre por Gmail, aunque sea el buzón propio, que si no va a app-movil);
    el HTML pasa por un directorio temporal 0700."""
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        f = Path(tmp) / "cuerpo.html"
        f.write_text(cuerpo_html, encoding="utf-8")
        r = subprocess.run([str(CORREO), *(["-m"] if forzar_correo else []), "-p", para, asunto, str(f)], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(f"correo a {para} falló ({r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")


def pie(mes, perfil):
    if perfil.nombre != "titular":     # Perfil2 y Perfil3 no están en la LAN ni usan el repo
        return ('<p style="color:#71717a;font-size:12px">Solo acciones · operativa únicamente el día 1 (las órdenes llevan '
                'TP/SL). Informe generado automáticamente: no es asesoramiento financiero.</p>')
    return ('<p style="color:#71717a;font-size:12px">Solo acciones · 200 €/mes · operativa únicamente el día 1 (las órdenes llevan TP/SL). '
            'El próximo informe da este plan por ejecutado; si no lo haces tal cual, corrígelo antes del día 1 con '
            '<code>python -m inversiones.cartera</code> en ~/work/inversiones.<br>'
            f'Nota en Obsidian: Inversiones/{mes}.md · <a href="{WEB}/{mes}">ver en SilverBullet (LAN)</a> · '
            f'<a href="{WEB}/Cartera">cartera</a></p>')


def enviar_informe(perfil, solo_titular=False):
    """Envía el último informe del perfil a sus destinatarios (o, con solo_titular, solo a la lista de errores)."""
    d = json.loads(perfil.ultimo.read_text())
    cuerpo = d["html"] + pie(d["mes"], perfil)
    dest = destinatarios("error") if solo_titular else destinatarios_perfil(perfil.nombre)
    asunto = d["asunto"] + (f" [{perfil.etiqueta}]" if solo_titular and perfil.nombre != "titular" else "")
    for p in dest:
        enviar(p, asunto, cuerpo, forzar_correo=True)   # los informes siempre por correo (2026-11-09)
    return dest


def log_servicio(n=40):
    r = subprocess.run(["journalctl", "--user", "-u", "inversiones.service", "--no-pager", "-n", str(n), "-o", "cat"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or "(sin log)"


def enviar_error(motivo, titulo="Inversiones: el informe mensual ha fallado"):
    cuerpo = (f"<h3>{html.escape(titulo)}</h3><p>{html.escape(motivo)}</p>"
              f"<pre style=\"font-size:12px;white-space:pre-wrap\">{html.escape(log_servicio())}</pre>")
    for p in destinatarios("error"):
        enviar(p, titulo, cuerpo)


def enviar_aviso(lineas, titulo="Inversiones: avisos del informe mensual"):
    """Aviso (no es un fallo): solo a la lista de errores/avisos."""
    cuerpo = f"<h3>{html.escape(titulo)}</h3><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in lineas) + "</ul>"
    for p in destinatarios("error"):
        enviar(p, titulo, cuerpo)
