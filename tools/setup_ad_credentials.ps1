# setup_ad_credentials.ps1
# ============================================================
# Stores AD credentials for MamalBrowse Sentinel service.
# Run this ONCE as local Administrator after changing the
# domain admin password or setting up the service.
#
# The credentials are stored in data/ad_credentials.json
# and used by the service when calling Enable/Disable-ADAccount.
# ============================================================

$projectRoot = "c:\Users\admin\Desktop\Student_Browsing_History_Analysis_System"
$credFile = Join-Path $projectRoot "data\ad_credentials.json"

Write-Host "=== MamalBrowse Sentinel - AD Credential Setup ==="
Write-Host ""
Write-Host "This stores the domain admin password so the Sentinel service"
Write-Host "can enable/disable student AD accounts automatically."
Write-Host ""
Write-Host "Credentials stored in: $credFile"
Write-Host "(Keep this file secure - it contains a plaintext password)"
Write-Host ""

$domainUser = Read-Host "Domain username (e.g. MAMAL\admin)"
$secPwd = Read-Host "Password" -AsSecureString
$plainPwd = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secPwd)
)

# Test the credentials against AD first
Write-Host ""
Write-Host "Testing credentials against mamal.nbi..."
try {
    $ctx = New-Object System.DirectoryServices.ActiveDirectory.DirectoryContext(
        "Domain", "mamal.nbi", $domainUser, $plainPwd
    )
    $domain = [System.DirectoryServices.ActiveDirectory.Domain]::GetDomain($ctx)
    Write-Host "Credential test: PASSED (Domain: $($domain.Name))"
} catch {
    Write-Host "WARNING: Credential test failed: $_"
    Write-Host "Credentials may be incorrect. Proceeding anyway..."
}

# Save to JSON
$credObj = @{ user = $domainUser; password = $plainPwd } | ConvertTo-Json
Set-Content -Path $credFile -Value $credObj -Encoding UTF8

Write-Host ""
Write-Host "Credentials saved to: $credFile"
Write-Host ""
Write-Host "Restart the MamalBrowseSentinel service to apply:"
Write-Host "  net stop MamalBrowseSentinel && net start MamalBrowseSentinel"

