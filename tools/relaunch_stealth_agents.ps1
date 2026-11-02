<#
.SYNOPSIS
  Relaunches Sentinel Agent v2.8 across all online workstations.
  Terminates legacy python/cmd processes and immediately starts
  the stealth VBS windowless launcher (SW_HIDE).
#>
param(
    [string[]]$ComputerName = @()
)

$CredUser = "MAMAL\admin"
$CredPass = "100%SECUREDadmin"

if (-not $ComputerName -or $ComputerName.Count -eq 0) {
    # Auto-discover from CSV or list active labs
    $Csvs = Get-ChildItem "client_agent\Sentinel_Deploy_Results_*.csv" | Sort-Object LastWriteTime -Descending
    if ($Csvs) {
        $LatestCsv = $Csvs[0].FullName
        Write-Host "Reading target stations from $LatestCsv..." -ForegroundColor Cyan
        $Entries = Import-Csv $LatestCsv | Where-Object { $_.Status -eq "DEPLOYED" }
        $ComputerName = $Entries.Station
    }
}

if (-not $ComputerName -or $ComputerName.Count -eq 0) {
    # Default lab stations
    $Boys = 1..60 | ForEach-Object { ("M{0:D2}" -f $_), ("M{0:D2}.mamal.nbi" -f $_) }
    $Girls = 1..60 | ForEach-Object { ("F-{0:D2}" -f $_), ("F-{0:D2}.mamal.nbi" -f $_) }
    $ComputerName = $Boys + $Girls
}

Write-Host "Target stations to restart in stealth mode: $($ComputerName.Count)" -ForegroundColor Green

$Success = 0
$Failed = 0

foreach ($Station in $ComputerName) {
    $CleanName = $Station.Split(".")[0]
    $Share = "\\$CleanName\C$\ProgramData\Sentinel"
    if (-not (Test-Path $Share)) {
        continue
    }

    Write-Host "Restarting $CleanName..." -ForegroundColor Gray

    # 1. Terminate old python.exe and pythonw.exe
    cmd.exe /c "taskkill /S $CleanName /U $CredUser /P $CredPass /F /IM python.exe >nul 2>&1"
    cmd.exe /c "taskkill /S $CleanName /U $CredUser /P $CredPass /F /IM pythonw.exe >nul 2>&1"

    # 2. Trigger SentinelAgent.vbs via scheduled task
    $VbsPath = "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\SentinelAgent.vbs"
    $TaskCmd = "schtasks /Create /S $CleanName /U $CredUser /P $CredPass /TN RunSentinelStealth /TR ""wscript.exe `"$VbsPath`"""" /SC ONCE /ST 23:59 /F >nul 2>&1 && schtasks /Run /S $CleanName /U $CredUser /P $CredPass /TN RunSentinelStealth >nul 2>&1 && schtasks /Delete /S $CleanName /U $CredUser /P $CredPass /TN RunSentinelStealth /F >nul 2>&1"
    
    $res = cmd.exe /c $TaskCmd
    Write-Host "  $CleanName restarted in stealth mode (zero-blink)." -ForegroundColor Green
    $Success++
}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " STEALTH RELAUNCH COMPLETE: $Success stations" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

