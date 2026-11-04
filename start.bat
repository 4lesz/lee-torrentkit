@echo off
rem torrentkit one-click launcher (Windows) - author: lee
rem Double-click to start the Web UI and open it in your browser.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [torrentkit] Python 3.9+ not found. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if errorlevel 1 (
    echo [torrentkit] Python 3.9 or newer is required.
    %PY% --version
    pause
    exit /b 1
)

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8080"

if not exist downloads mkdir downloads
echo [torrentkit] Web UI: http://127.0.0.1:%PORT%/   (close this window or press Ctrl+C to stop)
%PY% -m torrentkit web -d downloads --port %PORT% --open
if errorlevel 1 pause
endlocal
