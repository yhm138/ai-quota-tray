@echo off
REM Update this QuotaTray copy to the latest release and restart it.
REM Works for source checkouts and the exe/portable builds alike.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" -InstallDir "%~dp0."
echo.
pause
