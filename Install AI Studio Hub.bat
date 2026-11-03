@echo off
setlocal EnableExtensions
title AI Studio Hub - install
cd /d "%~dp0"

:: ===========================================================================
::  AI Studio Hub - Install AI Studio Hub.bat
::  One double-click from nothing to the hub running: installs Git, Python 3.11
::  and the Visual C++ runtime with winget when they are missing, gets the hub
::  and its studios, runs every studio's own Initialize script, sets up the hub
::  ("Initialize AI Studio Hub.bat"), starts it and opens the browser. Video
::  Studio then downloads the one model pack that suits your graphics card.
::
::  Runs install.ps1 from this folder - or, when this file was downloaded on
::  its own, the latest install.ps1 from GitHub, which clones the hub into
::  %USERPROFILE%\AI\ai-studio-hub. Safe to re-run.
::
::  Optional arguments are passed on, for example:
::    "Install AI Studio Hub.bat" -Skip forge      leave a studio out
::    "Install AI Studio Hub.bat" -NoStart         set up only, do not start
::    "Install AI Studio Hub.bat" -DryRun          show every step, change nothing
:: ===========================================================================

if exist "%~dp0install.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; & ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/jrlabanza/ai-studio-hub/main/install.ps1'))) %*"
)
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
