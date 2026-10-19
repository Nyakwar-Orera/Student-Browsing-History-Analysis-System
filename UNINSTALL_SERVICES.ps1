# ================================================================
#  BROWSEGUARD ENTERPRISE — SERVICE UNINSTALLER
#  Removes Windows Services and firewall rules cleanly.
#  Run as Administrator.
# ================================================================
#Requires -RunAsAdministrator
$ErrorActionPreference = "SilentlyContinue"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$LabMgrDir  = Join-Path $ScriptDir "Lab Manager"
$NssmExe    = Join-Path $LabMgrDir "nssm.exe"

$services = @("MamalBrowse Sentinel", "LabManager")

Write-Host ""
Write-Host "  MamalBrowse Sentinel - Service Uninstaller" -ForegroundColor Yellow
Write-Host ""

foreach ($svc in $services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
        Write-Host "  Stopping $svc..." -ForegroundColor Cyan
        Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        if (Test-Path $NssmExe) {
            & $NssmExe remove $svc confirm 2>$null
        } else {
            sc.exe delete $svc | Out-Null
        }
        Write-Host "  Removed service: $svc" -ForegroundColor Green
    } else {
        Write-Host "  Service not found (skipped): $svc" -ForegroundColor DarkGray
    }
}

# Remove firewall rules
foreach ($rule in @("MamalBrowse Sentinel-Dashboard","LabManager-Server")) {
    Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
    Write-Host "  Removed firewall rule: $rule" -ForegroundColor Green
}

# Remove desktop shortcut
$shortcut = Join-Path ([System.Environment]::GetFolderPath("CommonDesktopDirectory")) "Sentinel Dashboard.lnk"
if (Test-Path $shortcut) { Remove-Item $shortcut -Force; Write-Host "  Removed desktop shortcut" -ForegroundColor Green }

Write-Host ""
Write-Host "  Uninstall complete. Services will no longer start automatically." -ForegroundColor Green
Write-Host ""
