
#requires -Version 5.1
<#
MAMALBROWSE SENTINEL - MASS LAB DEPLOYMENT
Deploys the interactive BrowseGuard agent to authorised domain computers.

Run from an elevated PowerShell window on a domain administration computer.

The agent starts when a student logs in, in that student's desktop session.
It is NOT launched as SYSTEM.

Examples:
  .\mass_deploy_all_labs.ps1 -Limit 3 -WhatIf
  .\mass_deploy_all_labs.ps1 -Limit 3
  .\mass_deploy_all_labs.ps1
  .\mass_deploy_all_labs.ps1 -ComputerName MK-01,RB-02
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$ServerUrl = "http://10.10.117.76:5050",

    [string]$AgentSource = "",

    [string[]]$ComputerName = @(),

    [ValidateRange(1, 10000)]
    [int]$Limit = 0,

    [bool]$RemoveLegacySystemTask = $true
)

$ErrorActionPreference = "Stop"

if (-not $PSScriptRoot) {
    $PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    if (-not $PSScriptRoot) {
        $PSScriptRoot = "C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\client_agent"
    }
}
if (-not $AgentSource) {
    $AgentSource = Join-Path $PSScriptRoot "browseguard_agent.py"
}

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

$RemoteRoot = "C:\ProgramData\Sentinel"

$RemoteStartupFolder =
"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"

$StartupFileName = "SentinelAgent.vbs"

$OutputCsv = Join-Path $PSScriptRoot (
    "Sentinel_Deploy_Results_{0}.csv" -f (Get-Date -Format "yyyyMMdd_HHmmss")
)

$EncodingNoBom = New-Object System.Text.UTF8Encoding($false)

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " MAMALBROWSE SENTINEL - MASS DEPLOYMENT" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Server: $ServerUrl"
Write-Host "Agent : $AgentSource"
Write-Host ""

# ------------------------------------------------------------
# VALIDATE LOCAL SOURCE
# ------------------------------------------------------------

if (-not (Test-Path -LiteralPath $AgentSource -PathType Leaf)) {
    throw "Agent file not found: $AgentSource"
}

$AgentSource = (Resolve-Path -LiteralPath $AgentSource).Path

# ------------------------------------------------------------
# VALIDATE SERVER URL
# ------------------------------------------------------------

$ParsedServerUrl = $null

if (-not [Uri]::TryCreate(
        $ServerUrl,
        [UriKind]::Absolute,
        [ref]$ParsedServerUrl
    )) {
    throw "Invalid ServerUrl: $ServerUrl"
}

if ($ParsedServerUrl.Scheme -notin @("http", "https")) {
    throw "ServerUrl must use HTTP or HTTPS."
}

# ------------------------------------------------------------
# DISCOVER TARGET COMPUTERS
# ------------------------------------------------------------

$Targets = @()

if ($ComputerName.Count -gt 0) {

    Write-Host "[1/4] Using explicitly supplied computers..." -ForegroundColor Yellow

    $Targets = $ComputerName |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ } |
    Sort-Object -Unique
}
else {

    Write-Host "[1/4] Discovering computers from Active Directory..." -ForegroundColor Yellow

    try {
        Import-Module ActiveDirectory -ErrorAction Stop
    }
    catch {
        throw "ActiveDirectory PowerShell module is unavailable. Install RSAT or run this from a domain administration computer."
    }

    $OUs = @(
        @{
            Path   = "OU=Mamal_Boys_Computers,DC=mamal,DC=nbi"
            Filter = "ALL"
        },
        @{
            Path   = "OU=Mamal_Girls_Computers,DC=mamal,DC=nbi"
            Filter = "ALL"
        },
        @{
            Path   = "OU=Outterlabs,DC=mamal,DC=nbi"
            Filter = "LAB"
        }
    )

    $Discovered = [System.Collections.Generic.List[string]]::new()

    foreach ($OU in $OUs) {

        Write-Host "  Checking $($OU.Path)" -ForegroundColor Gray

        try {
            $Computers = Get-ADComputer `
                -SearchBase $OU.Path `
                -Filter 'Enabled -eq $true' `
                -Properties DNSHostName `
                -ErrorAction Stop

            foreach ($Computer in $Computers) {

                if (
                    $OU.Filter -eq "LAB" -and
                    $Computer.Name -notmatch '^(MK|RB)-?'
                ) {
                    continue
                }

                $Name = if ($Computer.DNSHostName) {
                    $Computer.DNSHostName
                }
                else {
                    $Computer.Name
                }

                if (-not $Discovered.Contains($Name)) {
                    $Discovered.Add($Name)
                }
            }
        }
        catch {
            Write-Warning "Could not query OU $($OU.Path): $_"
        }
    }

    $Targets = @($Discovered | Sort-Object -Unique)
}

if ($Limit -gt 0) {
    $Targets = @($Targets | Select-Object -First $Limit)
}

if ($Targets.Count -eq 0) {
    throw "No target computers were discovered."
}

Write-Host ""
Write-Host "Target computers: $($Targets.Count)" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------
# CREATE RESULTS COLLECTION
# ------------------------------------------------------------

$Results = [System.Collections.Generic.List[object]]::new()

$SuccessCount = 0
$FailedCount = 0
$WhatIfCount = 0

# ------------------------------------------------------------
# DEPLOY TO EACH COMPUTER
# ------------------------------------------------------------

foreach ($Station in $Targets) {

    Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Station: $Station" -ForegroundColor White

    $Status = "FAILED"
    $Details = ""

    try {

        $RemoteRootUNC = "\\$Station\C$\ProgramData\Sentinel"

        $RemoteStartupUNC =
        "\\$Station\C$\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"

        if (-not $PSCmdlet.ShouldProcess(
                $Station,
                "Deploy BrowseGuard interactive agent"
            )) {

            $Status = "WHATIF"
            $Details = "No changes made."
            $WhatIfCount++

            Write-Host "  WHATIF - no changes made." -ForegroundColor Yellow
        }
        else {

            # ------------------------------------------------
            # 1. TEST ADMIN SHARE ACCESS
            # ------------------------------------------------

            Write-Host "  Checking administrative share..." -ForegroundColor Gray

            if (-not (Test-Path "\\$Station\C$")) {
                throw "Cannot access C$ administrative share. Check permissions, firewall and workstation availability."
            }

            # ------------------------------------------------
            # 2. CREATE REMOTE DIRECTORIES
            # ------------------------------------------------

            Write-Host "  Creating Sentinel directory..." -ForegroundColor Gray

            New-Item `
                -Path $RemoteRootUNC `
                -ItemType Directory `
                -Force |
            Out-Null

            New-Item `
                -Path $RemoteStartupUNC `
                -ItemType Directory `
                -Force |
            Out-Null

            # ------------------------------------------------
            # 3. COPY AGENT
            # ------------------------------------------------

            Write-Host "  Copying BrowseGuard agent..." -ForegroundColor Gray

            $RemoteAgentUNC =
            Join-Path $RemoteRootUNC "browseguard_agent.py"

            Copy-Item `
                -LiteralPath $AgentSource `
                -Destination $RemoteAgentUNC `
                -Force

            # Verify copied file using SHA256
            $LocalHash = (Get-FileHash -LiteralPath $AgentSource -Algorithm SHA256).Hash

            $RemoteHash = (Get-FileHash -LiteralPath $RemoteAgentUNC -Algorithm SHA256).Hash

            if ($LocalHash -ne $RemoteHash) {
                throw "Agent checksum verification failed."
            }

            # ------------------------------------------------
            # 4. WRITE STATION IDENTIFIER
            # ------------------------------------------------

            Write-Host "  Writing station identity..." -ForegroundColor Gray

            $StationTag = $Station.Split('.')[0].Trim().ToUpperInvariant()

            $RemoteStationFile =
            Join-Path $RemoteRootUNC "station_name.txt"

            [System.IO.File]::WriteAllText(
                $RemoteStationFile,
                $StationTag,
                $EncodingNoBom
            )

            # ------------------------------------------------
            # 5. WRITE ENVIRONMENT CONFIGURATION
            # ------------------------------------------------

            $RemoteEnvFile = Join-Path $RemoteRootUNC ".env"

            $EnvContent = @"
BROWSEGUARD_SERVER=$ServerUrl
STATION_NAME=$StationTag
"@

            [System.IO.File]::WriteAllText(
                $RemoteEnvFile,
                $EnvContent,
                $EncodingNoBom
            )

            # ------------------------------------------------
            # 6. CREATE INTERACTIVE LAUNCHER
            # ------------------------------------------------

            Write-Host "  Creating interactive launcher..." -ForegroundColor Gray

            $SafeServer = $ServerUrl.Replace("'", "''")
            $SafeStation = $StationTag.Replace("'", "''")

            $LauncherContent = @'
$ErrorActionPreference = "Stop"

$Root = "C:\ProgramData\Sentinel"
$Agent = Join-Path $Root "browseguard_agent.py"

$env:BROWSEGUARD_SERVER = '__SERVER__'
$env:STATION_NAME = '__STATION__'

if (-not (Test-Path -LiteralPath $Agent)) {
    exit 2
}

# Prevent duplicate launches of this agent in the same logged-in session.
$Existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -in @("python.exe", "pythonw.exe") -and
        $_.CommandLine -and
        $_.CommandLine.IndexOf(
            $Agent,
            [StringComparison]::OrdinalIgnoreCase
        ) -ge 0
    } |
    Select-Object -First 1

if ($Existing) {
    exit 0
}

# Prefer pythonw.exe (windowless) to prevent any command prompt flashing
$Candidates = @(
    "C:\Program Files\Python314\pythonw.exe",
    "C:\Program Files\Python313\pythonw.exe",
    "C:\Program Files\Python312\pythonw.exe",
    "C:\Program Files\Python311\pythonw.exe",
    "C:\Python314\pythonw.exe",
    "C:\Python313\pythonw.exe",
    "C:\Python312\pythonw.exe",
    "C:\Python311\pythonw.exe",
    "C:\Program Files\Python314\python.exe",
    "C:\Program Files\Python313\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python311\python.exe",
    "C:\Python314\python.exe",
    "C:\Python313\python.exe",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe"
)

$Python = $Candidates |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1

if (-not $Python) {
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue

    if ($Command) {
        $Python = $Command.Source
    }
}

if (-not $Python) {
    exit 3
}

$Stdout = Join-Path $Root "launcher_stdout.log"
$Stderr = Join-Path $Root "launcher_stderr.log"

try {
    Start-Process `
        -FilePath $Python `
        -ArgumentList ('"{0}"' -f $Agent) `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $Stdout `
        -RedirectStandardError $Stderr
}
catch {
    $_ | Out-File -FilePath $Stderr -Append
    exit 4
}
'@

            $LauncherContent = $LauncherContent.Replace(
                "__SERVER__",
                $SafeServer
            ).Replace(
                "__STATION__",
                $SafeStation
            )

            $RemoteLauncher =
            Join-Path $RemoteRootUNC "launch_agent.ps1"

            [System.IO.File]::WriteAllText(
                $RemoteLauncher,
                $LauncherContent,
                $EncodingNoBom
            )

            # ------------------------------------------------
            # 7. CREATE COMMON STARTUP ENTRY
            # ------------------------------------------------

            Write-Host "  Installing user logon launcher..." -ForegroundColor Gray

            # Clean up any legacy .cmd or .bat in Startup to stop black console popup
            $LegacyCmd = Join-Path $RemoteStartupUNC "SentinelAgent.cmd"
            if (Test-Path -LiteralPath $LegacyCmd) {
                Remove-Item -LiteralPath $LegacyCmd -Force -ErrorAction SilentlyContinue
            }
            $LegacyBat = Join-Path $RemoteStartupUNC "SentinelAgent.bat"
            if (Test-Path -LiteralPath $LegacyBat) {
                Remove-Item -LiteralPath $LegacyBat -Force -ErrorAction SilentlyContinue
            }

            # VBScript launcher: SW_HIDE (0) executes powershell with 100% hidden window (no black cmd flash)
            $StartupContent = @'
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\ProgramData\Sentinel\launch_agent.ps1""", 0, False
'@

            $RemoteStartupFile =
            Join-Path $RemoteStartupUNC $StartupFileName

            [System.IO.File]::WriteAllText(
                $RemoteStartupFile,
                $StartupContent,
                [System.Text.Encoding]::ASCII
            )

            # ------------------------------------------------
            # 8. REMOVE OLD SYSTEM TASK TO AVOID DUPLICATES
            # ------------------------------------------------

            if ($RemoveLegacySystemTask) {
                Write-Host "  Checking for legacy SYSTEM task..." -ForegroundColor Gray
                try {
                    $prevEAP = $ErrorActionPreference
                    $ErrorActionPreference = "SilentlyContinue"
                    cmd.exe /c "schtasks.exe /End /S $Station /TN SentinelAgent >nul 2>&1"
                    cmd.exe /c "schtasks.exe /Delete /S $Station /TN SentinelAgent /F >nul 2>&1"
                } catch {
                } finally {
                    $ErrorActionPreference = $prevEAP
                }
            }

            # ------------------------------------------------
            # 9. VERIFY DEPLOYED FILES
            # ------------------------------------------------

            $RequiredFiles = @(
                (Join-Path $RemoteRootUNC "browseguard_agent.py"),
                (Join-Path $RemoteRootUNC "station_name.txt"),
                (Join-Path $RemoteRootUNC ".env"),
                (Join-Path $RemoteRootUNC "launch_agent.ps1"),
                $RemoteStartupFile
            )

            foreach ($File in $RequiredFiles) {
                if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
                    throw "Deployment verification failed: $File"
                }
            }

            $VerifiedHash = (
                Get-FileHash -LiteralPath $RemoteAgentUNC -Algorithm SHA256
            ).Hash

            if ($VerifiedHash -ne $LocalHash) {
                throw "Final agent checksum verification failed."
            }

            $Status = "DEPLOYED"
            $Details = "Agent copied and verified. Interactive launcher installed. Starts at next user logon."

            $SuccessCount++

            Write-Host "  DEPLOYED SUCCESSFULLY" -ForegroundColor Green
            Write-Host "  Station ID: $StationTag" -ForegroundColor Gray
        }
    }
    catch {

        $Status = "FAILED"
        $Details = $_.Exception.Message

        $FailedCount++

        Write-Host "  FAILED: $Details" -ForegroundColor Red
    }

    $Results.Add(
        [PSCustomObject]@{
            Computer = $Station
            Status   = $Status
            Details  = $Details
            Date     = Get-Date
        }
    )
}

# ------------------------------------------------------------
# EXPORT RESULTS
# ------------------------------------------------------------

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " DEPLOYMENT SUMMARY" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

Write-Host "Total targets       : $($Targets.Count)" -ForegroundColor White
Write-Host "Successfully copied : $SuccessCount" -ForegroundColor Green
Write-Host "Failed              : $FailedCount" -ForegroundColor Red
Write-Host "WhatIf              : $WhatIfCount" -ForegroundColor Yellow

$Results |
Export-Csv -LiteralPath $OutputCsv -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "Results CSV: $OutputCsv" -ForegroundColor Cyan
Write-Host ""
Write-Host "IMPORTANT:" -ForegroundColor Yellow
Write-Host "The interactive agent starts when a user next logs in."
Write-Host "A successful file deployment does not prove that the agent"
Write-Host "has connected to the server or is actively monitoring."
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
