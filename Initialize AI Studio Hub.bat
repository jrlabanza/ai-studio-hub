@echo off
setlocal EnableExtensions
title AI Studio Hub - setup
cd /d "%~dp0"

:: ===========================================================================
::  AI Studio Hub - Initialize AI Studio Hub.bat
::  Creates the hub's own small Python environment (.venv) and installs its
::  packages (FastAPI, uvicorn, httpx, websockets, psutil, Pillow). No models,
::  no PyTorch: the studios keep their own environments and are set up with
::  their own Initialize scripts. Safe to re-run.
:: ===========================================================================

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\.venv"
set "VPY=%VENV%\Scripts\python.exe"

echo.
echo  AI Studio Hub - setup
echo  ---------------------
echo.

set "BASEPY="
for %%V in (3.11 3.12 3.13 3.10) do (
    if not defined BASEPY (
        py -%%V -c "import sys" >nul 2>&1 && set "BASEPY=py -%%V"
    )
)
if not defined BASEPY (
    where python >nul 2>&1 && set "BASEPY=python"
)
if not defined BASEPY (
    echo  Python 3.10+ was not found. Install it from https://www.python.org/downloads/windows/
    echo  ^(tick "Add python.exe to PATH" and "py launcher"^), then run this script again.
    echo.
    pause
    exit /b 1
)

if not exist "%VPY%" (
    echo  Creating the hub environment in .venv using %BASEPY% ...
    %BASEPY% -m venv "%VENV%" || goto :fail
)

echo  Installing / upgrading packages ...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check || goto :fail
"%VPY%" -m pip install --upgrade -r "%ROOT%\hub\requirements.txt" --quiet --disable-pip-version-check || goto :fail

echo.
echo  Checking the studios ...
"%VPY%" -c "import sys; sys.path.insert(0, r'%ROOT%'); from hub.tools import TOOLS; from hub.config import tool_dir; [print(f'   {t.name:<13}', 'ready in ' + str(tool_dir(t.id)) if t.installed(tool_dir(t.id))[0] else 'NOT FOUND / NOT SET UP - expected at ' + str(tool_dir(t.id)) + ' (change the folder in Settings)') for t in TOOLS.values()]"

echo.
echo  Done. Double-click "Start AI Studio Hub.bat" to open AI Studio Hub.
echo.
pause
exit /b 0

:fail
echo.
echo  Setup failed. Check your internet connection and the messages above, then run this script again.
echo.
pause
exit /b 1
