@echo off
setlocal EnableDelayedExpansion
title PSX-SCORE Launcher

REM ============================================================
REM  PSX-SCORE  -  one-click Windows launcher (desktop, LIVE)
REM  Double-click this file. It will:
REM    1) make sure Python is installed (installs it via winget if not)
REM    2) download the latest app files from your GitHub repo
REM    3) install the Python packages it needs
REM    4) start the local engine and open the dashboard in your browser
REM  Keep the black window open while using the app; close it to stop.
REM
REM  Share ONLY this .bat with colleagues. As long as the repo below is
REM  public, their copy pulls the same code and runs the same way. When
REM  you push an update to GitHub, they get it on their next launch.
REM ============================================================

REM ----------------------- CONFIG -----------------------------
set "GITHUB_USER=Umairshahid01"
set "GITHUB_REPO=psx-score"
set "GITHUB_BRANCH=main"
set "PORT=5000"
REM Files the app needs at runtime (engine + scoring modules + the desktop HTML):
set "FILES=api.py config.py utils.py psx_data.py scraper.py scorer.py requirements.txt dashboard.html"
REM ------------------------------------------------------------

set "BASE=https://raw.githubusercontent.com/%GITHUB_USER%/%GITHUB_REPO%/%GITHUB_BRANCH%"
set "APPDIR=%LOCALAPPDATA%\psx-score"

echo.
echo  ================================================
echo     PSX-SCORE   -   starting up
echo  ================================================
echo.

REM ---------------- 1) Ensure Python --------------------------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )

if not defined PY (
  echo  Python was not found. Trying to install it automatically via winget...
  where winget >nul 2>nul
  if !errorlevel! EQU 0 (
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    REM winget does not refresh PATH for this window, so add the usual install paths:
    set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
    where python >nul 2>nul && set "PY=python"
  )
)

if not defined PY (
  echo.
  echo  Could not find or auto-install Python.
  echo  Please install Python 3.12 from:
  echo      https://www.python.org/downloads/
  echo  During setup, tick "Add python.exe to PATH". Then run this file again.
  echo.
  pause
  exit /b 1
)

echo  Using Python:
%PY% --version
echo.

REM ---------------- 2) Get latest code ------------------------
if not exist "%APPDIR%" mkdir "%APPDIR%"
cd /d "%APPDIR%"

echo  Downloading the latest app files from GitHub...
for %%F in (%FILES%) do (
  powershell -NoProfile -Command "try{ Invoke-WebRequest -UseBasicParsing -Uri '%BASE%/%%F' -OutFile '%%F'; exit 0 }catch{ exit 1 }"
  if !errorlevel! NEQ 0 echo     - could not download %%F
)

if not exist "api.py" (
  echo.
  echo  Download failed - api.py is missing.
  echo  Check your internet connection and that the repo is public:
  echo      https://github.com/%GITHUB_USER%/%GITHUB_REPO%
  echo.
  pause
  exit /b 1
)
if not exist "dashboard.html" (
  echo.
  echo  Download failed - dashboard.html is missing.
  echo  Make sure dashboard.html has been pushed to the repo.
  echo.
  pause
  exit /b 1
)
echo.

REM ---------------- 3) Install dependencies -------------------
echo  Installing Python packages ^(first run can take a couple of minutes^)...
%PY% -m pip install --upgrade pip --quiet
%PY% -m pip install -r requirements.txt --quiet
if %errorlevel% NEQ 0 (
  echo.
  echo  Package install hit a problem. Retrying so you can see why...
  %PY% -m pip install -r requirements.txt
)
echo.

REM ---------------- 4) Launch ---------------------------------
echo  ------------------------------------------------
echo   Opening PSX-SCORE at  http://127.0.0.1:%PORT%
echo   Keep THIS window open while you use the app.
echo   Close it ^(or press Ctrl+C^) to stop.
echo  ------------------------------------------------
echo.
echo  Note: the first time, Windows may ask to "Allow access" for Python
echo  through the firewall - click Allow. It only runs on your own PC.
echo.

REM Open the browser a few seconds after the server starts binding:
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 5; Start-Process 'http://127.0.0.1:%PORT%/'"

REM Run the engine in this window (foreground). Closing the window stops it.
%PY% api.py

endlocal
