# ============================================================
#  MAMALBROWSE SENTINEL — SCHEDULED TASKS SETUP
#  Run once as Administrator on the server (10.10.117.76)
#  Registers all automated background maintenance tasks in
#  Windows Task Scheduler.
#
#  Tasks registered:
#    1. MamalBrowse-AutoEnable    — Daily 06:00 (+ on boot)
#       Checks if suspended students (Darajah 1-7) have served
#       their full 14-day block and re-enables them automatically.
#       Darajah 8-11 students are NEVER re-enabled (laptop directive).
#
#    2. MamalBrowse-RetentionSweep — Hourly 00:00 onward
#       Archives audit logs older than 14 days and runs the
#       in-process retention sweep (backup for Flask thread).
#
#  Usage:
#    Right-click -> "Run with PowerShell" as Administrator
#    -- OR --
#    powershell -ExecutionPolicy Bypass -File tools\setup_scheduled_tasks.ps1
# ============================================================
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python     = (Get-Command python -ErrorAction SilentlyContinue)?.Source

# Prefer venv python if it exists
$VenvPython = Join-Path $ScriptDir "MamalLab\Scripts\python.exe"
if (Test-Path $VenvPython) { $Python = $VenvPython }

if (-not $Python -or -not (Test-Path $Python)) {
    Write-Host "[ERROR] Python not found. Install Python 3.9+ and ensure it is on PATH." -ForegroundColor Red
    exit 1
}

$AutoEnableScript = Join-Path $ScriptDir "tools\auto_enable_expired_suspensions.py"
$LogsDir          = Join-Path $ScriptDir "logs"

# Ensure logs directory exists
New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null

Write-Host ""
Write-Host "  +============================================================+" -ForegroundColor Magenta
Write-Host "  |   MAMALBROWSE SENTINEL — TASK SCHEDULER SETUP             |" -ForegroundColor Magenta
Write-Host "  |   Server: $($env:COMPUTERNAME)                                         |" -ForegroundColor Magenta
Write-Host "  +============================================================+" -ForegroundColor Magenta
Write-Host ""
Write-Host "  Python       : $Python" -ForegroundColor DarkCyan
Write-Host "  Script Dir   : $ScriptDir" -ForegroundColor DarkCyan
Write-Host "  Auto-Enable  : $AutoEnableScript" -ForegroundColor DarkCyan
Write-Host ""


# ─────────────────────────────────────────────────────────────
# TASK 1  — Daily 14-Day Suspension Auto-Enable Sweep
#           Runs at 06:00 every day + once at system startup
# ─────────────────────────────────────────────────────────────
$task1Name   = "MamalBrowse-AutoEnable"
$task1Action = New-ScheduledTaskAction `
    -Execute    $Python `
    -Argument   "`"$AutoEnableScript`"" `
    -WorkingDirectory $ScriptDir

# Trigger 1: Daily at 06:00
$trigger1Daily = New-ScheduledTaskTrigger -Daily -At "06:00"

# Trigger 2: At system startup (catches any missed runs if server was off at 06:00)
$trigger1Boot  = New-ScheduledTaskTrigger -AtStartup

$task1Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  (New-TimeSpan -Minutes 10) `
    -RestartCount        2 `
    -RestartInterval     (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable  `
    -RunOnlyIfNetworkAvailable:$false `
    -MultipleInstances   IgnoreNew

$task1Principal = New-ScheduledTaskPrincipal `
    -UserId    "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel  Highest

# Remove any existing task first
Unregister-ScheduledTask -TaskName $task1Name -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName    $task1Name `
    -TaskPath    "\MamalBrowse\" `
    -Action      $task1Action `
    -Trigger     @($trigger1Daily, $trigger1Boot) `
    -Settings    $task1Settings `
    -Principal   $task1Principal `
    -Description "MamalBrowse Sentinel: Daily 14-day suspension auto-expiry sweep. Re-enables Darajah 1-7 students who have completed their 14-day block. Darajah 8-11 students remain permanently disabled per institutional laptop directive." `
    | Out-Null

Write-Host "  [OK] Task '$task1Name' registered:" -ForegroundColor Green
Write-Host "       Schedule : Daily at 06:00 + at System Startup" -ForegroundColor DarkGray
Write-Host "       Script   : $AutoEnableScript" -ForegroundColor DarkGray
Write-Host "       Run As   : SYSTEM (elevated)" -ForegroundColor DarkGray
Write-Host "       Log      : $LogsDir\retention_auto_enable.log" -ForegroundColor DarkGray
Write-Host ""


# ─────────────────────────────────────────────────────────────
# TASK 2  — Hourly Retention Sweep (backup for in-process thread)
#           Calls the same auto_enable script every hour
#           This is a safety net: the Flask app already runs
#           an in-process hourly sweep, but this guarantees
#           execution even when Flask restarts mid-cycle.
# ─────────────────────────────────────────────────────────────
$task2Name   = "MamalBrowse-RetentionSweep"
$task2Action = New-ScheduledTaskAction `
    -Execute    $Python `
    -Argument   "`"$AutoEnableScript`" --quiet" `
    -WorkingDirectory $ScriptDir

# Trigger: Every hour, starting at midnight
$trigger2Hourly = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -Once -At "00:00"

$task2Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  (New-TimeSpan -Minutes 5) `
    -RestartCount        1 `
    -RestartInterval     (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable  `
    -RunOnlyIfNetworkAvailable:$false `
    -MultipleInstances   IgnoreNew

$task2Principal = New-ScheduledTaskPrincipal `
    -UserId    "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel  Highest

Unregister-ScheduledTask -TaskName $task2Name -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName    $task2Name `
    -TaskPath    "\MamalBrowse\" `
    -Action      $task2Action `
    -Trigger     $trigger2Hourly `
    -Settings    $task2Settings `
    -Principal   $task2Principal `
    -Description "MamalBrowse Sentinel: Hourly retention policy sweep (backup for Flask in-process thread). Ensures suspended accounts are auto-expired even during Flask restarts." `
    | Out-Null

Write-Host "  [OK] Task '$task2Name' registered:" -ForegroundColor Green
Write-Host "       Schedule : Every 1 hour (starting midnight)" -ForegroundColor DarkGray
Write-Host "       Script   : $AutoEnableScript --quiet" -ForegroundColor DarkGray
Write-Host "       Run As   : SYSTEM (elevated)" -ForegroundColor DarkGray
Write-Host ""


# ─────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  +============================================================+" -ForegroundColor Green
Write-Host "  |              SCHEDULED TASKS SETUP COMPLETE                |" -ForegroundColor Green
Write-Host "  +============================================================+" -ForegroundColor Green
Write-Host ""

# List what we registered
$tasks = Get-ScheduledTask -TaskPath "\MamalBrowse\" -ErrorAction SilentlyContinue
if ($tasks) {
    Write-Host "  Registered tasks in \MamalBrowse\ folder:" -ForegroundColor White
    foreach ($t in $tasks) {
        $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue
        $nextRun = if ($info) { $info.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "Unknown" }
        Write-Host "    - $($t.TaskName)  [State: $($t.State)]  Next run: $nextRun" -ForegroundColor DarkCyan
    }
} else {
    Write-Host "  (No tasks found in \MamalBrowse\ — check Task Scheduler manually)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Manage tasks:" -ForegroundColor White
Write-Host "    Open Task Scheduler : taskschd.msc" -ForegroundColor DarkGray
Write-Host "    Run now             : Start-ScheduledTask -TaskPath '\MamalBrowse\' -TaskName 'MamalBrowse-AutoEnable'" -ForegroundColor DarkGray
Write-Host "    Remove all          : Get-ScheduledTask -TaskPath '\MamalBrowse\' | Unregister-ScheduledTask -Confirm:`$false" -ForegroundColor DarkGray
Write-Host ""

