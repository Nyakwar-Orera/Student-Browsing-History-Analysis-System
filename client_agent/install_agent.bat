@echo off
:: ================================================================
:: MamalBrowse Sentinel Client Enforcement Agent — Windows Setup Script
:: Registers the agent as a Windows Scheduled Task on lab PCs.
:: Run as ADMINISTRATOR.
::
:: Includes: URL policy enforcement + Session Timer enforcement
:: ================================================================
setlocal

set AGENT_DIR=C:\ProgramData\Sentinel
set AGENT_FILE=%AGENT_DIR%\browseguard_agent.py
set PYTHON_PATH=C:\Python311\python.exe

:: Fallback Python detection
if not exist "%PYTHON_PATH%" (
    for /f "usebackq tokens=*" %%i in (`where python`) do set PYTHON_PATH=%%i
)

echo ============================================================
echo   BROWSEGUARD CLIENT AGENT INSTALLER
echo ============================================================
echo.

:: Station Name Configuration (e.g. MK-50, RB-43)
set TARGET_STATION=%~1
if "%TARGET_STATION%"=="" (
    set /p TARGET_STATION="Enter Lab Workstation Tag (e.g. MK-50, RB-43) [%COMPUTERNAME%]: "
)
if "%TARGET_STATION%"=="" set TARGET_STATION=%COMPUTERNAME%

echo [1/5] Creating Sentinel agent directory...
if not exist "%AGENT_DIR%" mkdir "%AGENT_DIR%"

echo [2/5] Copying agent script...
if exist "%~dp0browseguard_agent.py" (
    copy /Y "%~dp0browseguard_agent.py" "%AGENT_FILE%" >nul
) else if exist "%~dp0sentinel_agent.py" (
    copy /Y "%~dp0sentinel_agent.py" "%AGENT_FILE%" >nul
)

echo [3/5] Configuring Workstation Tag (%TARGET_STATION%)...
echo %TARGET_STATION%> "%AGENT_DIR%\station_name.txt"

echo [4/5] Installing Python dependencies...
"%PYTHON_PATH%" -m pip install requests uiautomation --quiet

echo [5/5] Registering Windows Scheduled Task (runs at logon, all users)...
schtasks /Delete /TN "SentinelAgent" /F >nul 2>&1

schtasks /Create ^
  /TN "SentinelAgent" ^
  /TR "\"%PYTHON_PATH%\" \"%AGENT_FILE%\"" ^
  /SC ONLOGON ^
  /RU SYSTEM ^
  /RL HIGHEST ^
  /F

echo.
echo ============================================================
echo   INSTALLATION COMPLETE
echo ============================================================
echo.
echo   Agent location : %AGENT_FILE%
echo   Station Tag    : %TARGET_STATION%
echo   Task name      : SentinelAgent
echo   Runs           : At user logon (SYSTEM account)
echo.
echo   Server Target  : http://10.10.117.76:5050
echo.
echo   Session time limits:
echo     Darajah 1-4  = 30 minutes (set by MamalBrowse Sentinel server)
echo     Darajah 5-7  = 60 minutes
echo     Darajah 8-12 / IGCSE = 120 minutes
echo.
pause
endlocal
