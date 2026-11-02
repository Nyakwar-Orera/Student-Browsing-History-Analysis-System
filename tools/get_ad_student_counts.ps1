# tools/get_ad_student_counts.ps1
Import-Module ActiveDirectory -ErrorAction SilentlyContinue
$b_active = (Get-ADUser -SearchBase 'OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi' -Filter {Enabled -eq $true} -ErrorAction SilentlyContinue).Count
$g_active = (Get-ADUser -SearchBase 'OU=TALEBAAT,OU=MAMAL,DC=mamal,DC=nbi' -Filter {Enabled -eq $true} -ErrorAction SilentlyContinue).Count
$b_total = (Get-ADUser -SearchBase 'OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi' -Filter * -ErrorAction SilentlyContinue).Count
$g_total = (Get-ADUser -SearchBase 'OU=TALEBAAT,OU=MAMAL,DC=mamal,DC=nbi' -Filter * -ErrorAction SilentlyContinue).Count
[PSCustomObject]@{
    talabaat = [int]$b_active
    talebaat = [int]$g_active
    total = [int]($b_active + $g_active)
    talabaat_total = [int]$b_total
    talebaat_total = [int]$g_total
    all_total = [int]($b_total + $g_total)
} | ConvertTo-Json -Compress
