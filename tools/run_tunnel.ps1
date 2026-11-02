# tools/run_tunnel.ps1 - Runs Cloudflare Tunnel and saves the current public HTTPS URL
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BaseDir   = Split-Path -Parent $ScriptDir
$Cloudflared = Join-Path $ScriptDir "cloudflared.exe"
$LogsDir     = Join-Path $BaseDir "logs"
$DataDir     = Join-Path $BaseDir "data"
$LogFile     = Join-Path $LogsDir "cloudflared.log"
$UrlFile     = Join-Path $DataDir "external_url.txt"

New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

Write-Host "Starting Cloudflare Zero-Trust Secure Tunnel for Port 5050..." -ForegroundColor Cyan

# Check if cloudflared is already running
$existing = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
if (-not $existing) {
    Start-Process -FilePath $Cloudflared -ArgumentList "tunnel --url http://localhost:5050 --logfile `"$LogFile`"" -WindowStyle Hidden
    Start-Sleep -Seconds 5
}

# Extract URL from log
$url = $null
for ($i = 0; $i -lt 15; $i++) {
    if (Test-Path $LogFile) {
        $lines = Get-Content $LogFile -Tail 50 -ErrorAction SilentlyContinue
        foreach ($line in $lines) {
            if ($line -match "https://[a-zA-Z0-9\-]+\.trycloudflare\.com") {
                $url = $matches[0]
            }
        }
        if ($url) { break }
    }
    Start-Sleep -Seconds 1
}

if ($url) {
    $url | Set-Content $UrlFile -Encoding UTF8
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  MAMALBROWSE SENTINEL - DUAL ACCESS ONLINE" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  INTERNAL (Lab & Server): http://10.10.117.76:5050" -ForegroundColor White
    Write-Host "  EXTERNAL (Anywhere)    : $url" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  URL saved to: $UrlFile" -ForegroundColor DarkGray
    Write-Host ""
} else {
    Write-Host "[WARN] Could not retrieve tunnel URL yet. Check $LogFile" -ForegroundColor Yellow
}

