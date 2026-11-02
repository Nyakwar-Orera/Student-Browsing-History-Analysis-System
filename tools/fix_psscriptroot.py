from pathlib import Path

p = Path(r"C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\client_agent\mass_deploy_all_labs.ps1")
txt = p.read_text(encoding="utf-8")

old_source = '[string]$AgentSource = (Join-Path $PSScriptRoot "browseguard_agent.py"),'
new_source = '[string]$AgentSource = "",'

txt = txt.replace(old_source, new_source)

old_error = '$ErrorActionPreference = "Stop"'
new_error = '''$ErrorActionPreference = "Stop"

if (-not $PSScriptRoot) {
    $PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    if (-not $PSScriptRoot) {
        $PSScriptRoot = "C:\\Users\\admin\\Desktop\\Student_Browsing_History_Analysis_System\\client_agent"
    }
}
if (-not $AgentSource) {
    $AgentSource = Join-Path $PSScriptRoot "browseguard_agent.py"
}'''

txt = txt.replace(old_error, new_error, 1)
p.write_text(txt, encoding="utf-8")
print("PSScriptRoot fix applied!")

