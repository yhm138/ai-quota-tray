@echo off
setlocal enabledelayedexpansion
title Locate gh.exe
echo.
echo   Looking for gh.exe and git.exe ...
echo.

set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"
set "LAD=%LOCALAPPDATA%"
set "UP=%USERPROFILE%"

echo   -- on PATH (as this cmd window sees it) --
where gh 2>nul || echo      gh  : not on PATH
where git 2>nul || echo      git : not on PATH

echo.
echo   -- on disk --
for %%P in (
    "!PF!\GitHub CLI\gh.exe"
    "!PF86!\GitHub CLI\gh.exe"
    "!LAD!\Programs\GitHub CLI\gh.exe"
    "!LAD!\Microsoft\WinGet\Links\gh.exe"
    "!LAD!\Microsoft\WindowsApps\gh.exe"
    "!UP!\scoop\shims\gh.exe"
    "C:\ProgramData\chocolatey\bin\gh.exe"
) do if exist %%P echo      FOUND %%~P

echo.
echo   -- what PowerShell sees (its PATH may be fresher than this window's) --
powershell -NoProfile -Command "$c = Get-Command gh -ErrorAction SilentlyContinue; if ($c) { '     PowerShell finds gh at: ' + $c.Source } else { '     PowerShell cannot find gh either' }"

echo.
echo   If a path is listed above but publish.bat still says gh is missing,
echo   open a cmd window and run these two lines:
echo.
echo       set GH_PATH=^<the path above^>
echo       publish.bat
echo.
pause
