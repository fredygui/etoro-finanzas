# etoro-finanzas

Sistema personal de inversión en acciones sobre la **API pública de eToro**, publicado de forma **anonimizada**
(sin carteras, informes, destinatarios, claves ni datos de cuenta). Pensado para un servidor Linux con unidades
`systemd --user` y [Claude Code](https://claude.com/claude-code) (`claude -p`) como motor de análisis.

| Carpeta | Qué hace |
|---|---|
| [`inversiones/`](inversiones/) | Informe y plan periódico por perfil (cribado, investigación con Claude, plan de compras/ventas con TP/SL) y ejecutor opcional del plan. |
| [`mesa-acciones/`](mesa-acciones/) | Mesa de trading *swing*: scout del universo, vigía de precios y comité de agentes que gestiona SL/TP y órdenes x1. Incluye simulador de pruebas (`pruebas/prueba_mesa.py`). |
| [`panel-etoro/`](panel-etoro/) | Panel web (solo lectura de eToro) con cartera, comité, operaciones y evolución; único efecto: pausar/reanudar la mesa. |

## Anonimización
- Perfiles renombrados a `titular`, `perfil2`, `perfil3`; correos → `avisos@example.com`; IPs/rutas genéricas.
- Las posiciones del simulador (símbolos, IDs e importes) son ficticias. `mesa-acciones/config.json` viene en `"modo": "lectura"`.
- Las claves van en `~/.config/inversiones/<perfil>.env` (fuera del repo, nunca versionadas).

## Aviso
Proyecto personal y experimental. **No es asesoramiento financiero**; operar con dinero real conlleva riesgo de pérdida.
