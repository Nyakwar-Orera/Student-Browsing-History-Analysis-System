# monitor_dashboard.ps1
# Continuous health monitor for MamalBrowse Sentinel server

param(
    [string]$ServerUrl = "http://localhost:5050",
    [int]$IntervalSeconds = 30
)

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "   BROWSEGUARD ENTERPRISE — SERVICE MONITOR" -ForegroundColor Cyan
Write-Host "   Monitoring: $ServerUrl (Interval: ${IntervalSeconds}s)" -ForegroundColor DarkGray
Write-Host "================================================================" -ForegroundColor Cyan

while ($true) {
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    try {
        $response = Invoke-WebRequest -Uri "$ServerUrl/api/classes" -TimeoutSec 5 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            Write-Host "[$timestamp] [HEALTHY] HTTP 200 OK — MamalBrowse Sentinel is responsive" -ForegroundColor Green
        } else {
            Write-Host "[$timestamp] [WARNING] HTTP $($response.StatusCode)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[$timestamp] [DOWN] Unable to reach $ServerUrl : $($_.Exception.Message)" -ForegroundColor Red
    }

    Start-Sleep -Seconds $IntervalSeconds
}
