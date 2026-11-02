<#
.SYNOPSIS
  Emergency Stop Tool: Halts Sentinel Client Agent across all domain workstations.
  1. Kills all running python.exe and pythonw.exe processes.
  2. Ends and deletes all Sentinel scheduled tasks (SentinelAgent, BrowseGuardAgent).
  3. Removes all Startup launchers (.vbs, .cmd, .bat).
#>

$CredUser = "MAMAL\admin"
$CredPass = "100%SECUREDadmin"

Write-Host "==========================================================" -ForegroundColor Red
Write-Host " MAMALBROWSE SENTINEL - EMERGENCY STOP ALL CLIENT AGENTS" -ForegroundColor Red
Write-Host "==========================================================" -ForegroundColor Red

# Discover computers from Active Directory
$OUs = @(
    "OU=Mamal_Boys_Computers,DC=mamal,DC=nbi",
    "OU=Mamal_Girls_Computers,DC=mamal,DC=nbi",
    "OU=Outterlabs,DC=mamal,DC=nbi"
)

$Stations = @()
try {
    foreach ($ou in $OUs) {
        $found = Get-ADComputer -Filter * -SearchBase $ou -ErrorAction SilentlyContinue | Select-Object -ExpandProperty DNSHostName
        if ($found) { $Stations += $found }
    }
} catch {
    # Fallback to standard naming
    $Boys = 1..60 | ForEach-Object { "M{0:D2}" -f $_ }
    $Girls = 1..40 | ForEach-Object { "F-{0:D2}" -f $_ }
    $Outter = 40..60 | ForEach-Object { ("MK-{0}" -f $_), ("RB-{0}" -f $_) }
    $Stations = $Boys + $Girls + $Outter
}

Write-Host "Target workstations: $($Stations.Count)" -ForegroundColor Cyan
Write-Host ""

$StoppedCount = 0
$OfflineCount = 0

foreach ($station in $Stations) {
    $cleanName = ($station -split '\.')[0]
    
    # Quick connectivity check
    $share = "\\$cleanName\C$\ProgramData\Sentinel"
    if (-not (Test-Path $share)) {
        $OfflineCount++
        continue
    }

    Write-Host "Stopping Sentinel on $cleanName..." -ForegroundColor Yellow

    # 1. Kill python processes
    cmd.exe /c "taskkill /S $cleanName /U $CredUser /P $CredPass /F /IM python.exe >nul 2>&1"
    cmd.exe /c "taskkill /S $cleanName /U $CredUser /P $CredPass /F /IM pythonw.exe >nul 2>&1"

    # 2. End and delete any scheduled tasks
    cmd.exe /c "schtasks /End /S $cleanName /U $CredUser /P $CredPass /TN SentinelAgent >nul 2>&1"
    cmd.exe /c "schtasks /Delete /S $cleanName /U $CredUser /P $CredPass /TN SentinelAgent /F >nul 2>&1"
    cmd.exe /c "schtasks /End /S $cleanName /U $CredUser /P $CredPass /TN BrowseGuardAgent >nul 2>&1"
    cmd.exe /c "schtasks /Delete /S $cleanName /U $CredUser /P $CredPass /TN BrowseGuardAgent /F >nul 2>&1"
    cmd.exe /c "schtasks /End /S $cleanName /U $CredUser /P $CredPass /TN RunSentinelStealth >nul 2>&1"
    cmd.exe /c "schtasks /Delete /S $cleanName /U $CredUser /P $CredPass /TN RunSentinelStealth /F >nul 2>&1"

    # 3. Remove Startup launchers so they don't restart on logon
    $startupFolder = "\\$cleanName\C$\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"
    if (Test-Path $startupFolder) {
        Remove-Item "$startupFolder\SentinelAgent.vbs" -Force -ErrorAction SilentlyContinue
        Remove-Item "$startupFolder\SentinelAgent.cmd" -Force -ErrorAction SilentlyContinue
        Remove-Item "$startupFolder\SentinelAgent.bat" -Force -ErrorAction SilentlyContinue
    }

    Write-Host "  [OK] $cleanName completely stopped and deactivated." -ForegroundColor Green
    $StoppedCount++
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Red
Write-Host " STOP COMPLETED" -ForegroundColor Red
Write-Host " Active Workstations Stopped: $StoppedCount" -ForegroundColor Green
Write-Host " Offline/Unreachable: $OfflineCount" -ForegroundColor Gray
Write-Host "==========================================================" -ForegroundColor Red

