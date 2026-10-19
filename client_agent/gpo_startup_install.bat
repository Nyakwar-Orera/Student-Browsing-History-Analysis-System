@echo off
:: =============================================================================
:: MamalBrowse Sentinel — Active Directory GPO Computer Startup Deployment Script
:: Configured in GPMC: Computer Configuration -> Policies -> Windows Settings -> Scripts (Startup)
:: Executes automatically as NT AUTHORITY\SYSTEM whenever any lab PC boots up.
:: Zero manual interaction required on client machines.
:: =============================================================================
setlocal

:: 1. Configuration
set SERVER_SHARE=\\10.10.117.76\Maintenance\MamalBrowse Sentinel
set AGENT_DIR=C:\ProgramData\Sentinel
set AGENT_FILE=%AGENT_DIR%\browseguard_agent.py
set STATION_FILE=%AGENT_DIR%\station_name.txt
set PYTHON_PATH=C:\Python311\python.exe

:: Fallback Python discovery
if not exist "%PYTHON_PATH%" (
    for /f "usebackq tokens=*" %%i in (`where python 2^>nul`) do set PYTHON_PATH=%%i
)

:: If Python is not installed yet on this PC, exit cleanly until pre-requisite is pushed
if "%PYTHON_PATH%"=="" exit /b 0

:: 2. Ensure Sentinel directory exists
if not exist "%AGENT_DIR%" mkdir "%AGENT_DIR%"

:: 3. Configure Authoritative Workstation Tag from Computer Name (e.g. MK-50, RB-43)
if not exist "%STATION_FILE%" (
    echo %COMPUTERNAME%> "%STATION_FILE%"
)

:: 4. Update Agent Script from Institutional Network Share
if exist "%SERVER_SHARE%\browseguard_agent.py" (
    xcopy /Y /D "%SERVER_SHARE%\browseguard_agent.py" "%AGENT_FILE%" >nul 2>&1
) else if exist "%~dp0browseguard_agent.py" (
    xcopy /Y /D "%~dp0browseguard_agent.py" "%AGENT_FILE%" >nul 2>&1
)

:: 5. Install / Verify Python dependencies
"%PYTHON_PATH%" -m pip install requests uiautomation --quiet >nul 2>&1

:: 6. Register / Refresh Scheduled Task (Runs at user logon under SYSTEM)
schtasks /Query /TN "SentinelAgent" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    schtasks /Create ^
      /TN "SentinelAgent" ^
      /TR "\"%PYTHON_PATH%\" \"%AGENT_FILE%\"" ^
      /SC ONLOGON ^
      /RU SYSTEM ^
      /RL HIGHEST ^
      /F >nul 2>&1
)

exit /b 0
