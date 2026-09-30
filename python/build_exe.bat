@echo off
setlocal
cd /d "%~dp0"
echo Building a single-file EXE (colleagues will not need Python) ...
if not exist ".venv\Scripts\python.exe" (
    echo Run install.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6.11" -q --disable-pip-version-check
".venv\Scripts\python.exe" tools\make_icon.py
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole --optimize 2 ^
    --name QuotaTray ^
    --icon ..\assets\quotatray.ico ^
    --hidden-import pystray._win32 ^
    --exclude-module cryptography ^
    --exclude-module cffi ^
    --exclude-module curl_cffi ^
    --exclude-module PIL._avif ^
    --exclude-module PIL.AvifImagePlugin ^
    --exclude-module PIL._webp ^
    --exclude-module PIL.WebPImagePlugin ^
    --exclude-module PIL._imagingft ^
    --exclude-module PIL._imagingcms ^
    --exclude-module PIL.ImageCms ^
    --exclude-module PIL._imagingmath ^
    --exclude-module PIL._imagingmorph ^
    --exclude-module PIL.ImageTk ^
    --exclude-module PIL._tkinter_finder ^
    --exclude-module PIL.ImageQt ^
    --exclude-module setuptools ^
    --exclude-module pkg_resources ^
    --exclude-module unittest ^
    --exclude-module pydoc ^
    --exclude-module doctest ^
    --exclude-module pdb ^
    --exclude-module lib2to3 ^
    --exclude-module xmlrpc ^
    --exclude-module tkinter.test ^
    --exclude-module test ^
    run.pyw
echo.
echo Done: dist\QuotaTray.exe
echo Note: the EXE enables run-at-login on its first run, pointing at wherever
echo       it sits at that moment. Move it to its final home (e.g.
echo       C:\Tools\QuotaTray\) BEFORE running it the first time.
pause
