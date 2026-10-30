@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
".venv\Scripts\python.exe" "assistant\server_session.py" --disconnect
if errorlevel 1 pause
