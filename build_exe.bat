@echo off
setlocal
cd /d "%~dp0"
echo Building a single-file EXE (colleagues will not need Python) ...
if not exist ".venv\Scripts\python.exe" (
    echo Run install.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller -q --disable-pip-version-check
".venv\Scripts\python.exe" tools\make_icon.py
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name QuotaTray ^
    --icon assets\quotatray.ico ^
    --hidden-import pystray._win32 ^
    --hidden-import PIL._tkinter_finder ^
    run.pyw
echo.
echo Done: dist\QuotaTray.exe
echo Note: the EXE enables run-at-login on its first run, pointing at wherever
echo       it sits at that moment. Move it to its final home (e.g.
echo       C:\Tools\QuotaTray\) BEFORE running it the first time.
pause
