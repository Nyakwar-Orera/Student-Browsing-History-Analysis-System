# tools/get_ad_student_roster.ps1
Import-Module ActiveDirectory -ErrorAction SilentlyContinue

$results = @()

$usersB = Get-ADUser -SearchBase 'OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi' -Filter * -Properties DisplayName, Enabled, DistinguishedName, Mail
foreach ($u in $usersB) {
    $results += [PSCustomObject]@{
        trno = $u.sAMAccountName
        name = $u.DisplayName
        enabled = [bool]$u.Enabled
        dn = $u.DistinguishedName
        gender = "Boys"
    }
}

$usersG = Get-ADUser -SearchBase 'OU=TALEBAAT,OU=MAMAL,DC=mamal,DC=nbi' -Filter * -Properties DisplayName, Enabled, DistinguishedName, Mail
foreach ($u in $usersG) {
    $results += [PSCustomObject]@{
        trno = $u.sAMAccountName
        name = $u.DisplayName
        enabled = [bool]$u.Enabled
        dn = $u.DistinguishedName
        gender = "Girls"
    }
}

$jsonPath = Join-Path $PSScriptRoot "..\data\ad_ou_students.json"
$results | ConvertTo-Json -Depth 2 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output "Extracted $($results.Count) students from AD OUs to $jsonPath"
