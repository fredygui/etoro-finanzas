"""Lanzador HTTP para n8n en 127.0.0.1:5683 (n8n no ejecuta comandos; aquí solo hay acciones fijas).

POST /semanal/ejecutar[?modo=real|solo-titular|prueba] -> arranca inversiones-semanal.service si no está en marcha
GET  /semanal/estado -> estado del servicio (activo, resultado, código, fechas) y, si falló o terminó con avisos, el log
GET  /salud
El modo se pasa a la unidad por ~/.local/state/inversiones/semanal.env (SEMANAL_ARGS); solo hay tres valores posibles.
"""
import json
import os
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("LANZADOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("LANZADOR_PORT", "5683"))
UNIDAD = "inversiones-semanal.service"
ENTORNO = Path.home() / ".local/state/inversiones/semanal.env"
MODOS = {"real": "--perfil perfil2,perfil3",  # titular recibe el informe completo semanal (inversiones-titular.timer)
         "solo-titular": "--solo-titular", "prueba": "--prueba"}


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def estado(_q=None):
    props = dict(l.split("=", 1) for l in systemctl(
        "show", UNIDAD, "-p", "ActiveState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp").stdout.splitlines()
        if "=" in l)
    activo = props.get("ActiveState") in ("active", "activating", "deactivating")
    out = {"activo": activo, "estado": props.get("ActiveState"), "resultado": props.get("Result"),
           "codigo": props.get("ExecMainStatus"), "inicio": props.get("ExecMainStartTimestamp"),
           "fin": props.get("ExecMainExitTimestamp")}
    if not activo and props.get("Result") not in ("success", None):
        out["log"] = subprocess.run(["journalctl", "--user", "-u", UNIDAD, "-n", "40", "--no-pager", "-q", "-o", "short-iso"],
                                    capture_output=True, text=True, timeout=30).stdout
    return out


def ejecutar(q):
    modo = (q.get("modo") or ["real"])[0]
    if modo not in MODOS:
        return {"lanzado": False, "error": f"modo desconocido: {modo}"}
    if estado()["activo"]:
        return {"lanzado": False, "motivo": f"{UNIDAD} ya en marcha"}
    ENTORNO.parent.mkdir(parents=True, exist_ok=True)
    ENTORNO.write_text(f"SEMANAL_ARGS={MODOS[modo]}\n")
    r = systemctl("start", "--no-block", UNIDAD)
    return {"lanzado": r.returncode == 0, "modo": modo, "solicitado": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": r.stderr.strip() or None}


RUTAS = {("GET", "/salud"): lambda q: {"ok": True}, ("GET", "/semanal/estado"): estado, ("POST", "/semanal/ejecutar"): ejecutar}


class Handler(BaseHTTPRequestHandler):
    def _responder(self, metodo):
        u = urlparse(self.path)
        fn = RUTAS.get((metodo, u.path))
        try:
            datos, codigo = (fn(parse_qs(u.query)), 200) if fn else ({"error": "no existe"}, 404)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            datos, codigo = {"error": str(e)}, 500
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        self._responder("GET")

    def do_POST(self):
        self._responder("POST")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
