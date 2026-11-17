@echo off
rem Create the Python environment used by every Robohand tool.
cd /d "%~dp0"
where py >nul 2>nul && (py -3.13 -m venv .venv) || (python -m venv .venv)
if errorlevel 1 (
    echo Python 3.13 64-bit is required: https://www.python.org/downloads/
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Package installation failed.
    pause
    exit /b 1
)
echo.
echo Setup complete. Start "Camera Studio.cmd".
pause
