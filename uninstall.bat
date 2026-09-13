@echo off
setlocal
cd /d "%~dp0"
echo Uninstalling QuotaTray ...

reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v QuotaTray /f >nul 2>nul
echo   - run-at-login entry removed

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run.pyw*' -or $_.CommandLine -like '*QuotaTray*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
echo   - running processes stopped

echo.
set /p DELCFG="Also delete config and logs in %%APPDATA%%\QuotaTray? [y/N] "
if /i "%DELCFG%"=="y" (
    rd /s /q "%APPDATA%\QuotaTray" 2>nul
    echo   - config directory deleted
)
echo.
echo The program files are still in this folder - delete it when you are done.
echo.
pause
