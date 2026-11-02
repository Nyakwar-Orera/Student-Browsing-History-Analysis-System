@echo off
REM MamalBrowse Sentinel — Persistent Server Launcher (Updated 2026-10-30)
REM Uses START command to detach from Job Object so process survives shell exit
setlocal enabledelayedexpansion
title MamalBrowse Sentinel Server (Port 5050 - Dual Internal & External Access)
cd /d "%~dp0"

echo ============================================================
echo   MAMALBROWSE SENTINEL - DUAL ACCESS PRODUCTION SERVER
echo ============================================================
echo.

:: Detect Python environment
set PYTHON_EXE=MamalLab\Scripts\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python.exe
)

echo [1/3] Verifying Python interpreter...
"%PYTHON_EXE%" --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Please ensure Python is installed or MamalLab venv exists.
    pause
    exit /b 1
)
echo       Python interpreter OK

echo.
echo [2/3] Starting Secure Cloudflare Tunnel for external access...
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\run_tunnel.ps1"

echo [3/3] Launching MamalBrowse Sentinel core service...
echo       INTERNAL ACCESS : http://10.10.117.76:5050
if exist "data\external_url.txt" (
    set /p EXT_URL=<data\external_url.txt
    echo       EXTERNAL ACCESS : !EXT_URL!
)
echo.
echo       Press Ctrl+C to stop the server.
echo ============================================================
echo.

"%PYTHON_EXE%" app_production.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [SERVER STOPPED OR FAILED TO START]
    pause
)
