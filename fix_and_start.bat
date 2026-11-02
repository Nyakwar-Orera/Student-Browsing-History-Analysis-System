@echo off
title MamalBrowse Sentinel — Fix and Start
cd /d "%~dp0"
color 0A

echo.
echo  ============================================================
echo    MAMALBROWSE SENTINEL — FIX AND START
echo  ============================================================
echo.

:: Detect Python interpreter
set PYTHON_EXE=MamalLab\Scripts\python.exe
set PIP_EXE=MamalLab\Scripts\pip.exe

if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python.exe
    set PIP_EXE=pip.exe
)

echo [1/5] Verifying Python...
"%PYTHON_EXE%" --version
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found!
    pause
    exit /b 1
)

echo.
echo [2/5] Installing / verifying required packages...
"%PIP_EXE%" install pymysql>=1.1.0 DBUtils>=3.0.0 waitress>=3.0.0 --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] pip install had warnings - attempting to continue...
)
echo       pymysql + DBUtils + waitress: OK

echo.
echo [3/5] Opening Windows Firewall port 5050 (TCP inbound)...
netsh advfirewall firewall delete rule name="MamalBrowse Sentinel 5050" >nul 2>&1
netsh advfirewall firewall add rule name="MamalBrowse Sentinel 5050" ^
    dir=in action=allow protocol=TCP localport=5050 profile=any ^
    description="MamalBrowse Sentinel web dashboard"
if %ERRORLEVEL% EQU 0 (
    echo       Firewall rule added: port 5050 TCP inbound ALLOWED
) else (
    echo [WARN] Could not add firewall rule (may need admin rights)
    echo        Try running this .bat as Administrator if browser still cant connect
)

echo.
echo [4/5] Checking if port 5050 is already in use...
netstat -ano | findstr ":5050" | findstr "LISTENING"
if %ERRORLEVEL% EQU 0 (
    echo [WARN] Port 5050 is already in use. Killing existing process...
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5050" ^| findstr "LISTENING"') do (
        taskkill /PID %%p /F >nul 2>&1
        echo       Killed PID %%p
    )
    timeout /t 2 /nobreak >nul
) else (
    echo       Port 5050 is free. Good.
)

echo.
echo [5/5] Starting MamalBrowse Sentinel on http://10.10.117.76:5050 ...
echo.
echo  Access from any lab PC: http://10.10.117.76:5050
echo  Access locally:         http://localhost:5050
echo.
echo  Press Ctrl+C to stop the server.
echo  ============================================================
echo.

"%PYTHON_EXE%" app_production.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [SERVER STOPPED OR FAILED]
    echo  Check production.log for details
    pause
)
