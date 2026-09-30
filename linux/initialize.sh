#!/usr/bin/env bash
# One-time setup of AI Studio Hub on Linux (safe to re-run):
#   1. the hub's own small Python environment in .venv (FastAPI, uvicorn, httpx,
#      websockets, psutil, Pillow - no PyTorch, no models)
#   2. a check of every studio: is its container image built, are its models there
#   3. an app-menu entry ("AI – Studio Hub")
#
#   ./initialize.sh               everything above
#   ./initialize.sh --no-desktop  skip the app-menu entry
#   ./initialize.sh --repair      recreate the .venv from scratch
#
# The studios themselves are set up with their own linux/initialize.sh (driver,
# Docker, image, models) - or "ai init <tool>" if you use the AI Launcher.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="$ROOT/.venv"; VPY="$VENV/bin/python"
DESKTOP=1; REPAIR=0
for a in "$@"; do
  case $a in
    --no-desktop) DESKTOP=0 ;;
    --repair) REPAIR=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

echo; echo "  AI Studio Hub - setup"; echo "  ---------------------"; echo

# --- 1. Python ---------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3.14 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null && { PY=$c; break; }
done
if [[ -z $PY ]]; then
  echo "  Python 3.10+ was not found. On Ubuntu:  sudo apt install python3 python3-venv"; exit 1
fi
if ! "$PY" -m venv --help >/dev/null 2>&1 || ! "$PY" -c 'import ensurepip' 2>/dev/null; then
  echo "  $PY has no venv/ensurepip module; installing python3-venv ..."
  if sudo -n true 2>/dev/null; then sudo -n apt-get install -y -q python3-venv python3-pip >/dev/null || true
  else sudo apt-get install -y python3-venv python3-pip || true; fi
fi
(( REPAIR )) && [[ -d $VENV ]] && { echo "  Removing the old environment ..."; rm -rf "$VENV"; }
if [[ ! -x $VPY ]]; then
  echo "  Creating the hub environment in .venv with $PY ($("$PY" -c 'import platform; print(platform.python_version())')) ..."
  "$PY" -m venv "$VENV" || { echo "  could not create the venv"; exit 1; }
fi
echo "  Installing / upgrading packages ..."
"$VPY" -m pip install --upgrade pip --quiet --disable-pip-version-check || exit 1
"$VPY" -m pip install --upgrade -r "$ROOT/hub/requirements.txt" --quiet --disable-pip-version-check || {
  echo "  Setup failed. Check your internet connection and the messages above, then run this script again."; exit 1; }

# --- 2. Studios --------------------------------------------------------------
echo; echo "  Checking the studios ..."
cd "$ROOT"
"$VPY" - <<'PY'
import sys
sys.path.insert(0, ".")
from hub.tools import TOOLS
from hub.config import tool_dir
from hub import docker as dk
print(f"   {'Docker':<14}", " ".join(dk.docker() or []) or "NOT REACHABLE - install Docker + the NVIDIA Container Toolkit (any studio's linux/initialize.sh does it)")
for t in TOOLS.values():
    d = tool_dir(t.id)
    ok, why = t.installed(d)
    if ok:
        mok, mwhy = t.model_present(d)
        print(f"   {t.name:<14} ready in {d}" + ("" if mok else f"  (models: {mwhy})"))
    else:
        print(f"   {t.name:<14} NOT SET UP - {why}  [{d}]")
PY

# --- 3. App-menu entry ------------------------------------------------------
if (( DESKTOP )); then
  echo; "$HERE/install-desktop.sh"
fi
echo
echo "  Done. Start it with  $HERE/run.sh  (or \"AI – Studio Hub\" in the app menu; or: ai run hub)."
echo
