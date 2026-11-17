@echo off
cd /d "%~dp0tracker_studio"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Python environment is missing. Run setup.cmd first.
    pause
    exit /b 1
)
start "Tracker Studio" /b "%~dp0.venv\Scripts\pythonw.exe" "%~dp0tracker_studio\server.py" --open-browser
