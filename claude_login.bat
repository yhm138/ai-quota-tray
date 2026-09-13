@echo off
setlocal
REM ---------------------------------------------------------------
REM  Sign in to Claude Code through the RIGHT local proxy port.
REM  Change PROXY_PORT below if your proxy tool uses a different one.
REM  (Clash Verge default mixed port = 7897, older Clash = 7890)
REM ---------------------------------------------------------------
set "PROXY_PORT=7897"

echo Current proxy variables:
echo   HTTP_PROXY  = %HTTP_PROXY%
echo   HTTPS_PROXY = %HTTPS_PROXY%
echo   ALL_PROXY   = %ALL_PROXY%
echo.
echo Overriding them to 127.0.0.1:%PROXY_PORT% for this window only ...

set "HTTP_PROXY=http://127.0.0.1:%PROXY_PORT%"
set "HTTPS_PROXY=http://127.0.0.1:%PROXY_PORT%"
set "http_proxy=http://127.0.0.1:%PROXY_PORT%"
set "https_proxy=http://127.0.0.1:%PROXY_PORT%"
set "ALL_PROXY="
set "all_proxy="
set "NO_PROXY=localhost,127.0.0.1,::1"
set "no_proxy=localhost,127.0.0.1,::1"

echo.
echo Checking that the proxy actually answers on %PROXY_PORT% ...
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', %PROXY_PORT%); $c.Close(); '  OK - port %PROXY_PORT% is open' } catch { '  FAILED - nothing is listening on 127.0.0.1:%PROXY_PORT%' }"
echo.

where claude >nul 2>nul
if errorlevel 1 (
    echo [X] 'claude' is not on PATH in this window.
    echo     Open the shell where 'claude' works and run these three lines:
    echo.
    echo       set HTTPS_PROXY=http://127.0.0.1:%PROXY_PORT%
    echo       set HTTP_PROXY=http://127.0.0.1:%PROXY_PORT%
    echo       claude
    echo.
    pause
    exit /b 1
)

echo Starting Claude Code. Use /login inside it to sign in to your subscription.
echo.
claude
