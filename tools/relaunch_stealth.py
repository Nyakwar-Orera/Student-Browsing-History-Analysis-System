"""
tools/relaunch_stealth.py
Relaunches Sentinel Agent v2.8 (Stealth & Zero-Blink Edition) across all active workstations.
Dynamically discovers deployed stations from the latest deployment CSV.
1. Kills legacy python.exe / pythonw.exe remotely.
2. Spawns SentinelAgent.vbs silently via schtasks.
3. Cleans up temporary scheduled task.
"""
import csv
import glob
import os
import subprocess
import time
from pathlib import Path

CRED_U = "MAMAL\\admin"
CRED_P = "100%SECUREDadmin"

VBS_TARGET = r'wscript.exe "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\SentinelAgent.vbs"'

def get_deployed_stations():
    csv_files = sorted(glob.glob("client_agent/Sentinel_Deploy_Results_*.csv"))
    if not csv_files:
        print("[-] No deployment CSV found in client_agent/")
        return []
    latest_csv = csv_files[-1]
    print(f"[*] Reading deployed stations from: {latest_csv}")
    stations = []
    with open(latest_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Status") == "DEPLOYED":
                station = row["Computer"].split(".")[0].strip()
                if station:
                    stations.append(station)
    return stations

def relaunch_station(station: str):
    share = Path(f"\\\\{station}\\C$\\ProgramData\\Sentinel")
    if not share.exists():
        print(f"[-] {station}: offline or inaccessible")
        return False

    # 1. Kill old python/pythonw processes
    subprocess.run(
        ["taskkill", "/S", station, "/U", CRED_U, "/P", CRED_P, "/F", "/IM", "python.exe"],
        capture_output=True, timeout=5
    )
    subprocess.run(
        ["taskkill", "/S", station, "/U", CRED_U, "/P", CRED_P, "/F", "/IM", "pythonw.exe"],
        capture_output=True, timeout=5
    )

    # 2. Create one-time task
    cmd_create = [
        "schtasks", "/Create", "/S", station, "/U", CRED_U, "/P", CRED_P,
        "/TN", "RunSentinelStealth",
        "/TR", VBS_TARGET,
        "/SC", "ONCE", "/ST", "23:59", "/F"
    ]
    res = subprocess.run(cmd_create, capture_output=True, text=True, timeout=8)
    if res.returncode != 0:
        print(f"[-] {station}: failed to create task ({res.stderr.strip()})")
        return False

    # 3. Run task
    cmd_run = [
        "schtasks", "/Run", "/S", station, "/U", CRED_U, "/P", CRED_P,
        "/TN", "RunSentinelStealth"
    ]
    subprocess.run(cmd_run, capture_output=True, text=True, timeout=8)

    # 4. Delete task
    cmd_del = [
        "schtasks", "/Delete", "/S", station, "/U", CRED_U, "/P", CRED_P,
        "/TN", "RunSentinelStealth", "/F"
    ]
    subprocess.run(cmd_del, capture_output=True, text=True, timeout=8)

    print(f"[+] {station}: successfully restarted with v2.8 Stealth Agent")
    return True

def main():
    stations = get_deployed_stations()
    print(f"=== RELAUNCHING SENTINEL v2.8 (EDGE ENABLED) ON {len(stations)} WORKSTATIONS ===")
    success_count = 0
    for s in stations:
        try:
            if relaunch_station(s):
                success_count += 1
        except Exception as e:
            print(f"[-] {s}: error {e}")
    print(f"\n=== DONE: {success_count}/{len(stations)} workstations launched ===")

if __name__ == "__main__":
    main()
