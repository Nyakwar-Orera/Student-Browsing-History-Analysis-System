from pathlib import Path

p = Path(r"C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\client_agent\mass_deploy_all_labs.ps1")
txt = p.read_text(encoding="utf-8")

old_schtasks = '''            if ($RemoveLegacySystemTask) {

                Write-Host "  Checking for legacy SYSTEM task..." -ForegroundColor Gray

                # Only target the old task created by the previous
                # Sentinel deployment scripts.
                & schtasks.exe /End /S $Station /TN "SentinelAgent" 2>&1 |
                Out-Null

                & schtasks.exe /Delete /S $Station /TN "SentinelAgent" /F 2>&1 |
                Out-Null
            }'''

new_schtasks = '''            if ($RemoveLegacySystemTask) {
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
            }'''

txt = txt.replace(old_schtasks, new_schtasks)
p.write_text(txt, encoding="utf-8")
print("schtasks error tolerance applied!")

