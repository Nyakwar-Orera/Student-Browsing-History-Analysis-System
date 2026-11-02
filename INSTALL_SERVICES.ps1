# ================================================================
#  MAMALBROWSE SENTINEL - MASTER AUTOMATIC STARTUP INSTALLER
#  Run once as Administrator on the server (10.10.117.76)
#  After this, everything starts automatically on every boot.
# ================================================================
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython  = Join-Path $ScriptDir "MamalLab\Scripts\python.exe"
$AppScript   = Join-Path $ScriptDir "app_production.py"
$ToolsDir    = Join-Path $ScriptDir "tools"
$NssmExe     = Join-Path $ToolsDir "nssm.exe"   # Place nssm.exe inside tools\

$SentinelSvc  = "MamalBrowseSentinel"
$TunnelSvc    = "MamalBrowseTunnel"
$DashboardUrl = "http://localhost:5050"

# Helper functions
function Write-Step { param([string]$Num, [string]$Msg)
    Write-Host ""
    Write-Host "  [$Num] $Msg" -ForegroundColor Cyan }

function Write-OK   { param([string]$Msg)
    Write-Host "       OK  $Msg" -ForegroundColor Green }

function Write-Warn { param([string]$Msg)
    Write-Host "       WARN  $Msg" -ForegroundColor Yellow }

function Write-Fail { param([string]$Msg)
    Write-Host "       FAIL  $Msg" -ForegroundColor Red }

function Install-NssmService {
    param(
        [string]$ServiceName,
        [string]$DisplayName,
        [string]$Description,
        [string]$ExePath,
        [string]$Arguments,
        [string]$WorkDir,
        [string]$LogFile,
        [string]$ErrFile,
        [int]   $StartupDelay = 0
    )
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Warn "Service '$ServiceName' already exists - removing it first..."
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        & $NssmExe remove $ServiceName confirm 2>$null
        Start-Sleep -Seconds 2
    }
    & $NssmExe install $ServiceName $ExePath $Arguments
    & $NssmExe set     $ServiceName DisplayName  $DisplayName
    & $NssmExe set     $ServiceName Description  $Description
    & $NssmExe set     $ServiceName AppDirectory $WorkDir
    & $NssmExe set     $ServiceName AppStdout    $LogFile
    & $NssmExe set     $ServiceName AppStderr    $ErrFile
    & $NssmExe set     $ServiceName AppStdoutCreationDisposition 4
    & $NssmExe set     $ServiceName AppStderrCreationDisposition 4
    & $NssmExe set     $ServiceName AppRotateFiles 1
    & $NssmExe set     $ServiceName AppRotateSeconds 86400
    & $NssmExe set     $ServiceName AppRotateBytes 10485760
    & $NssmExe set     $ServiceName Start SERVICE_AUTO_START
    & $NssmExe set     $ServiceName ObjectName  LocalSystem
    & $NssmExe set     $ServiceName Type        SERVICE_WIN32_OWN_PROCESS
    & $NssmExe set     $ServiceName AppRestartDelay 5000
    & $NssmExe set     $ServiceName AppExit      Default  Restart
    if ($StartupDelay -gt 0) {
        & $NssmExe set $ServiceName AppThrottle $StartupDelay
    }
}

# BANNER
Clear-Host
Write-Host ""
Write-Host "  +============================================================+" -ForegroundColor Magenta
Write-Host "  |   MAMALBROWSE SENTINEL - AUTOMATIC STARTUP INSTALLER       |" -ForegroundColor Magenta
Write-Host "  |   Server: $($env:COMPUTERNAME)  |  NSSM Windows Services              |" -ForegroundColor Magenta
Write-Host "  +============================================================+" -ForegroundColor Magenta
Write-Host ""

# STEP 1 - Pre-flight checks
Write-Step "1/7" "Running pre-flight checks..."

if (-not (Test-Path $VenvPython)) {
    Write-Fail "Python venv not found at: $VenvPython"
    Write-Host "         Create it: python -m venv MamalLab" -ForegroundColor Yellow
    exit 1
}
Write-OK "Python venv: $VenvPython"

if (-not (Test-Path $NssmExe)) {
    Write-Fail "nssm.exe not found at: $NssmExe"
    Write-Host "         Download from https://nssm.cc and place nssm.exe inside the tools\ folder." -ForegroundColor Yellow
    exit 1
}
Write-OK "NSSM: $NssmExe"

$PolicyFile = Join-Path $ScriptDir "config\access_policy.yaml"
if (-not (Test-Path $PolicyFile)) {
    Write-Fail "config\access_policy.yaml not found."
    exit 1
}
Write-OK "Access policy: $PolicyFile"

foreach ($d in @("$ScriptDir\data", "$ScriptDir\logs", "$ScriptDir\students\violations")) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Write-OK "Data directories OK"

Write-Host "       Installing Python dependencies..." -ForegroundColor DarkGray
& $VenvPython -m pip install -r "$ScriptDir\requirements.txt" --quiet
Write-OK "Python dependencies OK"

# STEP 2 - Install MamalBrowse Sentinel service
Write-Step "2/7" "Installing MamalBrowse Sentinel as Windows Service..."

$bgLog = Join-Path $ScriptDir "logs\sentinel_service.log"
$bgErr = Join-Path $ScriptDir "logs\sentinel_service_err.log"

Install-NssmService `
    -ServiceName $SentinelSvc `
    -DisplayName "MamalBrowse Sentinel Server" `
    -Description "MamalBrowse Sentinel AJSN - Student browsing analysis, session enforcement, and live dashboard (Flask/Waitress port 5050)" `
    -ExePath     $VenvPython `
    -Arguments   "`"$AppScript`"" `
    -WorkDir     $ScriptDir `
    -LogFile     $bgLog `
    -ErrFile     $bgErr

Write-OK "Service '$SentinelSvc' installed (auto-start, restart-on-crash)"

# STEP 2b - Install Cloudflare Tunnel service
Write-Step "2b/7" "Installing MamalBrowse Cloudflare Tunnel as Windows Service..."

$TunnelScript = Join-Path $ScriptDir "tools\tunnel_manager.py"
$cfLog        = Join-Path $ScriptDir "logs\tunnel_service.log"
$cfErr        = Join-Path $ScriptDir "logs\tunnel_service_err.log"

Install-NssmService `
    -ServiceName $TunnelSvc `
    -DisplayName "MamalBrowse Cloudflare Tunnel" `
    -Description "MamalBrowse Sentinel Secure External HTTPS Tunnel (Cloudflare trycloudflare.com)" `
    -ExePath     $VenvPython `
    -Arguments   "`"$TunnelScript`"" `
    -WorkDir     $ScriptDir `
    -LogFile     $cfLog `
    -ErrFile     $cfErr

Write-OK "Service '$TunnelSvc' installed (auto-start, restart-on-crash)" 

# STEP 3 - Configure Windows Firewall
Write-Step "3/7" "Configuring Windows Firewall rules..."

Remove-NetFirewallRule -DisplayName "MamalBrowse Sentinel-Dashboard" -ErrorAction SilentlyContinue
New-NetFirewallRule `
    -DisplayName  "MamalBrowse Sentinel-Dashboard" `
    -Description  "MamalBrowse Sentinel web dashboard and API" `
    -Direction    Inbound `
    -Protocol     TCP `
    -LocalPort    5050 `
    -Action       Allow `
    -Profile      Any | Out-Null
Write-OK "Firewall: TCP 5050 ALLOW"

# STEP 4 - Desktop shortcut
Write-Step "4/7" "Creating dashboard desktop shortcut..."

$desktopPath  = [System.Environment]::GetFolderPath("CommonDesktopDirectory")
$shortcutPath = Join-Path $desktopPath "Sentinel Dashboard.lnk"
$wshell = New-Object -ComObject WScript.Shell
$sc = $wshell.CreateShortcut($shortcutPath)
$sc.TargetPath  = "$env:SystemRoot\System32\rundll32.exe"
$sc.Arguments   = "url.dll,FileProtocolHandler http://localhost:5050"
$sc.Description = "Open MamalBrowse Sentinel Dashboard"
$sc.Save()
Write-OK "Desktop shortcut created: $shortcutPath"

# STEP 5 - Push client agent to lab PC share
Write-Step "5/7" "Staging client enforcement agent on network share..."

$AgentSource  = Join-Path $ScriptDir "client_agent\browseguard_agent.py"
$AgentInstall = Join-Path $ScriptDir "client_agent\install_agent.bat"
$shareLocalPath = "C:\Maintenance\MamalBrowse Sentinel"
$shareName      = "Maintenance"

New-Item -ItemType Directory -Path $shareLocalPath -Force | Out-Null
$existingShare = Get-SmbShare -Name $shareName -ErrorAction SilentlyContinue
if (-not $existingShare) {
    New-SmbShare -Name $shareName -Path "C:\Maintenance" -ReadAccess "Everyone" | Out-Null
    Write-OK "Created network share: \\$($env:COMPUTERNAME)\$shareName"
} else {
    Write-OK "Network share exists: \\$($env:COMPUTERNAME)\$shareName"
}
if (Test-Path $AgentSource) {
    Copy-Item -Path $AgentSource -Destination "$shareLocalPath\browseguard_agent.py" -Force
    Write-OK "Copied browseguard_agent.py to share"
}
if (Test-Path $AgentInstall) {
    Copy-Item -Path $AgentInstall -Destination "$shareLocalPath\install_agent.bat" -Force
    Write-OK "Copied install_agent.bat to share"
}
(Get-Date -Format "yyyy-MM-dd HH:mm:ss") | Set-Content "$shareLocalPath\version.txt"
# STEP 6 - Register Windows Scheduled Tasks (auto-enable + retention sweep)
Write-Step "6/7" "Registering Windows Task Scheduler jobs..."

$SetupTasksScript = Join-Path $ScriptDir "tools\setup_scheduled_tasks.ps1"
if (Test-Path $SetupTasksScript) {
    try {
        & powershell -ExecutionPolicy Bypass -File $SetupTasksScript
        Write-OK "Scheduled tasks registered (MamalBrowse-AutoEnable + MamalBrowse-RetentionSweep)"
    } catch {
        Write-Warn "Scheduled task registration failed: $_"
        Write-Warn "Run manually: powershell -ExecutionPolicy Bypass -File tools\setup_scheduled_tasks.ps1"
    }
} else {
    Write-Warn "setup_scheduled_tasks.ps1 not found. Skipping scheduled task registration."
}

# STEP 7 - Start the service
Write-Step "7/7" "Starting MamalBrowse Sentinel service..."

try {
    Start-Service -Name $SentinelSvc -ErrorAction Stop
    Write-OK "MamalBrowse Sentinel Server service STARTED"
} catch {
    Write-Warn "Sentinel start warning: $_"
}

try {
    Start-Service -Name $TunnelSvc -ErrorAction Stop
    Write-OK "MamalBrowse Cloudflare Tunnel service STARTED"
} catch {
    Write-Warn "Start warning: $_"
}
Start-Sleep -Seconds 6

# SUMMARY
$svc = Get-Service -Name $SentinelSvc -ErrorAction SilentlyContinue
$svcTunnel = Get-Service -Name $TunnelSvc -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "  +============================================================+" -ForegroundColor Green
Write-Host "  |              INSTALLATION COMPLETE                         |" -ForegroundColor Green
Write-Host "  +============================================================+" -ForegroundColor Green
Write-Host ""
Write-Host "  SERVICE STATUS:" -ForegroundColor White
$svcColor = if ($svc -and $svc.Status -eq "Running") { "Green" } else { "Red" }
Write-Host "    MamalBrowse Sentinel Server   : $($svc.Status)" -ForegroundColor $svcColor
$tColor = if ($svcTunnel -and $svcTunnel.Status -eq "Running") { "Green" } else { "Red" }
Write-Host "    MamalBrowse Cloudflare Tunnel : $($svcTunnel.Status)" -ForegroundColor $tColor
Write-Host ""
Write-Host "  STARTUP  : Service AUTO-STARTS on every boot" -ForegroundColor DarkCyan
Write-Host "  RECOVERY : NSSM auto-restarts 5s after any crash" -ForegroundColor DarkCyan
Write-Host "  LOGS     : $ScriptDir\logs\" -ForegroundColor DarkCyan
Write-Host "  TASKS    : taskschd.msc -> \MamalBrowse\ (AutoEnable daily 06:00, Sweep hourly)" -ForegroundColor DarkCyan
Write-Host "  DASHBOARD: http://localhost:5050  |  http://10.10.117.76:5050" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "  CLIENT PCS: Run as Admin on each lab workstation:" -ForegroundColor DarkGray
Write-Host "    \\$($env:COMPUTERNAME)\Maintenance\MamalBrowse Sentinel\install_agent.bat" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  MANAGE SERVICE:" -ForegroundColor White
Write-Host "    Start   : Start-Service $SentinelSvc" -ForegroundColor DarkGray
Write-Host "    Stop    : Stop-Service  $SentinelSvc" -ForegroundColor DarkGray
Write-Host "    Status  : Get-Service   $SentinelSvc" -ForegroundColor DarkGray
Write-Host "    Uninstall: .\UNINSTALL_SERVICES.ps1" -ForegroundColor DarkGray
Write-Host ""
