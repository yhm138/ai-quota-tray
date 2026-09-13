@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title QuotaTray - finish the push

echo.
echo   ==================================================
echo    Finish pushing QuotaTray to GitHub
echo   ==================================================
echo.
echo   The local commit already exists; only the push failed.
echo   This script shows the real error instead of hiding it.
echo.

set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"
set "LAD=%LOCALAPPDATA%"
set "UP=%USERPROFILE%"

REM ---------------------------------------------------------------- locate
set "GIT="
if defined GIT_PATH if exist "%GIT_PATH%" set "GIT=%GIT_PATH%"
if not defined GIT (
    for /f "delims=" %%i in ('where git 2^>nul') do if not defined GIT set "GIT=%%i"
)
if not defined GIT (
    for %%P in (
        "!PF!\Git\cmd\git.exe"
        "!PF86!\Git\cmd\git.exe"
        "!LAD!\Programs\Git\cmd\git.exe"
        "!LAD!\Microsoft\WinGet\Links\git.exe"
        "!UP!\scoop\shims\git.exe"
        "C:\ProgramData\chocolatey\bin\git.exe"
    ) do if not defined GIT if exist %%P set "GIT=%%~P"
)
if not defined GIT (
    echo   [X] git not found. Run find_gh.bat first.
    pause
    exit /b 1
)

set "GH="
if defined GH_PATH if exist "%GH_PATH%" set "GH=%GH_PATH%"
if not defined GH (
    for /f "delims=" %%i in ('where gh 2^>nul') do if not defined GH set "GH=%%i"
)
if not defined GH (
    for %%P in (
        "!PF!\GitHub CLI\gh.exe"
        "!PF86!\GitHub CLI\gh.exe"
        "!LAD!\Programs\GitHub CLI\gh.exe"
        "!LAD!\Microsoft\WinGet\Links\gh.exe"
        "!LAD!\Microsoft\WindowsApps\gh.exe"
        "!UP!\scoop\shims\gh.exe"
        "C:\ProgramData\chocolatey\bin\gh.exe"
    ) do if not defined GH if exist %%P set "GH=%%~P"
)

set "GITDIR="
for %%D in ("!GIT!") do set "GITDIR=%%~dpD"
set "GHDIR="
if defined GH for %%D in ("!GH!") do set "GHDIR=%%~dpD"
set "PATH=!GITDIR!;!GHDIR!;%PATH%"

REM ---------------------------------------------------------------- state
echo   -- current state --
echo.
echo   last commit:
"!GIT!" log --oneline -1 2>nul || echo      (no commits - run publish.bat first)
echo.
echo   remotes:
"!GIT!" remote -v
echo.
echo   branch:
"!GIT!" rev-parse --abbrev-ref HEAD
echo.
echo   files in the commit:
for /f %%i in ('"!GIT!" ls-files ^| find /c /v ""') do echo      %%i tracked files
echo.

set "BRANCH="
for /f "delims=" %%i in ('"!GIT!" rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%i"
if not defined BRANCH set "BRANCH=main"

"!GIT!" remote get-url origin >nul 2>nul
if errorlevel 1 (
    echo   [X] No 'origin' remote. Run publish.bat first, or add it by hand:
    echo       git remote add origin https://github.com/USER/REPO.git
    pause
    exit /b 1
)

REM ------------------------------------------------- credential helper
if defined GH (
    echo   -- configuring git's credential helper for github.com --
    echo      ^(this is the step that is usually missing: gh auth login does
    echo       not always run it, so HTTPS pushes get rejected^)
    "!GH!" auth setup-git
    if errorlevel 1 (
        echo      [-] gh auth setup-git failed. You may need:
        echo          gh auth refresh -h github.com -s repo
    ) else (
        echo      done.
    )
    echo.
)

REM ---------------------------------------------------------------- push
echo   -- pushing !BRANCH! to origin (full output below) --
echo.
"!GIT!" push -u origin "!BRANCH!"
set "RC=!ERRORLEVEL!"
echo.

if not "!RC!"=="0" (
    echo   [X] push failed with exit code !RC!.
    echo.
    echo       Common causes and fixes:
    echo.
    echo       * Credentials rejected
    echo           gh auth refresh -h github.com -s repo
    echo           gh auth setup-git
    echo         then run this script again.
    echo.
    echo       * The remote is not empty / histories differ
    echo           git push -u origin !BRANCH! --force
    echo         Safe here only because the remote repository is empty.
    echo.
    echo       * Proxy or network. Your proxy port changed once before:
    echo           set HTTPS_PROXY=http://127.0.0.1:7897
    echo         then run this script again in that same window.
    echo.
    echo       * Switch to SSH instead of HTTPS
    echo           gh auth login --git-protocol ssh
    echo           git remote set-url origin git@github.com:USER/REPO.git
    echo.
    pause
    exit /b 1
)

echo   -- verifying the remote actually has the commit --
"!GIT!" ls-remote --heads origin "!BRANCH!"
if errorlevel 1 (
    echo   [X] Could not read the remote back.
    pause
    exit /b 1
)
echo.
echo   Pushed successfully.
echo.

REM ---------------------------------------------------------------- release
set "URL="
for /f "delims=" %%i in ('"!GIT!" remote get-url origin 2^>nul') do set "URL=%%i"
echo   Repository: !URL!
echo.
set "TAGIT="
set /p "TAGIT=Tag v1.0.0 and start the release build now? [y/N] "
if /i "!TAGIT!"=="y" (
    "!GIT!" tag v1.0.0 2>nul
    "!GIT!" push origin v1.0.0
    if errorlevel 1 (
        echo   [-] Tag push failed. If v1.0.0 already exists remotely, pick a new version.
    ) else (
        echo.
        echo   Build started. Watch it under the Actions tab of the repository.
    )
)

echo.
pause
