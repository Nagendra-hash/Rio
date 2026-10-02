@echo off
REM ============================================================================
REM portable.bat — run JARVIS straight from a pendrive on any Windows PC.
REM
REM Modes, decided by what the drive contains:
REM   • jarvis-portable\python\python.exe exists → runtime bundle prepared by
REM     "python scripts/build_portable.py --windows" on a machine that has
REM     Python (see PORTABLE.md). Dependencies live in jarvis-portable\
REM     site-packages\ — no internet, no install, no admin rights.
REM   • otherwise → setup mode: downloads the embeddable Python, bootstraps
REM     pip with get-pip.py, installs requirements into jarvis-portable\
REM     site-packages\. Needs internet ONCE. (The embeddable distribution
REM     cannot create venvs and ships no pip — hence get-pip.py.)
REM
REM Everything writable (config\, memory\) lives in jarvis-home\ on the drive,
REM via the JARVIS_HOME environment variable — nothing is ever written to the
REM host PC. Plug in, double-click, unplug when done.
REM ============================================================================

REM Keep the console open on error so the message can be read before the window
REM closes — a portable launcher that vanishes on failure teaches nothing.
setlocal EnableExtensions
if not defined JARVIS_KEEP_WINDOW set JARVIS_KEEP_WINDOW=1

cd /d "%~dp0"

REM ── Locate the app tree (this script sits in its root) ──────────────────────
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

REM ── Writable home on the drive: config + memory + journal live here ────────
set "JARVIS_HOME=%APP_DIR%\jarvis-home"
if not exist "%JARVIS_HOME%" mkdir "%JARVIS_HOME%"

REM ── Runtime layout (matches scripts/build_portable.py --windows) ────────────
set "PORTABLE=%APP_DIR%\jarvis-portable"
set "RUNTIME=%PORTABLE%\python"
set "SITE_PKG=%PORTABLE%\site-packages"
set "EMBED_ZIP=%PORTABLE%\python-embed.zip"
set "GETPIP=%PORTABLE%\get-pip.py"

if exist "%RUNTIME%\python.exe" goto :have_runtime

echo [portable] First run on this drive — one-time setup (needs internet).
echo [portable] After this, the drive runs fully offline on any Windows PC.
echo.

if not exist "%PORTABLE%" mkdir "%PORTABLE%"

if not exist "%EMBED_ZIP%" (
    echo [portable] Downloading embeddable Python 3.12 ~11 MB^)...
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip' -OutFile '%EMBED_ZIP%' -UseBasicParsing } catch { exit 1 }"
    if errorlevel 1 (
        echo [portable] ERROR: download failed. Connect to the internet and retry,
        echo [portable] or place python-3.12.8-embed-amd64.zip in jarvis-portable\ yourself.
        goto :fail
    )
)

echo [portable] Unpacking Python runtime...
powershell -NoProfile -Command "Expand-Archive -Force '%EMBED_ZIP%' '%RUNTIME%'"
if errorlevel 1 goto :fail

REM The embeddable distribution ships imports restricted by its ._pth file —
REM rewrite it to enable site-packages (same body build_portable.py uses):
REM   ..\site-packages     the drive's dependency folder, next to python\
REM   Lib\site-packages    where get-pip.py installs pip itself
REM Relative paths resolve from the ._pth file's own directory.
> "%RUNTIME%\python312._pth" (
    echo python312.zip
    echo .
    echo ..\site-packages
    echo Lib\site-packages
    echo import site
)

REM The embeddable distribution ships NO pip — bootstrap it with get-pip.py.
if not exist "%GETPIP%" (
    echo [portable] Fetching get-pip.py ~2.5 MB^)...
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%GETPIP%' -UseBasicParsing } catch { exit 1 }"
    if errorlevel 1 (
        echo [portable] ERROR: download failed — no pip without get-pip.py.
        echo [portable] Retry online, or drop get-pip.py into jarvis-portable\ yourself.
        goto :fail
    )
)

echo [portable] Bootstrapping pip...
"%RUNTIME%\python.exe" "%GETPIP%" --no-warn-script-location
if errorlevel 1 goto :fail

echo [portable] Installing dependencies ~2-4 GB, one time — this is the slow part^)...
"%RUNTIME%\python.exe" -m pip install -r "%APP_DIR%\requirements.txt" --target "%SITE_PKG%" --no-warn-script-location
if errorlevel 1 goto :fail

REM Marker for later runs: deps are present (checked before launch).
> "%PORTABLE%\.deps-ok" echo ok

:have_runtime
set "PY=%RUNTIME%\python.exe"

REM If the drive was set up by build_portable.py, .deps-ok already exists. A
REM missing marker means the dependency install was interrupted — reinstall.
if exist "%PORTABLE%\.deps-ok" goto :deps_ok
echo [portable] Dependencies incomplete — finishing install (needs internet)...
"%PY%" -m pip install -r "%APP_DIR%\requirements.txt" --target "%SITE_PKG%" --no-warn-script-location
if errorlevel 1 goto :fail
> "%PORTABLE%\.deps-ok" echo ok
:deps_ok

echo [portable] JARVIS_HOME=%JARVIS_HOME%
echo [portable] Starting JARVIS...
"%PY%" "%APP_DIR%\main.py" %*
goto :eof

:fail
echo.
echo [portable] Setup failed — see the message above.
if "%JARVIS_KEEP_WINDOW%"=="1" pause
exit /b 1
