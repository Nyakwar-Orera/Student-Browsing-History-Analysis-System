@echo off
:: ================================================================
::  MAMALBROWSE SENTINEL - AUTO-ELEVATING SERVICE INSTALLER
::  Automatically prompts for Administrator rights (UAC) and runs
::  INSTALL_SERVICES.ps1 to register the Windows Service & Tasks.
:: ================================================================
title MamalBrowse Sentinel - Windows Service Installer
cd /d "%~dp0"

:: Check for Administrative permissions
>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"
if '%errorlevel%' NEQ '0' (
    echo.
    echo  ============================================================
    echo    Requesting Administrator privileges...
    echo  ============================================================
    echo.
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    del "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    pushd "%CD%"
    cd /d "%~dp0"

echo.
echo  ============================================================
echo    MAMALBROWSE SENTINEL - INSTALLING AS WINDOWS SERVICE
echo  ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALL_SERVICES.ps1"

echo.
echo  ============================================================
echo    Installation script execution finished.
echo  ============================================================
echo.
pause

