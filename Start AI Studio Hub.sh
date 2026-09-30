#!/usr/bin/env bash
# AI Studio Hub - Start AI Studio Hub.sh  (the Linux twin of "Start AI Studio Hub.bat")
# Starts the hub that hosts the studios in one place and shares the GPU between them.
# Run "Initialize AI Studio Hub.sh" once first. Optional arguments are passed on:
#   --port 7900      another hub port
#   --host 0.0.0.0   let other devices on your network use it
#   --no-browser     do not open the browser
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/linux/run.sh" "$@"
