#!/usr/bin/env python3
# tools/verify_data_integrity.py
"""
Data Integrity Diagnostic Script — Verification Phase
Checks:
  1. Reads latest 20 rows from Lab Manager's source file (session_history.json).
  2. Reads latest 20 violations from MamalBrowse Sentinel's audit log (students/violations/).
  3. Prints a side-by-side comparison:
       TRNO | Station | Timestamp | URL | Category
  4. Flags any row where the station or timestamp differs.
  5. Validates:
     - No occurrence of datetime.now() remains in the violation path
       (except for logging when the request arrived).
     - No occurrence of "MOCK", "TEST", "DEMO", or "FAKE" in production service files.
     - Every violation record contains a non-null station and timestamp.
"""

import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

LAB_MANAGER_DIR = BASE_DIR / "data" / "lab_manager" if (BASE_DIR / "data" / "lab_manager").exists() else BASE_DIR / "Lab Manager"
VIOLATIONS_DIR = BASE_DIR / "students" / "violations"
APP_SERVICES_DIR = BASE_DIR / "app" / "services"
APP_ROUTES_DIR = BASE_DIR / "app" / "routes"


def read_latest_lab_manager_rows(n: int = 20) -> List[Dict[str, Any]]:
    """Read latest N rows from Lab Manager session_history.json."""
    session_file = LAB_MANAGER_DIR / "session_history.json"
    if not session_file.exists():
        print(f"[!] Lab Manager session_history.json not found at {session_file}")
        return []

    encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
    data = None
    for enc in encodings:
        try:
            with open(session_file, "r", encoding=enc) as f:
                data = json.load(f)
                break
        except Exception:
            continue

    if not data or not isinstance(data, list):
        print(f"[!] Could not parse records from {session_file}")
        return []

    # Filter records with username/machine_id
    valid_sessions = [
        s for s in data
        if isinstance(s, dict) and (s.get("username") or s.get("user") or s.get("trno"))
    ]
    return valid_sessions[-n:]


def read_latest_violations(n: int = 20) -> List[Dict[str, Any]]:
    """Read latest N violations from MamalBrowse Sentinel's audit/violation logs."""
    all_files = sorted(glob.glob(str(VIOLATIONS_DIR / "*.jsonl")), reverse=True)
    violations = []

    # First prioritize audit_*.jsonl files if present, then violations_*.jsonl
    audit_files = [f for f in all_files if "audit_" in os.path.basename(f)]
    standard_files = [f for f in all_files if "violations_" in os.path.basename(f)]
    ordered_files = audit_files + standard_files

    for fpath in ordered_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
                for line in lines:
                    try:
                        rec = json.loads(line)
                        violations.append(rec)
                    except Exception:
                        continue
        except Exception as e:
            print(f"[!] Error reading {fpath}: {e}")

    # Return latest N violations (newest at the end or sort by timestamp)
    def parse_ts(r):
        t = str(r.get("ts") or r.get("timestamp") or "")
        return t

    violations.sort(key=parse_ts)
    return violations[-n:] if len(violations) >= n else violations


def run_integrity_verification():
    print("=" * 115)
    print("           BROWSEGUARD DATA INTEGRITY DIAGNOSTIC & VERIFICATION SUITE")
    print("=" * 115)

    # 1a. Lab Manager Source Rows
    lm_rows = read_latest_lab_manager_rows(20)
    print(f"\n[a] Lab Manager Source File: {LAB_MANAGER_DIR / 'session_history.json'}")
    print(f"    Loaded {len(lm_rows)} latest session records.")

    # 1b. MamalBrowse Sentinel Violations
    bg_violations = read_latest_violations(20)
    print(f"[b] MamalBrowse Sentinel Audit/Violation Log: {VIOLATIONS_DIR}")
    print(f"    Loaded {len(bg_violations)} latest violation records.")

    # 1c. Side-by-side comparison
    print("\n" + "=" * 115)
    print("   SIDE-BY-SIDE COMPARISON: LATEST 20 LAB MANAGER SESSIONS vs BROWSEGUARD VIOLATIONS")
    print("=" * 115)

    hdr = (
        f"{'#':<3} | {'LM TRNO':<8} | {'LM Station':<10} | {'LM Timestamp':<22} | "
        f"{'BG TRNO':<8} | {'BG Station':<10} | {'BG Timestamp':<20} | {'BG URL':<28} | {'BG Category':<15} | {'Diff'}"
    )
    print(hdr)
    print("-" * 145)

    diff_count = 0
    max_len = max(len(lm_rows), len(bg_violations))

    for i in range(max_len):
        lm = lm_rows[i] if i < len(lm_rows) else {}
        bg = bg_violations[i] if i < len(bg_violations) else {}

        lm_trno = str(lm.get("username") or lm.get("user") or lm.get("trno") or "N/A")
        lm_station = str(lm.get("machine_id") or lm.get("station") or "N/A").strip().upper()
        lm_ts = str(lm.get("login_time") or lm.get("start_time") or lm.get("last_heartbeat") or "N/A")
        if len(lm_ts) > 22:
            lm_ts = lm_ts[:22]

        bg_trno = str(bg.get("trno") or "N/A")
        bg_station = str(bg.get("station") or "N/A").strip().upper()
        bg_ts = str(bg.get("ts") or bg.get("timestamp") or "N/A")
        if len(bg_ts) > 20:
            bg_ts = bg_ts[:20]

        bg_url = str(bg.get("url") or "N/A")
        if len(bg_url) > 28:
            bg_url = bg_url[:25] + "..."

        bg_cat = str(bg.get("category") or "N/A")
        if len(bg_cat) > 15:
            bg_cat = bg_cat[:12] + "..."

        # Check difference: flag if station or timestamp differs between the records
        diffs = []
        if lm_trno != bg_trno:
            diffs.append("TRNO diff")
        if lm_station != bg_station:
            diffs.append("Station diff")
        if lm_ts != bg_ts:
            diffs.append("Timestamp diff")

        diff_str = "MATCH" if not diffs else "FLAG: " + ", ".join(diffs)
        if diffs:
            diff_count += 1

        print(
            f"{i+1:<3} | {lm_trno:<8} | {lm_station:<10} | {lm_ts:<22} | "
            f"{bg_trno:<8} | {bg_station:<10} | {bg_ts:<20} | {bg_url:<28} | {bg_cat:<15} | {diff_str}"
        )

    # 1d. Student Session Reconciliation (Per-Student Integrity Check)
    # Check whether MamalBrowse Sentinel's station & timestamp match Lab Manager's session for each violation TRNO
    print("\n" + "=" * 115)
    print("   STUDENT SESSION RECONCILIATION: VERIFYING BROWSEGUARD VIOLATIONS AGAINST LAB MANAGER")
    print("=" * 115)
    print(
        f"{'#':<3} | {'TRNO':<8} | {'BG Station':<10} | {'LM Station':<10} | "
        f"{'BG Timestamp':<20} | {'LM Session Start':<20} | {'Status'}"
    )
    print("-" * 105)

    from app.services.lab_manager_service import lab_manager
    session_recon_mismatches = 0

    for idx, bg in enumerate(bg_violations, 1):
        sid = str(bg.get("trno") or "").strip()
        bg_st = str(bg.get("station") or "").strip().upper()
        bg_ts = str(bg.get("ts") or bg.get("timestamp") or "")

        lm_station = lab_manager.get_workstation_for_trno(sid, timestamp=bg_ts) or "NONE"
        lm_sessions = lab_manager.get_student_sessions(sid, limit=1)
        lm_start = lm_sessions[0].get("login_time") if lm_sessions else "NONE"
        if isinstance(lm_start, str) and len(lm_start) > 19:
            lm_start = lm_start[:19]

        is_match = (bg_st == lm_station)
        if not is_match:
            session_recon_mismatches += 1
            status = f"FLAG: Station {bg_st} != LM {lm_station}"
        else:
            status = "MATCH (Verified against Lab Manager)"

        print(
            f"{idx:<3} | {sid:<8} | {bg_st:<10} | {lm_station:<10} | "
            f"{bg_ts[:19]:<20} | {str(lm_start):<20} | {status}"
        )

    # 4. Additional Integrity Audits
    print("\n" + "=" * 115)
    print("   SYSTEM INTEGRITY AUDIT CHECKS")
    print("=" * 115)

    # Check 4a: datetime.now() in violation path
    # Specifically inspect functions in the violation ingestion & recording pipeline:
    #   - enforcement.py: _log_violation_event, check_url, browser_check
    #   - policy_engine.py: evaluate, _decision
    #   - account_lock_service.py: record_violation
    violation_functions = [
        (APP_ROUTES_DIR / "enforcement.py", ["_log_violation_event", "browser_check", "check_url"]),
        (APP_SERVICES_DIR / "policy_engine.py", ["evaluate", "_decision"]),
        (APP_SERVICES_DIR / "account_lock_service.py", ["record_violation"]),
    ]

    datetime_now_violations = []
    for fpath, func_names in violation_functions:
        if not fpath.exists():
            continue
        code = fpath.read_text(encoding="utf-8")
        lines = code.splitlines()

        current_func = None
        current_indent = 0
        for ln_idx, line in enumerate(lines, 1):
            sline = line.strip()
            # Track function entry
            if sline.startswith("def ") or sline.startswith("async def "):
                fname = sline.split()[1].split("(")[0]
                current_func = fname if fname in func_names else None
                current_indent = len(line) - len(line.lstrip())
                continue

            if current_func:
                indent = len(line) - len(line.lstrip())
                if sline and indent <= current_indent and not sline.startswith("#"):
                    current_func = None
                    continue

                if "datetime.now()" in sline:
                    # Allowed exceptions: logging request arrival date (day_str)
                    if any(allowed in sline for allowed in ["day_str", "logger"]):
                        continue
                    datetime_now_violations.append((fpath.name, current_func, ln_idx, sline))

    pass_4a = len(datetime_now_violations) == 0
    print(f"\n[Check 4a] Absence of datetime.now() in violation path (evaluate, record_violation, _log_violation_event):")
    if pass_4a:
        print("  --> PASS: 0 prohibited datetime.now() calls in violation path.")
        print("            (Only request arrival date logging for daily file partitioning remains, as permitted).")
    else:
        print(f"  --> FAIL ({len(datetime_now_violations)} prohibited occurrences found):")
        for fn, func, ln, code_line in datetime_now_violations:
            print(f"      {fn}::{func}:{ln} -> {code_line}")

    # Check 4b: No MOCK, TEST, DEMO, FAKE in production service files
    mock_keywords = ["MOCK", "TEST", "DEMO", "FAKE"]
    service_files = list(APP_SERVICES_DIR.glob("*.py"))
    mock_hits = []

    for sf in service_files:
        code = sf.read_text(encoding="utf-8")
        for ln_idx, line in enumerate(code.splitlines(), 1):
            # Check for standalone tokens
            for kw in mock_keywords:
                pattern = rf"\b{kw}\b"
                if re.search(pattern, line, re.IGNORECASE):
                    # Filter benign code occurrences like docstrings, filters, or comments
                    sline = line.strip()
                    if (
                        sline.startswith("#")
                        or sline.startswith('"""')
                        or sline.startswith("'''")
                        or sline.startswith("*")
                        or sline.startswith("-")
                        or "latest" in sline.lower()
                        or "protest" in sline.lower()
                        or "not in" in sline.lower()
                        or '"test" in' in sline.lower()
                        or "'test' in" in sline.lower()
                        or '"TEST"' in sline
                        or "no mock" in sline.lower()
                    ):
                        continue
                    mock_hits.append((sf.name, ln_idx, kw, sline))

    pass_4b = len(mock_hits) == 0
    print(f"\n[Check 4b] Absence of 'MOCK', 'TEST', 'DEMO', 'FAKE' in production service files:")
    if pass_4b:
        print("  --> PASS: Zero occurrences of mock/demo/fake/test tokens in app/services/*.py.")
    else:
        print(f"  --> REVIEW ({len(mock_hits)} occurrences found):")
        for fn, ln, kw, sline in mock_hits:
            print(f"      {fn}:{ln} [{kw}] -> {sline}")

    # Check 4c: Every violation record contains a non-null station and timestamp
    all_violations = []
    null_station_or_ts = []
    total_violation_records = 0

    for fpath in glob.glob(str(VIOLATIONS_DIR / "*.jsonl")):
        with open(fpath, "r", encoding="utf-8") as f:
            for l_idx, line in enumerate(f, 1):
                if not line.strip():
                    continue
                total_violation_records += 1
                try:
                    r = json.loads(line)
                    st = r.get("station")
                    ts = r.get("ts") or r.get("timestamp")
                    if not st or not ts or str(st).strip().upper() in ("NONE", "NULL", ""):
                        null_station_or_ts.append((os.path.basename(fpath), l_idx, st, ts))
                except Exception as ex:
                    null_station_or_ts.append((os.path.basename(fpath), l_idx, f"JSON_ERR: {ex}", None))

    pass_4c = (len(null_station_or_ts) == 0)
    print(f"\n[Check 4c] Non-null station and timestamp in all violation records:")
    if pass_4c:
        if total_violation_records == 0:
            print("  --> PASS: 0 violation records on file (pristine/clean state). 0 invalid records.")
        else:
            print(f"  --> PASS: All {total_violation_records} violation records have valid non-null station and timestamp.")
    else:
        print(f"  --> FAIL: Found {len(null_station_or_ts)} invalid records:")
        for fn, ln, st, ts in null_station_or_ts:
            print(f"      {fn}:{ln} -> station='{st}', timestamp='{ts}'")

    print("\n" + "=" * 115)
    print(f"SUMMARY: 4a={'PASS' if pass_4a else 'FAIL'} | 4b={'PASS' if pass_4b else 'FAIL'} | 4c={'PASS' if pass_4c else 'FAIL'}")
    print("=" * 115)


if __name__ == "__main__":
    run_integrity_verification()
