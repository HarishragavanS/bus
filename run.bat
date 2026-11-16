@echo off
title VehicleGate — Starting System

echo.
echo  =============================================
echo   VehicleGate - Smart Vehicle Entry System
echo  =============================================
echo.

:: Check if virtual environment exists, create if not
IF NOT EXIST "venv\Scripts\activate.bat" (
    echo [1/3] Creating virtual environment...
    python -m venv venv
    echo       Done.
) ELSE (
    echo [1/3] Virtual environment found.
)

:: Activate virtual environment
echo [2/3] Activating virtual environment...
call venv\Scripts\activate.bat

:: Install dependencies
echo [3/3] Installing dependencies...
pip install -r requirements.txt --quiet

echo.
echo  System is starting...
echo  Open your browser at: http://localhost:5000
echo  Press CTRL+C to stop.
echo.

:: Run the app
python app.py

pause
