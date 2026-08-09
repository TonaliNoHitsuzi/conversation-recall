@echo off
chcp 65001 >nul
REM ============================================================
REM   conversation-recall · 本地网页人工入口终端启动器
REM   用法：双击本文件，或在终端执行  web\run.bat
REM   首次启动访问  http://127.0.0.1:8719
REM ============================================================

cd /d "%~dp0"
python server.py
pause
