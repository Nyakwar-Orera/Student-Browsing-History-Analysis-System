# ================================================================
#  MamalBrowse Sentinel - Quick Start / Status Script
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

# Check if installed as Windows Service
$bgSvc = Get-Service -Name "MamalBrowse Sentinel" -ErrorAction SilentlyContinue
$lmSvc = Get-Service -Name "LabManager" -ErrorAction SilentlyContinue

if ($bgSvc) {
    Write-Host "  [SERVICE MODE] MamalBrowse Sentinel is installed as a Windows Service." -ForegroundColor Green
    Write-Host ""
    Write-Host "  CURRENT STATUS:" -ForegroundColor White

    $bgColor = "Yellow"
    if ($bgSvc.Status -eq "Running") {
        $bgColor = "Green"
    }
    Write-Host "    MamalBrowse Sentinel : $($bgSvc.Status)" -ForegroundColor $bgColor

    if ($lmSvc) {
        $lmColor = "Yellow"
        if ($lmSvc.Status -eq "Running") {
            $lmColor = "Green"
        }
        Write-Host "    Lab Manager : $($lmSvc.Status)" -ForegroundColor $lmColor
    }
    Write-Host ""

    if ($bgSvc.Status -ne "Running") {
        Write-Host "  Starting services..." -ForegroundColor Yellow
        if ($lmSvc) {
            if ($lmSvc.Status -ne "Running") {
                Start-Service "LabManager" -ErrorAction SilentlyContinue
                Write-Host "    Lab Manager : Started" -ForegroundColor Green
            }
        }
        Start-Sleep -Seconds 2
        Start-Service "MamalBrowse Sentinel"
        Write-Host "    MamalBrowse Sentinel : Started" -ForegroundColor Green
    } else {
        Write-Host "  Services are running." -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "  Dashboard : http://localhost:5050" -ForegroundColor DarkCyan
    Write-Host "  Dashboard : http://10.10.117.76:5050" -ForegroundColor DarkCyan
    Write-Host ""
    exit 0
}

# Direct launch mode
Write-Host "  [DIRECT MODE] Launching Waitress production server..." -ForegroundColor Yellow
Write-Host ""

if (-not (Test-Path $VenvPython)) {
    $VenvPython = "python.exe"
}

Write-Host "  Interpreter: $VenvPython" -ForegroundColor DarkGray
Write-Host "  Script     : $AppScript" -ForegroundColor DarkGray
Write-Host "  URL        : http://10.10.117.76:5050  |  http://localhost:5050" -ForegroundColor Green
Write-Host ""
Write-Host "  Press Ctrl+C to stop the server." -ForegroundColor Yellow
Write-Host ""

Set-Location $ScriptDir
& $VenvPython $AppScript
