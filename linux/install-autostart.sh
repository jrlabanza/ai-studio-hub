#!/usr/bin/env bash
# Start AI Studio Hub when you log in (a systemd user service), so http://localhost:7900 is there after a reboot.
#
#   ./install-autostart.sh            install and enable it (starts at the next login; --now starts it too)
#   ./install-autostart.sh --remove   disable and remove it
#
# The hub starts idle: studios still start on demand (or by their own "autostart" setting). Logging out or shutting
# down stops the hub, and it stops the studios it started. Its console log goes to: journalctl --user -u ai-studio-hub
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=ai-studio-hub.service
DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

command -v systemctl >/dev/null || { echo "  no systemd here: start the hub with $HERE/run.sh --background"; exit 1; }

if [[ ${1:-} == --remove ]]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$DIR/$UNIT"
  systemctl --user daemon-reload
  echo "  autostart removed"
  exit 0
fi

mkdir -p "$DIR"
cat > "$DIR/$UNIT" <<U
[Unit]
Description=AI Studio Hub (http://localhost:7900)

[Service]
Type=simple
ExecStart=$HERE/run.sh --no-open
# SIGTERM: the hub stops the studios it started, which can take a while for a busy container
TimeoutStopSec=180
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
U
systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null
echo "  autostart on: the hub starts when you log in (remove: $HERE/install-autostart.sh --remove)"
if [[ ${1:-} == --now ]]; then
  if curl -sf -o /dev/null --max-time 2 http://localhost:7900/api/state 2>/dev/null; then
    echo "  the hub is already running outside the service; it is used as is (the service takes over after a reboot)"
  else
    systemctl --user start "$UNIT" && echo "  started: http://localhost:7900"
  fi
fi
