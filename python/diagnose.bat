@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Not installed yet - run install.bat first.
    pause
    exit /b 1
)
echo Probing every quota source, please wait ...
echo.
".venv\Scripts\python.exe" "%~dp0run.pyw" --diagnose
echo.
echo ---- [FAIL] lines are sources that are unavailable ----
echo ---- [OK]   is the source actually being used      ----
pause
