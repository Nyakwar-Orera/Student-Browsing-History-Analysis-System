"""
tools/deploy_newly_online.py
Deploys Sentinel Agent v2.8.1 (Edge allowed + 3s auto-dismiss + zero-blink)
to all newly online workstations that were offline during earlier deployment.
"""
import concurrent.futures
import shutil
import subprocess
from pathlib import Path

TARGETS = ['M11', 'M25', 'M43', 'M44', 'MK-43', 'RB-50', 'RB-52', 'RB-53', 'RB-54', 'RB-58']
LOCAL_AGENT = Path(r"client_agent\browseguard_agent.py")
CRED_U = "MAMAL\\admin"
CRED_P = "100%SECUREDadmin"

VBS_CONTENT = (
    'Set WshShell = CreateObject("WScript.Shell")\r\n'
    'WshShell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\\ProgramData\\Sentinel\\launch_agent.ps1""", 0, False\r\n'
)

def deploy_to_station(station: str) -> tuple[str, bool, str]:
    try:
        fqdn = f"{station}.mamal.nbi"
        c_share = Path(f"\\\\{fqdn}\\C$")
        if not c_share.exists():
            return station, False, "C$ unreachable"

        root_unc = c_share / "ProgramData" / "Sentinel"
        startup_unc = c_share / "ProgramData" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

        root_unc.mkdir(parents=True, exist_ok=True)
        startup_unc.mkdir(parents=True, exist_ok=True)

        # 1. Copy agent
        shutil.copyfile(LOCAL_AGENT, root_unc / "browseguard_agent.py")

        # 2. Copy launcher ps1
        launcher_src = Path(r"\\M01.mamal.nbi\C$\ProgramData\Sentinel\launch_agent.ps1")
        if launcher_src.exists():
            shutil.copyfile(launcher_src, root_unc / "launch_agent.ps1")

        # 3. Write stealth VBS
        (startup_unc / "SentinelAgent.vbs").write_text(VBS_CONTENT, encoding="ascii")

        # 4. Remove legacy cmd/bat
        for legacy in ["SentinelAgent.cmd", "SentinelAgent.bat"]:
            p = startup_unc / legacy
            if p.exists():
                p.unlink()

        # 5. Kill old python if any
        subprocess.run(
            ["taskkill", "/S", fqdn, "/U", CRED_U, "/P", CRED_P, "/F", "/IM", "python.exe"],
            capture_output=True, timeout=5
        )
        subprocess.run(
            ["taskkill", "/S", fqdn, "/U", CRED_U, "/P", CRED_P, "/F", "/IM", "pythonw.exe"],
            capture_output=True, timeout=5
        )

        return station, True, "Successfully Deployed & Updated"
    except Exception as e:
        return station, False, f"Failed: {e}"

def main():
    print(f"=== DEPLOYING SENTINEL v2.8.1 TO {len(TARGETS)} NEWLY ONLINE MACHINES ===")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(deploy_to_station, s): s for s in TARGETS}
        for f in concurrent.futures.as_completed(futures):
            station, ok, msg = f.result()
            symbol = "[+]" if ok else "[-]"
            print(f"  {symbol} {station:7s} : {msg}")

if __name__ == "__main__":
    main()
