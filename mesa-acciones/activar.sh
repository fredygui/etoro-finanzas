#!/usr/bin/env bash
# activar.sh — pasa mesa-acciones a dinero REAL: pausa inversiones-ejecutar, modo real y servicios systemd de usuario
# Requiere la confirmación expresa del usuario (opera con dinero real en eToro). Revertir: ver README («Pausar»).
set -euo pipefail
cd "$HOME/work/mesa-acciones"
PATH="$HOME/work/toolbox/bin:$PATH"

echo "1/5 simulador (eToro, mercado y comité falsos)…"
tmp=$(mktemp -d)
MESA_SIN_WA=1 .venv/bin/python pruebas/prueba_mesa.py "$tmp" | tail -1
rm -rf "$tmp"

echo "2/5 pauso el ejecutor de los lunes de inversiones…"
systemctl --user disable --now inversiones-ejecutar.timer
cambio "Pausado inversiones-ejecutar.timer (lo sustituye mesa-acciones)" \
  "systemctl --user disable --now inversiones-ejecutar.timer" \
  "pausar la mesa (README de mesa-acciones) y systemctl --user enable --now inversiones-ejecutar.timer" \
  "la mesa de acciones gestiona en real la cartera del titular; el informe de los lunes 06:00 sigue"

echo "3/5 modo real en config.json…"
.venv/bin/python - <<'EOF'
import json
c = json.load(open("config.json"))
c["modo"] = "real"
open("config.json", "w").write(json.dumps(c, indent=2, ensure_ascii=False) + "\n")
EOF
git add config.json && git commit -qm "Modo real activado" || true

echo "4/5 servicios…"
py=".venv/bin/python -m mesa_acciones"
crear-servicio mesa-acciones mesa-acciones-vigia "$py vigia servicio" -f \
  -d "Mesa acciones: VIGÍA de precios (convoca la mesa por alertas)"
crear-servicio mesa-acciones mesa-acciones-ciclo "$py ciclo" -t "Mon..Fri *:0/15" -f \
  -d "Mesa acciones: sincroniza con eToro y reúne al comité si hay posiciones sin plan"
crear-servicio mesa-acciones mesa-acciones-revision "$py revision" -t "Mon..Fri 16:15" -f \
  -d "Mesa acciones: revisión diaria del comité (Scout + cartera + informe semanal)"
crear-servicio mesa-acciones mesa-acciones-universo "$py universo" -t "Sun 07:00" -f \
  -d "Mesa acciones: recalcula el universo líquido"
crear-servicio mesa-acciones mesa-acciones-informe "$py informe" -t "Mon..Fri 22:30" -f \
  -d "Mesa acciones: resumen del día a la app (solo si hubo operaciones)"
git add systemd && git commit -qm "Unidades systemd de la mesa" || true

echo "5/5 primera sincronización (adopta la cartera; el comité se reúne cuando abra su bolsa)…"
systemctl --user start mesa-acciones-ciclo.service
sleep 3
journalctl --user -u mesa-acciones-ciclo -u mesa-acciones-vigia -n 20 --no-pager
.venv/bin/python -m mesa_acciones estado
