#!/usr/bin/env bash
# Verify the hub: its environment imports, then a real boot on a spare port and
# a check that the shell, the API and every studio entrance answer. Exit 0 = all good.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VPY="$ROOT/.venv/bin/python"; PORT=${HUB_TEST_PORT:-7950}
fail=0
[[ -x $VPY ]] || { echo "not set up: run $HERE/initialize.sh"; exit 1; }
cd "$ROOT"

echo "=== 1/2 environment ==="
out=$("$VPY" -c "
import sys, fastapi, uvicorn, httpx, websockets, psutil, PIL
print('python', '.'.join(map(str, sys.version_info[:3])))
print('fastapi', fastapi.__version__, 'uvicorn', uvicorn.__version__, 'httpx', httpx.__version__)
from hub.tools import TOOLS; from hub.config import tool_dir
for t in TOOLS.values():
    ok, why = t.installed(tool_dir(t.id)); print(('ok  ' if ok else 'MISSING ') + t.id, '' if ok else '- ' + why)
" 2>&1); echo "$out" | sed 's/^/  /'
grep -q "^python 3\.1[0-9]" <<< "$out" || { echo "  >> WRONG PYTHON"; fail=1; }
grep -q "Traceback" <<< "$out" && fail=1

echo "=== 2/2 boot ==="
"$VPY" -m hub --port "$PORT" --no-browser --log-level error > "$ROOT/data/logs/hub-test.log" 2>&1 &
pid=$!
t0=$(date +%s); code=""
while (( $(date +%s) - t0 < 60 )); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/api/state" 2>/dev/null)
  [[ $code == 200 ]] && break; sleep 1
done
if [[ $code == 200 ]]; then
  echo "  hub UP in $(( $(date +%s) - t0 ))s on port $PORT"
  state=$(curl -s "http://127.0.0.1:$PORT/api/state")
  "$VPY" -c "
import json, sys
st = json.loads(sys.stdin.read())
print('  gpu:', st['system']['gpu'].get('name') or 'none', '·', st['orchestrator']['policy'])
for tid in st['order']:
    t = st['tools'][tid]
    print(f\"  {t['name']:<14} entrance :{t['proxy_port']}  backend :{t['port']}  {t['backend'] or '-':<6} {'installed' if t['installed'] else 'NOT SET UP: ' + t['install_note']}\")
" <<< "$state"
  for p in $("$VPY" -c "import json,sys; st=json.loads(sys.stdin.read()); print(' '.join(str(st['tools'][t]['proxy_port']) for t in st['order']))" <<< "$state"); do
    c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -H 'Accept: application/json' "http://127.0.0.1:$p/__hub/state")
    [[ $c == 200 ]] || { echo "  >> entrance :$p does not answer (http=$c)"; fail=1; }
  done
else
  echo "  FAILED (http=$code)"; tail -15 "$ROOT/data/logs/hub-test.log" | sed 's/^/  | /'; fail=1
fi
kill -TERM $pid 2>/dev/null; wait $pid 2>/dev/null
echo; (( fail )) && echo "RESULT: FAIL" || echo "RESULT: PASS"
exit $fail
