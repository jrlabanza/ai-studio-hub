#!/usr/bin/env bash
# Stop AI Studio Hub. The hub stops every studio it started on its way out
# ("Stop all studios when the hub closes" in Settings, on by default).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PIDFILE="$ROOT/data/hub.pid"
pid=""
[[ -f $PIDFILE ]] && pid=$(<"$PIDFILE")
if [[ -z $pid ]] || ! kill -0 "$pid" 2>/dev/null; then
  pid=$(pgrep -u "$(id -u)" -f "$ROOT/.venv/bin/python -m hub" | head -1)
fi
if [[ -z $pid ]]; then echo "AI Studio Hub is not running"; exit 0; fi
echo "==> stopping AI Studio Hub (pid $pid)"
kill -TERM "$pid" 2>/dev/null
for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || { echo "    stopped"; exit 0; }; sleep 1; done
echo "    still running after 60 s - killing"; kill -KILL "$pid" 2>/dev/null; rm -f "$PIDFILE"
