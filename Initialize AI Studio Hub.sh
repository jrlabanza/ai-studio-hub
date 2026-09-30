#!/usr/bin/env bash
# AI Studio Hub - Initialize AI Studio Hub.sh  (the Linux twin of "Initialize AI Studio Hub.bat")
# Creates the hub's own small Python environment (.venv), checks the studios and adds
# an app-menu entry. No models, no PyTorch: the studios keep their own containers and
# are set up with their own linux/initialize.sh. Safe to re-run.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/linux/initialize.sh" "$@"
