# restart_server.ps1
# Restarts MamalBrowse Sentinel using WMI process creation
# WMI-spawned processes are detached from the calling shell's Job Object
# and survive after the shell session ends.

$ScriptDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "$ScriptDir\MamalLab\Scripts\python.exe"
$PidFile = "$ScriptDir\data\server_pids.txt"

Write-Host "=== MamalBrowse Sentinel Restart ===" -ForegroundColor Cyan

# --- Kill existing processes ---
if (Test-Path $PidFile) {
    Get-Content $PidFile | ForEach-Object {
        if ($_ -match '=(\d+)') {
            $targetPid = [int]$Matches[1]
            $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $targetPid -Force
                Write-Host "Killed PID $targetPid ($($proc.ProcessName))" -ForegroundColor Yellow
            }
        }
    }
}

# Also kill any stale python processes running app_production or tunnel_manager
Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object {
    $id = $_.Id
    $cmd = (Get-WmiObject Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue).CommandLine
    if ($cmd -match 'app_production|tunnel_manager') {
        Stop-Process -Id $id -Force
        Write-Host "Killed stale PID $id" -ForegroundColor Yellow
    }
}
Start-Sleep -Seconds 2

# --- Spawn app via WMI (detached from Job Object) ---
Write-Host "Starting app_production.py via WMI..." -ForegroundColor Green
$appResult = ([wmiclass]"Win32_Process").Create("$Python app_production.py", $ScriptDir, $null)
if ($appResult.ReturnValue -eq 0) {
    $appPid = $appResult.ProcessId
    Write-Host "App started: PID $appPid" -ForegroundColor Green
} else {
    Write-Host "FAILED to start app (ReturnValue=$($appResult.ReturnValue))" -ForegroundColor Red
    exit 1
}

# --- Spawn tunnel manager via WMI ---
Write-Host "Starting tunnel_manager.py via WMI..." -ForegroundColor Green
$tunnelResult = ([wmiclass]"Win32_Process").Create("$Python tools\tunnel_manager.py", $ScriptDir, $null)
if ($tunnelResult.ReturnValue -eq 0) {
    $tunnelPid = $tunnelResult.ProcessId
    Write-Host "Tunnel started: PID $tunnelPid" -ForegroundColor Green
} else {
    Write-Host "WARNING: Tunnel failed to start (ReturnValue=$($tunnelResult.ReturnValue))" -ForegroundColor Yellow
    $tunnelPid = 0
}

# --- Save PIDs ---
"APP_PID=$appPid`nTUNNEL_PID=$tunnelPid" | Out-File $PidFile -Encoding utf8

# --- Verify ---
Start-Sleep -Seconds 5
$r = curl.exe -s -o nul -w "%{http_code}" http://localhost:5050
Write-Host "=== HTTP Status: $r ===" -ForegroundColor Cyan
if ($r -eq "200") {
    Write-Host "Server is UP and responding." -ForegroundColor Green
} else {
    Write-Host "Server did not respond (may still be starting)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "PIDs saved to: $PidFile"
Write-Host "App PID: $appPid | Tunnel PID: $tunnelPid"

