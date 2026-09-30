#!/usr/bin/env bash
# App-menu entry for AI Studio Hub: a console window, close it to stop the hub
# and the studios it started (the same behaviour as the Windows .bat).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
A="$HOME/.local/share/applications"; mkdir -p "$A"
cat > "$A/ai-studio-hub.desktop" <<D
[Desktop Entry]
Type=Application
Name=AI – Studio Hub
Comment=One home for the local AI studios with a GPU auto-loader (localhost:7900)
Icon=applications-science
Exec=$HERE/run.sh
Terminal=true
Categories=Graphics;AudioVideo;
Keywords=AI;generation;hub;
StartupNotify=false
D
command -v update-desktop-database >/dev/null && update-desktop-database "$A" || true
echo "  app-menu entry installed: AI – Studio Hub"
