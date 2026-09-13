@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title QuotaTray - publish to GitHub

echo.
echo   ==================================================
echo    Publish QuotaTray to GitHub
echo   ==================================================
echo.

REM ================================================================
REM  Locate git and gh WITHOUT trusting PATH.
REM
REM  A cmd window opened by double-clicking a .bat inherits explorer's
REM  cached environment, which does not pick up PATH changes made by a
REM  recent install until you sign out and back in. So "where gh" can
REM  fail on a machine where gh works fine in PowerShell.
REM
REM  Override either one before running if they live somewhere unusual:
REM      set GH_PATH=C:\full\path\to\gh.exe
REM      set GIT_PATH=C:\full\path\to\git.exe
REM
REM  Note: every candidate below is expanded with !delayed! expansion.
REM  "C:\Program Files (x86)\..." contains a ')' and would otherwise close
REM  the surrounding for-set early at parse time.
REM ================================================================

set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"
set "LAD=%LOCALAPPDATA%"
set "UP=%USERPROFILE%"

REM ---------------------------------------------------------------- git
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
    echo   [X] git could not be found.
    echo.
    echo       Checked PATH and:
    echo         !PF!\Git\cmd\git.exe
    echo         !LAD!\Programs\Git\cmd\git.exe
    echo         !LAD!\Microsoft\WinGet\Links\git.exe
    echo.
    echo       Install it:   winget install Git.Git
    echo       Or find it with  where git  and rerun after:
    echo         set GIT_PATH=C:\full\path\to\git.exe
    echo.
    pause
    exit /b 1
)
set "GITDIR="
for %%D in ("!GIT!") do set "GITDIR=%%~dpD"
echo   git: !GIT!

REM ---------------------------------------------------------------- gh
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
set "GHDIR="
if defined GH for %%D in ("!GH!") do set "GHDIR=%%~dpD"

REM Put both on PATH so gh can shell out to git internally.
set "PATH=!GITDIR!;!GHDIR!;%PATH%"

set "MANUAL_URL="
if defined GH (
    echo   gh:  !GH!
) else (
    echo.
    echo   [-] The GitHub CLI was not found on PATH or in the usual places:
    echo         !PF!\GitHub CLI\gh.exe
    echo         !LAD!\Microsoft\WinGet\Links\gh.exe
    echo         !LAD!\Programs\GitHub CLI\gh.exe
    echo.
    echo       If gh works in PowerShell, run there:   where.exe gh
    echo       then rerun this script from a cmd window after:
    echo         set GH_PATH=C:\full\path\to\gh.exe
    echo.
    echo       Or publish with plain git instead: create an EMPTY repository
    echo       at https://github.com/new  - no README, no .gitignore, no
    echo       licence - and paste its URL here.
    echo.
    set /p "MANUAL_URL=Repository URL, or press Enter to abort: "
    if not defined MANUAL_URL (
        echo   Aborted.
        pause
        exit /b 1
    )
)

REM ---------------------------------------------------------------- account
set "OWNER="
set "REPO=QuotaTray"
if defined GH (
    "!GH!" auth status >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   [X] gh is installed but not signed in to this account.
        echo       Run:  gh auth login
        echo       Choose HTTPS and "Login with a web browser" - no password needed.
        echo.
        pause
        exit /b 1
    )
    for /f "usebackq delims=" %%i in (`"!GH!" api user --jq .login 2^>nul`) do set "OWNER=%%i"
    if not defined OWNER (
        echo   [X] Could not read your GitHub username. Try:  gh auth login
        pause
        exit /b 1
    )
    echo   account: !OWNER!
    echo.
    set /p "REPO=Repository name [QuotaTray]: "
    if "!REPO!"=="" set "REPO=QuotaTray"
) else (
    REM https://github.com/OWNER/REPO - for /f collapses the double slash,
    REM so the tokens are: 1=https:  2=github.com  3=owner  4=repo
    for /f "tokens=3 delims=/" %%i in ("!MANUAL_URL!") do set "OWNER=%%i"
    for %%i in ("!MANUAL_URL!") do set "REPO=%%~ni"
    echo   account: !OWNER!   repository: !REPO!   (parsed from the URL)
)

echo.
if defined GH (
    echo   About to create the PUBLIC repository:  !OWNER!/!REPO!
) else (
    echo   About to push to the existing repository:  !MANUAL_URL!
)
echo   Source folder: %~dp0
echo.
set "GO="
set /p "GO=Continue? [y/N] "
if /i not "!GO!"=="y" (
    echo   Cancelled.
    pause
    exit /b 0
)

REM ---------------------------------------------------------------- python
set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if defined PYEXE (
    echo.
    echo   [1/6] Writing the GitHub Actions workflows ...
    !PYEXE! tools\install_workflows.py
    echo.
    echo   [2/6] Pointing the README badges at !OWNER!/!REPO! ...
    !PYEXE! tools\set_repo_owner.py "!OWNER!" "!REPO!"
) else (
    echo   [1-2/6] Skipped - no Python found.
    echo           The Actions workflows and README badges will need doing by hand.
)

REM ---------------------------------------------------------------- git repo
echo.
echo   [3/6] Preparing the git repository ...
if not exist ".git" (
    "!GIT!" init -b main
    if errorlevel 1 (
        "!GIT!" init
        "!GIT!" checkout -b main
    )
) else (
    echo         (already a git repository)
)

REM Repo-local identity only, so a missing global config cannot block the
REM commit and the machine's global git setup is left alone.
"!GIT!" config user.name >nul 2>nul || "!GIT!" config user.name "!OWNER!"
"!GIT!" config user.email >nul 2>nul || "!GIT!" config user.email "!OWNER!@users.noreply.github.com"

echo.
echo   [4/6] Staging files (.gitignore keeps .venv, dist and _probe.txt out) ...
"!GIT!" add -A
"!GIT!" status --short

echo.
"!GIT!" diff --cached --quiet
if not errorlevel 1 (
    echo         Nothing new to commit.
) else (
    "!GIT!" commit -q -m "QuotaTray: Windows tray monitor for Claude, Codex and Antigravity quotas" -m "Multi-source fallback collection per provider, diagnostics view, run-at-login."
    echo         Committed.
)

REM ---------------------------------------------------------------- publish
echo.
echo   [5/6] Creating the repository and pushing ...
"!GIT!" remote get-url origin >nul 2>nul
if errorlevel 1 (
    if defined GH (
        "!GH!" repo create "!OWNER!/!REPO!" --public --source=. --remote=origin --push --description "Windows 11 tray app showing Claude, Codex and Antigravity IDE quota usage"
        if errorlevel 1 (
            echo   [X] gh repo create failed. If the name is taken, rerun with a different one.
            pause
            exit /b 1
        )
    ) else (
        "!GIT!" remote add origin "!MANUAL_URL!"
        "!GIT!" push -u origin main
        if errorlevel 1 (
            echo   [X] push failed. Check the URL, and that the repository is empty.
            pause
            exit /b 1
        )
    )
) else (
    echo         origin already exists, pushing instead
    "!GIT!" push -u origin main
)

REM ---------------------------------------------------------------- release
echo.
echo   [6/6] Release
echo         Tagging a version runs the GitHub Actions build, which attaches
echo         QuotaTray.exe, the portable zip and SHA256SUMS.txt to the release.
echo.
set "TAGIT="
set /p "TAGIT=Tag v1.0.0 and start the release build now? [y/N] "
if /i "!TAGIT!"=="y" (
    "!GIT!" tag v1.0.0
    "!GIT!" push origin v1.0.0
    echo.
    echo         Build started. Watch it here:
    echo         https://github.com/!OWNER!/!REPO!/actions
)

echo.
echo   Done:  https://github.com/!OWNER!/!REPO!
echo.
pause
