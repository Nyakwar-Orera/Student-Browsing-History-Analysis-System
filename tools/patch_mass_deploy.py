"""
tools/patch_mass_deploy.py
Updates mass_deploy_all_labs.ps1:
1. Replaces SentinelAgent.cmd with SentinelAgent.vbs (completely eliminates cmd console popup on login).
2. Cleans up legacy SentinelAgent.cmd and SentinelAgent.bat.
3. Prefers pythonw.exe over python.exe in launch_agent.ps1.
"""
from pathlib import Path

DEPLOY_PATH = Path(r"C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\client_agent\mass_deploy_all_labs.ps1")

content = DEPLOY_PATH.read_text(encoding="utf-8")

# 1. Startup filename
content = content.replace(
    '$StartupFileName = "SentinelAgent.cmd"',
    '$StartupFileName = "SentinelAgent.vbs"'
)

# 2. Python candidates (prefer pythonw.exe)
old_candidates = '''# Use a centrally installed Python interpreter.
$Candidates = @(
    "C:\\Program Files\\Python314\\python.exe",
    "C:\\Program Files\\Python313\\python.exe",
    "C:\\Program Files\\Python312\\python.exe",
    "C:\\Program Files\\Python311\\python.exe",
    "C:\\Python314\\python.exe",
    "C:\\Python313\\python.exe",
    "C:\\Python312\\python.exe",
    "C:\\Python311\\python.exe"
)'''

new_candidates = '''# Prefer pythonw.exe (windowless) to prevent any command prompt flashing
$Candidates = @(
    "C:\\Program Files\\Python314\\pythonw.exe",
    "C:\\Program Files\\Python313\\pythonw.exe",
    "C:\\Program Files\\Python312\\pythonw.exe",
    "C:\\Program Files\\Python311\\pythonw.exe",
    "C:\\Python314\\pythonw.exe",
    "C:\\Python313\\pythonw.exe",
    "C:\\Python312\\pythonw.exe",
    "C:\\Python311\\pythonw.exe",
    "C:\\Program Files\\Python314\\python.exe",
    "C:\\Program Files\\Python313\\python.exe",
    "C:\\Program Files\\Python312\\python.exe",
    "C:\\Program Files\\Python311\\python.exe",
    "C:\\Python314\\python.exe",
    "C:\\Python313\\python.exe",
    "C:\\Python312\\python.exe",
    "C:\\Python311\\python.exe"
)'''

content = content.replace(old_candidates, new_candidates)

# 3. Startup content (VBScript with SW_HIDE = 0) & cleanup legacy cmd/bat
old_startup = '''            $StartupContent = @\'
@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\\ProgramData\\Sentinel\\launch_agent.ps1"
\'@

            $RemoteStartupFile =
            Join-Path $RemoteStartupUNC $StartupFileName

            [System.IO.File]::WriteAllText(
                $RemoteStartupFile,
                $StartupContent,
                [System.Text.Encoding]::ASCII
            )'''

new_startup = '''            # Clean up any legacy .cmd or .bat in Startup to stop black console popup
            $LegacyCmd = Join-Path $RemoteStartupUNC "SentinelAgent.cmd"
            if (Test-Path -LiteralPath $LegacyCmd) {
                Remove-Item -LiteralPath $LegacyCmd -Force -ErrorAction SilentlyContinue
            }
            $LegacyBat = Join-Path $RemoteStartupUNC "SentinelAgent.bat"
            if (Test-Path -LiteralPath $LegacyBat) {
                Remove-Item -LiteralPath $LegacyBat -Force -ErrorAction SilentlyContinue
            }

            # VBScript launcher: SW_HIDE (0) executes powershell with 100% hidden window (no black cmd flash)
            $StartupContent = @\'
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\\ProgramData\\Sentinel\\launch_agent.ps1""", 0, False
\'@

            $RemoteStartupFile =
            Join-Path $RemoteStartupUNC $StartupFileName

            [System.IO.File]::WriteAllText(
                $RemoteStartupFile,
                $StartupContent,
                [System.Text.Encoding]::ASCII
            )'''

content = content.replace(old_startup, new_startup)

DEPLOY_PATH.write_text(content, encoding="utf-8")
print("SUCCESS: Updated mass_deploy_all_labs.ps1 with VBS stealth launcher & pythonw support.")

