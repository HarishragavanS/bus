@echo off
:: Ensure working directory is the script's folder
cd /d "%~dp0"

title VehicleGate - Smart Vehicle Entry System

echo.
echo =============================================
echo   VehicleGate - Smart Vehicle Entry System
echo =============================================
echo.

:: Check if virtual environment exists, create if not
if not exist "venv\Scripts\activate.bat" (
    echo [1/2] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment. Ensure Python is installed and added to PATH.
        pause
        exit /b 1
    )
    echo       Virtual environment created.
    echo       Installing requirements...
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    echo [1/2] Virtual environment found.
    call venv\Scripts\activate.bat
)

echo [2/2] Starting application...
echo.
echo System is starting at: http://localhost:5000
echo Press CTRL+C to stop.
echo.

:: Run app.py using the activated virtual environment
python app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with an error.
)

pause
