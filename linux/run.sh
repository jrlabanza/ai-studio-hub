#!/usr/bin/env bash
# Start AI Studio Hub in this window.
#
#   ./run.sh                  foreground: log in this window, Ctrl+C / close = stop the hub and every studio it started
#   ./run.sh --background     start detached, open the browser, return
#   ./run.sh --no-open        don't open the browser
#   ./stop.sh                 stop it
#
# Anything else is passed to the hub (--port N, --host 0.0.0.0, --no-browser).
# The hub itself is a small Python program (no PyTorch, no models); the studios
# run in their own containers, driven through docker compose. Run
# ./initialize.sh once first (or "Initialize AI Studio Hub.sh" in the repo root).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TOOL="hub"; PORT=7900; URL="http://localhost:$PORT"
VPY="$ROOT/.venv/bin/python"

BG=0; ARGS=()
for a in "$@"; do
  case $a in
    --background|-d) BG=1 ;;
    --no-open) ARGS+=(--no-browser) ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) ARGS+=("$a") ;;
  esac
done
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  [[ ${ARGS[$i]} == --port && -n ${ARGS[$((i + 1))]:-} ]] && PORT=${ARGS[$((i + 1))]} && URL="http://localhost:$PORT"
done

if [[ ! -x $VPY ]]; then
  echo
  echo "  AI Studio Hub is not set up yet."
  echo "  Run  $HERE/initialize.sh  first - it installs the hub's small Python environment."
  echo
  exit 1
fi

export PYTHONUTF8=1 PYTHONUNBUFFERED=1
cd "$ROOT"
echo; echo "    AI Studio Hub"; echo "    $URL"; echo
if (( BG )); then
  if curl -sf -o /dev/null --max-time 2 "$URL/api/state" 2>/dev/null; then
    echo "    already running; opening it.  stop: ./stop.sh"
    command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 &
    exit 0
  fi
  mkdir -p "$ROOT/data/logs"
  nohup "$VPY" -m hub "${ARGS[@]}" >> "$ROOT/data/logs/hub.log" 2>&1 &
  disown
  echo "    running in the background (log: data/logs/hub.log).  stop: ./stop.sh"
else
  echo "    Ctrl+C or close this window to stop the hub and every studio it started."
  echo "-----------------------------------------------------------------"
  exec "$VPY" -m hub "${ARGS[@]}"
fi
