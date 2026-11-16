#!/usr/bin/env bash
# recordatorio.sh [--solo-titular] — penúltimo día del mes: recuerda a cada perfil ingresar dinero en eToro antes del informe del día 1
set -euo pipefail
cd "$(dirname "$0")"
CORREO="$HOME/work/toolbox/bin/correo"
solo_titular=no
[[ "${1:-}" == "--solo-titular" ]] && solo_titular=si

# mes del próximo informe (día 1 del mes que viene)
mes=$(LC_TIME=es_ES.UTF-8 date -d "$(date +%Y-%m-01) +1 month" '+%B de %Y' 2>/dev/null || date -d "$(date +%Y-%m-01) +1 month" '+%Y-%m')

importe() {  # rango de aportación según la config de la cartera del perfil
  local f=cartera.json
  [[ "$1" != titular ]] && f="cartera-$1.json"
  jq -r '.config | if (.aportacion_min_eur // 100) == (.aportacion_max_eur // 200)
                   then "\(.aportacion_min_eur) €"
                   else "entre \(.aportacion_min_eur // 100) y \(.aportacion_max_eur // 200) €" end' "$f"
}

fallos=0
for perfil in $(jq -r '.perfiles | keys[]' destinatarios.json); do
  nombre="${perfil^}"
  cantidad=$(importe "$perfil")
  asunto="Recordatorio: mete dinero en eToro antes del día 1 (${nombre})"
  html=$(cat <<EOF
<p>Hola ${nombre},</p>
<p>El <b>día 1 a las 07:00</b> se genera tu informe de inversiones de <b>${mes}</b>, y el plan de compras se calcula
con el <b>efectivo libre que tengas en eToro</b> en ese momento.</p>
<p>👉 Ingresa tu aportación del mes (<b>${cantidad}</b>) en eToro <b>antes del día 1</b> para que el plan la tenga en cuenta.</p>
<p style="color:#888;font-size:12px">Correo automático del servidor servidor. No es asesoramiento financiero.</p>
EOF
)
  if [[ $solo_titular == si ]]; then
    destinos=(avisos@example.com)
  else
    mapfile -t destinos < <(jq -r --arg p "$perfil" '.perfiles[$p][]' destinatarios.json)
  fi
  for d in "${destinos[@]}"; do
    if printf '%s\n' "$html" | "$CORREO" -p "$d" "$asunto" -; then
      echo "recordatorio de $perfil enviado a $d"
    else
      echo "ERROR enviando recordatorio de $perfil a $d" >&2
      fallos=$((fallos + 1))
    fi
  done
done
exit $((fallos > 0))
