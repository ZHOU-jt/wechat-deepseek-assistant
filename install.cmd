@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist "assistant\settings.json" copy "assistant\settings.example.json" "assistant\settings.json" >nul
if not exist "assistant\prompt.txt" copy "assistant\prompt.example.txt" "assistant\prompt.txt" >nul
echo Installation complete. Run start.cmd.
pause
exit /b 0
:failed
echo Installation failed. Install Python 3.12 x64 with Tcl/Tk and the Python launcher.
pause
exit /b 1
