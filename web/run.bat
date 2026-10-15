@echo off
chcp 65001 >nul
REM ============================================================
REM   conversation-recall web launcher
REM   Default   : hidden backend (pythonw, no console window),
REM               the server opens the web UI in your browser.
REM   Debug mode: run.bat debug  -> visible console with live logs
REM   Stop      : web UI power button (top-right), or closing
REM               all pages auto-stops the backend.
REM ============================================================
cd /d "%~dp0"
if /i "%~1"=="debug" (
  python server.py
  goto :eof
)
start "" pythonw server.py --open-browser
