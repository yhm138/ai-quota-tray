@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Not installed yet - run install.bat first.
    pause
    exit /b 1
)
echo Collecting a diagnostic report, this takes a few seconds ...
echo.
".venv\Scripts\python.exe" tools\probe.py
echo.
echo Tokens and cookies are redacted in the report.
pause
