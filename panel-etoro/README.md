# panel-etoro

Web LAN (puerto **8777**, servicio de usuario `panel-etoro`) que muestra la cartera real de eToro y la mesa de acciones
([[mesa-acciones]]). Es una interfaz de menu-web (categoría Finanzas), así que aparece en app-movil sin recompilar.

- `server.py`: stdlib (`http.server`), `static/index.html` (HTML/CSS/JS y SVG propios, sin CDN), tema claro/oscuro.
- `etoro_ro.py`: cliente eToro **solo GET** con caché (60 s API, 5 min velas); no puede operar.
- Lee `~/work/mesa-acciones/{data,config.json}` (nunca importa su código de trading; solo carga `horario.py`).
- `data/historial.json`: instantánea del valor cada 15 min (hilo del servidor).
- Único efecto sobre la mesa: `POST /api/modo` (cabecera `X-Panel: 1`) cambia la clave `"modo"` de config.json
  (`lectura`/`real`), escritura atómica conservando el resto. Botón «Pausar/Reanudar mesa» con confirmación.
- Pruebas: `PANEL_CONFIG=/ruta/copia.json PANEL_HISTORIAL=/ruta/h.json .venv/bin/python server.py 8779`.

Endpoints: `/api/panel`, `/api/comite`, `/api/acta/<id>`, `/api/operaciones`, `/api/historial`, `/api/velas/<id>?i=OneHour|FourHours|OneDay`.
