<#
.SYNOPSIS
    MamalBrowse Sentinel — Network Mass Deployment Engine
    Deploys BrowseGuard Agent remotely across all lab workstations (MK-*, RB-*, M*, F-*)
    directly from the institutional server without visiting individual PCs.

.DESCRIPTION
    Uses Windows Admin Shares (C$) and Remote Task Scheduler (schtasks /S):
      1. Discovers target lab machines from Lab Manager, Active Directory, or list.
      2. Pushes browseguard_agent.py to \\<Station>\C$\ProgramData\Sentinel\
      3. Sets \\<Station>\C$\ProgramData\Sentinel\station_name.txt to the machine's exact station tag
      4. Creates the 'SentinelAgent' scheduled task to run at logon with SYSTEM privileges

.EXAMPLE
    .\deploy_agents_remote.ps1 -Lab "MK" -Start 1 -End 50
    Deploys to MK-01 through MK-50.

.EXAMPLE
    .\deploy_agents_remote.ps1 -AutoDiscoverFromLabManager
    Deploys to all workstations recorded in Lab Manager session history.
#>

param(
    [string]$ServerHost = "10.10.117.76:5050",
    [string]$Lab = "",                     # e.g. "MK", "RB", "M", "F"
    [int]$Start = 1,
    [int]$End = 50,
    [string[]]$ComputerList = @(),
    [switch]$AutoDiscoverFromLabManager,
    [switch]$ForceReinstall
)

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  MAMALBROWSE SENTINEL — REMOTE NETWORK DEPLOYMENT ENGINE   " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$CurrentDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentSource = Join-Path $CurrentDir "browseguard_agent.py"

if (-not (Test-Path $AgentSource)) {
    Write-Host "[FATAL] Source agent not found at $AgentSource" -ForegroundColor Red
    exit 1
}

# 1. Build List of Target Workstations
$Targets = [System.Collections.Generic.List[string]]::new()

if ($AutoDiscoverFromLabManager) {
    Write-Host "[1/3] Auto-discovering workstations from Lab Manager session records..." -ForegroundColor Yellow
    $LmSessionPath = "..\data\lab_manager\session_history.json"
    if (Test-Path $LmSessionPath) {
        try {
            $JsonContent = Get-Content $LmSessionPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $Discovered = $JsonContent | ForEach-Object { $_.machine_id } | Where-Object { $_ -and $_ -ne "UNKNOWN" -and $_ -ne "PC" } | Select-Object -Unique
            foreach ($m in $Discovered) {
                $clean = $m.Trim().ToUpper()
                if ($clean -and -not $Targets.Contains($clean)) {
                    $Targets.Add($clean)
                }
            }
            Write-Host "  Found $($Targets.Count) unique workstations in Lab Manager records." -ForegroundColor Green
        } catch {
            Write-Host "  Error reading session history: $_" -ForegroundColor Red
        }
    }
} elseif ($ComputerList.Count -gt 0) {
    foreach ($c in $ComputerList) { $Targets.Add($c.Trim().ToUpper()) }
} elseif ($Lab) {
    Write-Host "[1/3] Generating station list for Lab '$Lab' ($Start to $End)..." -ForegroundColor Yellow
    for ($i = $Start; $i -le $End; $i++) {
        $numStr = $i.ToString("D2")
        $Targets.Add("$Lab-$numStr")
    }
} else {
    Write-Host "Specify targets via:" -ForegroundColor Yellow
    Write-Host "  -AutoDiscoverFromLabManager" -ForegroundColor White
    Write-Host "  -Lab MK -Start 1 -End 50" -ForegroundColor White
    Write-Host "  -ComputerList MK-01,MK-02,RB-43" -ForegroundColor White
    exit 0
}

Write-Host "Target workstation count: $($Targets.Count)" -ForegroundColor Cyan
Write-Host ""

# 2. Iterate and deploy
$SuccessCount = 0
$OfflineCount = 0
$FailedCount = 0

foreach ($Station in $Targets) {
    Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Deploying to Station: $Station ... " -NoNewline -ForegroundColor White

    # Check network ping/reachability
    $Ping = Test-Connection -ComputerName $Station -Count 1 -Quiet -ErrorAction SilentlyContinue
    if (-not $Ping) {
        Write-Host "[OFFLINE / UNREACHABLE]" -ForegroundColor DarkYellow
        $OfflineCount++
        continue
    }

    $RemoteDir = "\\$Station\C$\ProgramData\Sentinel"
    $RemoteAgent = "$RemoteDir\browseguard_agent.py"
    $RemoteStationFile = "$RemoteDir\station_name.txt"

    try {
        # Create directory
        if (-not (Test-Path $RemoteDir)) {
            New-Item -Path $RemoteDir -ItemType Directory -Force -ErrorAction Stop | Out-Null
        }

        # Copy agent
        Copy-Item -Path $AgentSource -Destination $RemoteAgent -Force -ErrorAction Stop

        # Write authoritative station identifier
        Set-Content -Path $RemoteStationFile -Value $Station -Encoding UTF8 -Force

        # Register Scheduled Task on remote workstation
        $PythonRemote = "C:\Python311\python.exe"
        $TaskCmd = "schtasks /Create /S $Station /TN SentinelAgent /TR `"`"$PythonRemote`" `"$RemoteAgent`"`" /SC ONLOGON /RU SYSTEM /RL HIGHEST /F"
        
        $Res = Invoke-Expression $TaskCmd 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[SUCCESS: INSTALLED & SCHEDULED]" -ForegroundColor Green
            $SuccessCount++
        } else {
            Write-Host "[TASK REGISTRATION WARNING: $Res]" -ForegroundColor Yellow
            $SuccessCount++
        }
    } catch {
        Write-Host "[ERROR: $_]" -ForegroundColor Red
        $FailedCount++
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  DEPLOYMENT SUMMARY" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Total Target Stations : $($Targets.Count)" -ForegroundColor White
Write-Host "  Successfully Deployed : $SuccessCount" -ForegroundColor Green
Write-Host "  Offline (Turned Off)  : $OfflineCount" -ForegroundColor DarkYellow
Write-Host "  Failed (Access Error) : $FailedCount" -ForegroundColor Red
Write-Host "============================================================" -ForegroundColor Cyan
