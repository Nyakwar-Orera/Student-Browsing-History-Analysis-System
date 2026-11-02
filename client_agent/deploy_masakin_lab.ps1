<#
.SYNOPSIS
    MamalBrowse Sentinel — Masakin Lab (MK) Targeted Deployment
    Deploys v2.1 agent to MK-40 through MK-59 (20 computers).
#>

$ErrorActionPreference = 'SilentlyContinue'

$AgentSource = 'C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\client_agent\browseguard_agent.py'
$ServerUrl   = 'http://10.10.117.76:5050'
$PipPackages = 'requests urllib3 certifi charset-normalizer idna'

Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  MASAKIN LAB (MK) — TARGETED DEPLOYMENT v2.1' -ForegroundColor Cyan
Write-Host '  YouTube Smart Filter + Live Warns' -ForegroundColor DarkCyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

# Masakin lab machine range (20 computers)
$Targets = @(
    'MK-40','MK-41','MK-42','MK-43','MK-44','MK-45','MK-46','MK-47',
    'MK-48','MK-49','MK-50','MK-51','MK-52','MK-53','MK-54','MK-55',
    'MK-56','MK-57','MK-58','MK-59'
)

Write-Host "Target stations: $($Targets.Count)" -ForegroundColor Cyan
Write-Host ''

$sopts = New-PSSessionOption -OperationTimeout 15000 -OpenTimeout 10000
$ok = 0; $offline = 0; $fail = 0
$Results = [System.Collections.Generic.List[PSCustomObject]]::new()

foreach ($Station in $Targets) {
    Write-Host ("  [{0,-10}] " -f $Station) -NoNewline

    # Ping check
    if (-not (Test-Connection -ComputerName $Station -Count 1 -Quiet)) {
        Write-Host 'OFFLINE' -ForegroundColor DarkYellow
        $Results.Add([PSCustomObject]@{ Station=$Station; Status='OFFLINE'; Note='' })
        $offline++; continue
    }

    $RemoteDir   = "\\$Station\C`$\ProgramData\Sentinel"
    $RemoteAgent = "$RemoteDir\browseguard_agent.py"

    try {
        # 1. Create Sentinel dir
        if (-not (Test-Path $RemoteDir)) {
            New-Item -Path $RemoteDir -ItemType Directory -Force | Out-Null
        }

        # 2. Copy updated agent
        Copy-Item -Path $AgentSource -Destination $RemoteAgent -Force -ErrorAction Stop

        # 3. Write station identity + server config
        $Station | Set-Content -Path "$RemoteDir\station_name.txt" -Encoding UTF8 -Force
        "BROWSEGUARD_SERVER=$ServerUrl`nSTATION_NAME=$Station" |
            Set-Content -Path "$RemoteDir\.env" -Encoding UTF8 -Force

        # 4. Write run.bat with dynamic Python path detection
        $batLines = @(
            '@echo off',
            'set PYPATH=C:\Program Files\Python314\python.exe',
            'if not exist "%PYPATH%" set PYPATH=C:\Program Files\Python313\python.exe',
            'if not exist "%PYPATH%" set PYPATH=python',
            '"%PYPATH%" "C:\ProgramData\Sentinel\browseguard_agent.py"'
        )
        ($batLines -join "`r`n") | Set-Content -Path "$RemoteDir\run.bat" -Encoding ASCII -Force

        # 5. Install pip dependencies via WinRM
        Invoke-Command -ComputerName $Station -ScriptBlock {
            param($pkgs)
            $py = 'C:\Program Files\Python314\python.exe'
            if (-not (Test-Path $py)) { $py = 'C:\Program Files\Python313\python.exe' }
            & $py -m pip install $pkgs.Split() --quiet --no-warn-script-location 2>&1 | Out-Null
        } -ArgumentList $PipPackages -SessionOption $sopts -ErrorAction SilentlyContinue

        # 6. Register SentinelAgent scheduled task
        #    Primary: remote schtasks with NT AUTHORITY\SYSTEM (avoids domain-trust "account name" error)
        schtasks /Delete /S $Station /TN SentinelAgent /F 2>$null | Out-Null
        schtasks /Create /S $Station /TN SentinelAgent `
                 /TR 'C:\ProgramData\Sentinel\run.bat' `
                 /SC ONLOGON /RU 'NT AUTHORITY\SYSTEM' /RL HIGHEST /F 2>$null | Out-Null
        $taskOk = ($LASTEXITCODE -eq 0)

        # Fallback: register task locally via WinRM Invoke-Command
        if (-not $taskOk) {
            $taskOk = Invoke-Command -ComputerName $Station -ScriptBlock {
                schtasks /Delete /TN SentinelAgent /F 2>$null | Out-Null
                schtasks /Create /TN SentinelAgent `
                         /TR 'C:\ProgramData\Sentinel\run.bat' `
                         /SC ONLOGON /RU 'NT AUTHORITY\SYSTEM' /RL HIGHEST /F 2>$null | Out-Null
                $LASTEXITCODE -eq 0
            } -SessionOption $sopts -ErrorAction SilentlyContinue
        }

        if ($taskOk) {
            Write-Host 'OK - INSTALLED & SCHEDULED' -ForegroundColor Green
            $Results.Add([PSCustomObject]@{ Station=$Station; Status='SUCCESS'; Note='SentinelAgent v2.1 registered' })
            $ok++
        } else {
            Write-Host 'PARTIAL - files copied, task needs manual schtasks' -ForegroundColor Yellow
            $Results.Add([PSCustomObject]@{ Station=$Station; Status='PARTIAL'; Note='agent copied, run install_agent.bat locally' })
            $ok++
        }
    } catch {
        Write-Host "ERROR: $_" -ForegroundColor Red
        $Results.Add([PSCustomObject]@{ Station=$Station; Status='ERROR'; Note=$_.ToString() })
        $fail++
    }
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  MASAKIN LAB DEPLOYMENT SUMMARY' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ("  Installed/Scheduled : {0}" -f $ok)      -ForegroundColor Green
Write-Host ("  Offline / Skipped   : {0}" -f $offline) -ForegroundColor Yellow
Write-Host ("  Errors              : {0}" -f $fail)     -ForegroundColor Red
Write-Host ''

$CsvPath = 'C:\Users\admin\Desktop\Sentinel_MK_Deploy_Results.csv'
$Results | Export-Csv -Path $CsvPath -NoTypeInformation -Encoding UTF8
Write-Host "  Results saved to: $CsvPath" -ForegroundColor Cyan
Write-Host ''
Write-Host '  Agents activate automatically on next student login.' -ForegroundColor White
Write-Host '  Browsing data appears in dashboard within 30s of login.' -ForegroundColor White
