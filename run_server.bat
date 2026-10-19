@echo off
title MamalBrowse Sentinel Server (Port 5050)
cd /d "%~dp0"

echo ============================================================
echo   MAMALBROWSE SENTINEL — PRODUCTION SERVER
echo ============================================================
echo.

:: Detect Python environment
set PYTHON_EXE=MamalLab\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python.exe
)

echo [1/2] Verifying Python interpreter...
"%PYTHON_EXE%" --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Please ensure Python is installed or MamalLab venv exists.
    pause
    exit /b 1
)

echo [2/2] Launching MamalBrowse Sentinel on http://10.10.117.76:5050 ...
echo       Press Ctrl+C to stop the server.
echo.

"%PYTHON_EXE%" app_production.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [SERVER STOPPED OR FAILED TO START]
    pause
)
