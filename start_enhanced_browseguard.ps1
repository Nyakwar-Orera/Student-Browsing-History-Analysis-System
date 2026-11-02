# ================================================================
#  MamalBrowse Sentinel - Quick Start / Launch Script
#  Launches the production server on port 5050 (PowerShell 5.1 Compatible)
# ================================================================
$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir "MamalLab\Scripts\python.exe"
$AppScript  = Join-Path $ScriptDir "app_production.py"
$LogFile    = Join-Path $ScriptDir "production.log"

Write-Host ""
Write-Host "  +==========================================================+" -ForegroundColor Cyan
Write-Host "  |          MAMALBROWSE SENTINEL - SERVER STARTUP           |" -ForegroundColor Cyan
Write-Host "  |   Website Enforcement + Session Timer + Live Dashboard   |" -ForegroundColor Cyan
Write-Host "  +==========================================================+" -ForegroundColor Cyan
Write-Host ""

# Check if port 5050 is already occupied; terminate stale instance if needed
$occupied = Get-NetTCPConnection -LocalPort 5050 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
if ($occupied) {
    foreach ($pidToKill in $occupied) {
        if ($pidToKill -gt 0) {
            Write-Host "  Releasing port 5050 (terminating existing PID $pidToKill)..." -ForegroundColor Yellow
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 1
}

# Python Interpreter Selection
if (-not (Test-Path $VenvPython)) {
    $VenvPython = "python.exe"
}

Write-Host "  Interpreter : $VenvPython" -ForegroundColor DarkGray
Write-Host "  Entrypoint  : $AppScript" -ForegroundColor DarkGray
Write-Host "  Dashboard   : http://10.10.117.76:5050  |  http://localhost:5050" -ForegroundColor Green
Write-Host ""
Write-Host "  MamalBrowse Sentinel production server is starting..." -ForegroundColor Cyan
Write-Host "  Press Ctrl+C to stop the server." -ForegroundColor Yellow
Write-Host ""

Set-Location $ScriptDir
& $VenvPython $AppScript
