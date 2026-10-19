@echo off
:: This script MUST be run as Administrator
title Open Firewall Port 5050 — MamalBrowse Sentinel

net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERROR] Please right-click this file and choose "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo    Opening Windows Firewall — Port 5050 TCP (Inbound)
echo  ============================================================
echo.

:: Remove old rule if exists
netsh advfirewall firewall delete rule name="MamalBrowse Sentinel 5050" >nul 2>&1

:: Add new inbound rule — all profiles (domain, private, public)
netsh advfirewall firewall add rule ^
    name="MamalBrowse Sentinel 5050" ^
    dir=in ^
    action=allow ^
    protocol=TCP ^
    localport=5050 ^
    profile=any ^
    description="MamalBrowse Sentinel web dashboard — allows lab PCs to reach the server"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo  [SUCCESS] Firewall rule added!
    echo.
    echo  Lab PCs can now access: http://10.10.117.76:5050
    echo  Server local access:    http://localhost:5050
    echo.
) else (
    echo.
    echo  [FAILED] Could not add firewall rule.
    echo  Make sure you are running as Administrator.
    echo.
)

:: Show the rule to confirm
echo  Verifying rule:
netsh advfirewall firewall show rule name="MamalBrowse Sentinel 5050"

echo.
pause
