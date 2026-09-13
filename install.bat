@echo off
setlocal
cd /d "%~dp0"
title QuotaTray Setup

echo.
echo   ==================================================
echo    QuotaTray - AI quota tray monitor  ^|  Setup
echo   ==================================================
echo.

set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo   [X] Python was not found.
    echo       Install Python 3.10+ first:  winget install Python.Python.3.12
    echo       Tick "Add python.exe to PATH" and "tcl/tk and IDLE" during setup.
    echo.
    pause
    exit /b 1
)
echo   [1/4] Python found: %PY%

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo   [X] This Python has no tkinter, so the panel cannot be shown.
    echo       Reinstall Python with the "tcl/tk and IDLE" option enabled.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo   [2/4] Creating the .venv virtual environment ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo   [X] Could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo   [2/4] Virtual environment already exists, skipping.
)

echo   [3/4] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q --disable-pip-version-check
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo   [!] Default index failed, retrying via the Tsinghua mirror ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
)
".venv\Scripts\python.exe" -c "import pystray, PIL, requests, cryptography" >nul 2>nul
if errorlevel 1 (
    echo   [X] Dependencies are incomplete. Send the errors above to your admin.
    pause
    exit /b 1
)

echo   [4/4] Starting (the first run also enables run-at-login) ...
start "" ".venv\Scripts\pythonw.exe" "%~dp0run.pyw"

echo.
echo   Done. Look for the QuotaTray icon in the system tray and click it.
echo   If it is hidden, click the "^" arrow on the taskbar and drag it out.
echo.
echo   Troubleshooting: run diagnose.bat to see every quota source probe.
echo.
timeout /t 8 >nul
