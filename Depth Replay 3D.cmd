@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Python environment is missing. Run setup.cmd first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m robohand.replay.player %*
