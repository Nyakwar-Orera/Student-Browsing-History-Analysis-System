"""
tools/push_and_relaunch_v281.py
Fast parallel push of browseguard_agent.py (v2.8.1 with 3-second auto-dismiss alerts & zero-blink in-memory execution)
across all 94 deployed workstations.
"""
import concurrent.futures
import csv
import glob
import shutil
import subprocess
import time
from pathlib import Path

CRED_U = "MAMAL\\admin"
CRED_P = "100%SECUREDadmin"
LOCAL_AGENT = Path(r"C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\client_agent\browseguard_agent.py")
VBS_TARGET = r'wscript.exe "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\SentinelAgent.vbs"'

def get_deployed_stations():
    csv_files = sorted(glob.glob("client_agent/Sentinel_Deploy_Results_*.csv"))
    if not csv_files:
        return []
    latest_csv = csv_files[-1]
    stations = []
    with open(latest_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Status") == "DEPLOYED":
                station = row["Computer"].split(".")[0].strip()
                if station:
                    stations.append(station)
    return stations

def update_and_relaunch(station: str) -> tuple[str, bool, str]:
    dest_dir = Path(f"\\\\{station}\\C$\\ProgramData\\Sentinel")
    dest_file = dest_dir / "browseguard_agent.py"
    if not dest_dir.exists():
        return (station, False, "Directory unreachable")

    try:
        # 1. Fast copy
        shutil.copyfile(LOCAL_AGENT, dest_file)
    except Exception as e:
        return (station, False, f"Copy failed: {e}")

    # 2. Kill running pythonw.exe
    subprocess.run(
        ["taskkill", "/S", station, "/U", CRED_U, "/P", CRED_P, "/F", "/IM", "pythonw.exe"],
        capture_output=True, timeout=5
    )

    # 3. Create one-time task & trigger stealth relaunch
    cmd_create = [
        "schtasks", "/Create", "/S", station, "/U", CRED_U, "/P", CRED_P,
        "/TN", "RunSentinelStealth",
        "/TR", VBS_TARGET,
        "/SC", "ONCE", "/ST", "23:59", "/F"
    ]
    res = subprocess.run(cmd_create, capture_output=True, text=True, timeout=8)
    if res.returncode != 0:
        return (station, False, f"schtasks create failed: {res.stderr.strip()[:60]}")

    subprocess.run(
        ["schtasks", "/Run", "/S", station, "/U", CRED_U, "/P", CRED_P, "/TN", "RunSentinelStealth"],
        capture_output=True, timeout=8
    )
    subprocess.run(
        ["schtasks", "/Delete", "/S", station, "/U", CRED_U, "/P", CRED_P, "/TN", "RunSentinelStealth", "/F"],
        capture_output=True, timeout=8
    )

    return (station, True, "Updated & Relaunched")

def main():
    stations = get_deployed_stations()
    print(f"=== PUSHING SENTINEL v2.8.1 (3-SEC AUTO-DISMISS ALERTS) TO {len(stations)} STATIONS ===")
    start_t = time.time()
    success = 0
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(update_and_relaunch, s): s for s in stations}
        for future in concurrent.futures.as_completed(futures):
            station, ok, msg = future.result()
            if ok:
                print(f"  [+] {station:7s} : {msg}")
                success += 1
            else:
                print(f"  [-] {station:7s} : {msg}")
                failed += 1

    elapsed = time.time() - start_t
    print(f"\n=== COMPLETED IN {elapsed:.1f}s: {success} updated successfully, {failed} failed ===")

if __name__ == "__main__":
    main()

