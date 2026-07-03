@echo off
setlocal EnableDelayedExpansion
title PSX-SCORE  Launcher

REM =====================================================================
REM   PSX-SCORE  -  one-click launcher
REM
REM   EDIT THESE THREE LINES ONCE with your own GitHub details, then
REM   share THIS .bat file with friends. Every time it runs it pulls the
REM   newest code from your GitHub repo, so updates reach everyone
REM   automatically - they never have to re-download anything.
REM =====================================================================
set "GITHUB_USER=umairshahid01"
set "GITHUB_REPO=psx-score"
set "GITHUB_BRANCH=main"
REM =====================================================================

REM ----- pass repo details to the Python app (config.py reads these) ---
set "PSX_GH_USER=%GITHUB_USER%"
set "PSX_GH_REPO=%GITHUB_REPO%"
set "PSX_GH_BRANCH=%GITHUB_BRANCH%"

set "RAW=https://raw.githubusercontent.com/%GITHUB_USER%/%GITHUB_REPO%/%GITHUB_BRANCH%"
set "APPDIR=%LOCALAPPDATA%\PSXScore"

echo.
echo   ===============================================
echo      P S X . S C O R E
echo      Pakistan Stock Exchange Fundamental Scorer
echo   ===============================================
echo.

if "%GITHUB_USER%"=="YOURNAME" (
  echo   [!] Please edit run.bat first:
  echo       open it in Notepad and set GITHUB_USER / GITHUB_REPO / GITHUB_BRANCH
  echo       to point at your own GitHub repository.
  echo.
  pause
  exit /b 1
)

REM --------------------------------------------------------------------
REM  1) Make sure Python is available
REM --------------------------------------------------------------------
set "PY="
where py        >nul 2>&1 && set "PY=py"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )

if not defined PY (
  echo   [*] Python was not found. Trying to install it with winget...
  where winget >nul 2>&1
  if !errorlevel! EQU 0 (
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    echo   [*] Please CLOSE this window and run the file again so Python is on PATH.
    pause
    exit /b 0
  ) else (
    echo   [!] Could not auto-install. Please install Python 3.10+ from:
    echo       https://www.python.org/downloads/   ^(tick "Add python.exe to PATH"^)
    echo.
    pause
    exit /b 1
  )
)
echo   [ok] Python found: %PY%

REM --------------------------------------------------------------------
REM  2) Prepare the local app folder
REM --------------------------------------------------------------------
if not exist "%APPDIR%" mkdir "%APPDIR%"
cd /d "%APPDIR%"
echo   [ok] Working folder: %APPDIR%

REM --------------------------------------------------------------------
REM  3) Pull the latest code from GitHub (every launch)
REM --------------------------------------------------------------------
echo.
echo   [*] Downloading latest version from GitHub...
set "FILES=config.py utils.py psx_data.py scraper.py scorer.py predictor.py deepdata.py analyzer.py dashboard.html requirements.txt"
set "DL_OK=1"

where curl >nul 2>&1
if !errorlevel! EQU 0 (
  for %%F in (%FILES%) do (
    curl -fsSL "%RAW%/%%F" -o "%%F"
    if !errorlevel! NEQ 0 ( echo       [!] failed: %%F & set "DL_OK=0" ) else ( echo       [ok] %%F )
  )
) else (
  for %%F in (%FILES%) do (
    powershell -NoProfile -Command "try{ Invoke-WebRequest -UseBasicParsing '%RAW%/%%F' -OutFile '%%F'; exit 0 }catch{ exit 1 }"
    if !errorlevel! NEQ 0 ( echo       [!] failed: %%F & set "DL_OK=0" ) else ( echo       [ok] %%F )
  )
)

if "!DL_OK!"=="0" (
  echo.
  echo   [!] Some files did not download. Check that the repo is PUBLIC and the
  echo       USER / REPO / BRANCH above are correct. If you have run it before,
  echo       it will now start with the previously downloaded copy.
  echo.
  if not exist "analyzer.py" ( pause & exit /b 1 )
)

REM --------------------------------------------------------------------
REM  4) Install / update Python dependencies
REM --------------------------------------------------------------------
echo.
echo   [*] Checking Python packages (first run can take a minute)...
%PY% -m pip install --quiet --upgrade pip            >nul 2>&1
%PY% -m pip install --quiet -r requirements.txt
if !errorlevel! NEQ 0 (
  echo   [!] Dependency install hit a problem - retrying for current user...
  %PY% -m pip install --user -r requirements.txt
)
echo   [ok] Packages ready.

REM --------------------------------------------------------------------
REM  5) Launch - analyzer.py starts the local server and opens the browser
REM --------------------------------------------------------------------
echo.
echo   ===============================================
echo      Starting PSX.SCORE ...
echo      Your browser will open automatically.
echo      Keep THIS window open while using the app.
echo      Close it (or press Ctrl+C) when you are done.
echo   ===============================================
echo.
%PY% analyzer.py

echo.
echo   PSX.SCORE has stopped.
pause
endlocal
