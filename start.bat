@echo off
rem torrentkit one-click launcher (Windows) - author: lee
rem Double-click to start the Web UI and open it in your browser.
setlocal
cd /d "%~dp0"

rem Try each candidate for real and keep the first one that runs Python 3.9+.
rem (py.exe may exist but have no usable runtime; "python" may be the Store alias.)
set "PY="
for %%C in ("py -3" "py" "python" "python3") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=%%~C"
    )
)
if not defined PY (
    echo [torrentkit] Python 3.9+ not found.
    echo [torrentkit] Install it from https://www.python.org/downloads/ and tick "Add Python to PATH".
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
