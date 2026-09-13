@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title QuotaTray - cut a release

echo.
echo   ==================================================
echo    Cut a QuotaTray release
echo   ==================================================
echo.
echo   Pushing a version tag runs the GitHub Actions build, which
echo   attaches QuotaTray.exe, QuotaTray-portable.zip and
echo   SHA256SUMS.txt to the release.
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
    echo   [X] git not found. Run find_gh.bat.
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

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3"
)

REM ---------------------------------------------------------------- preflight
echo   -- preflight --
echo.

"!GIT!" rev-parse --git-dir >nul 2>nul
if errorlevel 1 (
    echo   [X] Not a git repository. Run publish.bat first.
    pause
    exit /b 1
)

set "ORIGIN="
for /f "delims=" %%i in ('"!GIT!" remote get-url origin 2^>nul') do set "ORIGIN=%%i"
if not defined ORIGIN (
    echo   [X] No 'origin' remote. Run publish.bat first.
    pause
    exit /b 1
)
echo      repository : !ORIGIN!

REM Refresh the workflow files, then make sure they are actually committed.
REM Tagging a commit that has no workflow in it produces no build at all.
if defined PYEXE (
    !PYEXE! tools\install_workflows.py >nul 2>nul
)
"!GIT!" ls-files --error-unmatch .github/workflows/build.yml >nul 2>nul
if errorlevel 1 (
    echo   [X] .github/workflows/build.yml is not tracked by git.
    echo       Without it, a tag will not build anything. Fix with:
    echo         git add .github/workflows
    echo         git commit -m "add build workflow"
    echo         git push
    pause
    exit /b 1
)
echo      workflow   : .github/workflows/build.yml is committed

REM Commit anything still pending, so the tag points at a complete tree.
"!GIT!" add -A
"!GIT!" diff --cached --quiet
if errorlevel 1 (
    echo      pending    : uncommitted changes found, committing them now
    "!GIT!" commit -q -m "Prepare release"
    "!GIT!" push origin main
    if errorlevel 1 (
        echo   [X] push failed. Run fix_push.bat.
        pause
        exit /b 1
    )
) else (
    echo      pending    : working tree clean
)

REM Local main must match what is on the remote.
set "LOCAL="
set "REMOTE="
for /f "delims=" %%i in ('"!GIT!" rev-parse main 2^>nul') do set "LOCAL=%%i"
for /f "tokens=1" %%i in ('"!GIT!" ls-remote origin refs/heads/main 2^>nul') do set "REMOTE=%%i"
if not "!LOCAL!"=="!REMOTE!" (
    echo      [-] local main and origin/main differ, pushing ...
    "!GIT!" push origin main
    if errorlevel 1 (
        echo   [X] push failed. Run fix_push.bat.
        pause
        exit /b 1
    )
)
echo      commit     : !LOCAL!
echo.

REM ---------------------------------------------------------------- version
set "VER=v1.0.0"
set /p "VER=Version tag [v1.0.0]: "
if "!VER!"=="" set "VER=v1.0.0"
echo !VER! | findstr /r "^v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$" >nul
if errorlevel 1 (
    echo   [X] "!VER!" is not of the form vMAJOR.MINOR.PATCH, for example v1.0.0
    pause
    exit /b 1
)

REM Already used?
"!GIT!" rev-parse "!VER!" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo   [-] Tag !VER! already exists locally.
    set "REDO="
    set /p "REDO=Delete it locally and remotely, then re-tag? [y/N] "
    if /i "!REDO!"=="y" (
        "!GIT!" tag -d "!VER!" >nul 2>nul
        "!GIT!" push origin ":refs/tags/!VER!" >nul 2>nul
        if defined GH "!GH!" release delete "!VER!" --yes >nul 2>nul
        echo       removed.
    ) else (
        echo   Pick a different version and run this again.
        pause
        exit /b 1
    )
)

echo.
echo   Tagging !VER! at !LOCAL!
echo.

"!GIT!" tag -a "!VER!" -m "QuotaTray !VER!"
if errorlevel 1 (
    echo   [X] Could not create the tag.
    pause
    exit /b 1
)

"!GIT!" push origin "!VER!"
if errorlevel 1 (
    echo   [X] Pushing the tag failed.
    echo       Remove the local tag with:  git tag -d !VER!
    echo       then check credentials and try again.
    pause
    exit /b 1
)

echo.
echo   Tag pushed. The build is starting.
echo.

REM ---------------------------------------------------------------- watch
if defined GH (
    echo   -- waiting for the workflow to appear --
    timeout /t 8 >nul
    "!GH!" run list --workflow build.yml --limit 3
    echo.
    set "WATCH="
    set /p "WATCH=Watch the build live? It takes a few minutes. [Y/n] "
    if /i not "!WATCH!"=="n" (
        echo.
        "!GH!" run watch
        echo.
        "!GH!" release view "!VER!"
    )
) else (
    echo   Watch the build under the Actions tab of your repository.
)

echo.
echo   When the build finishes, the release will carry:
echo     QuotaTray.exe            single file
echo     QuotaTray-portable.zip   folder build, starts faster, fewer AV hits
echo     SHA256SUMS.txt           checksums for both
echo.
pause
