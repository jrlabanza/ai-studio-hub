@echo off
setlocal EnableExtensions
title AI Studio Hub
cd /d "%~dp0"

:: ===========================================================================
::  AI Studio Hub - Start AI Studio Hub.bat
::  Starts the hub that hosts Image, Voice, Video and Music Studio in one place
::  and shares the GPU between them. Run "Initialize AI Studio Hub.bat" once first.
::
::  Optional arguments are passed to the hub, for example:
::    "Start AI Studio Hub.bat" --port 7900       another hub port
::    "Start AI Studio Hub.bat" --host 0.0.0.0    let other devices on your network use it
::    "Start AI Studio Hub.bat" --no-browser      do not open the browser
:: ===========================================================================

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "VPY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%VPY%" (
    echo.
    echo  AI Studio Hub is not set up yet.
    echo  Run "Initialize AI Studio Hub.bat" first - it installs the hub's small Python environment.
    echo.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

echo.
echo  Starting AI Studio Hub ...  (close this window to stop the hub and every studio it started)
echo.
"%VPY%" -m hub %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo  The hub stopped with exit code %RC%. See the messages above.
    echo  If packages look broken, re-run "Initialize AI Studio Hub.bat".
    echo.
    pause
)
exit /b %RC%
