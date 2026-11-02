#!/usr/bin/env python3
"""
auto_enable_expired_suspensions.py
===================================
Institutional Retention Policy Enforcement Script:
Automatically checks all disabled/blocked student accounts and:
  1. RE-ENABLES students in Darajah 1–7 whose 14-day suspension period has elapsed.
  2. STRICTLY MAINTAINS Darajah 8, 9, 10, and 11 as PERMANENTLY DISABLED (these senior
     students have personal laptops and are not permitted to access computer lab PCs).
  3. Re-enables Active Directory domain accounts (mamal.nbi) and clears local block lists.

Usage:
  python tools/auto_enable_expired_suspensions.py [--dry-run] [--verbose] [--quiet]

Flags:
  --dry-run   Preview which accounts will be re-enabled without making changes.
  --verbose   Display granular per-student telemetry and timestamp breakdown.
  --quiet     Suppress all console output (log to file only). Ideal for Task Scheduler runs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Ensure UTF-8 output on Windows consoles
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
ACCOUNTS_FILE = DATA_DIR / "student_accounts.json"
BLOCKED_TRNOS_FILE = DATA_DIR / "blocked_trnos.txt"
LOG_FILE = LOGS_DIR / "retention_auto_enable.log"

POLICY_WINDOW_DAYS = 14

LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ]
)
logger = logging.getLogger("AutoEnableSuspensions")


def is_senior_darajah(darajah: Any) -> bool:
    """Return True if the cohort is Darajah 8, 9, 10, or 11 (senior cohorts with personal laptops)."""
    d_clean = str(darajah or "").upper().strip()
    return bool(re.search(r'\b(8|9|10|11)\b', d_clean))


def parse_timestamp(ts_val: Any) -> datetime | None:
    """Safely parse ISO date/timestamp string."""
    if not ts_val:
        return None
    try:
        clean = str(ts_val).replace("Z", "").split("+")[0].split(".")[0].strip()
        return datetime.fromisoformat(clean)
    except Exception:
        try:
            return datetime.strptime(str(ts_val)[:10], "%Y-%m-%d")
        except Exception:
            return None


def run_sweep(dry_run: bool = False, verbose: bool = False) -> Dict[str, Any]:
    now = datetime.now()
    cutoff = now - timedelta(days=POLICY_WINDOW_DAYS)

    logger.info("=" * 72)
    logger.info("MAMALBROWSE SENTINEL — 14-DAY SUSPENSION AUTO-EXPIRY ENGINE")
    logger.info("=" * 72)
    logger.info(f"Execution Time : {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Policy Window  : {POLICY_WINDOW_DAYS} Days (Cutoff: <= {cutoff.strftime('%Y-%m-%d %H:%M:%S')})")
    logger.info(f"Mode           : {'[DRY RUN — No changes written]' if dry_run else '[LIVE EXECUTION — Applying changes]'}")
    logger.info("-" * 72)

    if not ACCOUNTS_FILE.exists():
        logger.error(f"Accounts database not found: {ACCOUNTS_FILE}")
        return {"ok": False, "error": "ACCOUNTS_FILE_MISSING"}

    try:
        accounts: Dict[str, Any] = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8") or "{}")
    except Exception as e:
        logger.error(f"Failed loading accounts file: {e}")
        return {"ok": False, "error": str(e)}

    # Authoritative student roster lookup
    roster = {}
    try:
        from app.student_roster import load_student_roster
        roster = load_student_roster() or {}
    except Exception as r_err:
        logger.warning(f"Could not load authoritative student roster: {r_err}")

    # Account lock service for live unblock
    account_lock_service = None
    if not dry_run:
        try:
            from app.services.account_lock_service import account_lock_service as als
            account_lock_service = als
        except Exception as als_err:
            logger.warning(f"Could not load account_lock_service: {als_err}")

    total_scanned = 0
    total_blocked = 0
    eligible_to_enable: List[Dict[str, Any]] = []
    senior_maintained_disabled: List[Dict[str, Any]] = []
    active_suspensions: List[Dict[str, Any]] = []

    for trno, acc in accounts.items():
        total_scanned += 1
        status = str(acc.get("status") or "").lower()
        if status != "blocked":
            continue

        total_blocked += 1
        sid = str(trno).strip()

        # Determine authoritative darajah
        dar = str(acc.get("darajah") or "").strip()
        if not dar or dar.upper() in ("UNKNOWN", "GENERAL"):
            dar = str((roster.get(sid) or {}).get("darajah") or "").strip()

        name = str(acc.get("name") or (roster.get(sid) or {}).get("name") or f"Student #{sid}").strip()

        # INSTITUTIONAL RULE: Darajah 8–11 MUST REMAIN DISABLED
        if is_senior_darajah(dar):
            senior_maintained_disabled.append({
                "trno": sid,
                "name": name,
                "darajah": dar or "Darajah 8-11",
                "reason": "Senior student with personal laptop — lab PC access permanently barred",
            })
            continue

        # Non-senior (Darajah 1–7)
        b_at = parse_timestamp(acc.get("blocked_at") or acc.get("last_incident"))
        if not b_at:
            # If no timestamp, default to safe active suspension
            active_suspensions.append({
                "trno": sid,
                "name": name,
                "darajah": dar or "Darajah 1-7",
                "days_elapsed": 0,
                "days_remaining": POLICY_WINDOW_DAYS,
                "reason": "Missing suspension timestamp",
            })
            continue

        days_elapsed = (now - b_at).days
        if b_at <= cutoff:
            eligible_to_enable.append({
                "trno": sid,
                "name": name,
                "darajah": dar or "Junior",
                "blocked_at": b_at.strftime("%Y-%m-%d %H:%M"),
                "days_elapsed": days_elapsed,
                "account_data": acc,
            })
        else:
            days_remaining = max(0, POLICY_WINDOW_DAYS - days_elapsed)
            active_suspensions.append({
                "trno": sid,
                "name": name,
                "darajah": dar or "Junior",
                "blocked_at": b_at.strftime("%Y-%m-%d %H:%M"),
                "days_elapsed": days_elapsed,
                "days_remaining": days_remaining,
            })

    # Display Breakdown
    logger.info(f"Total Student Accounts Scanned : {total_scanned}")
    logger.info(f"Total Currently Blocked        : {total_blocked}")
    logger.info(f"  * Eligible to Auto-Enable   : {len(eligible_to_enable)} (>= 14 days, Darajah 1-7)")
    logger.info(f"  * Active Suspensions        : {len(active_suspensions)} (< 14 days remaining)")
    logger.info(f"  * Senior Laptop Directive   : {len(senior_maintained_disabled)} (Darajah 8-11 strictly kept disabled)")
    logger.info("-" * 72)

    # 1. Seniors Maintained Disabled
    if verbose or dry_run:
        logger.info(f"[LOCKED] SENIOR STUDENTS MAINTAINED DISABLED ({len(senior_maintained_disabled)}):")
        for s in senior_maintained_disabled[:15]:
            logger.info(f"   * TRNO #{s['trno']:<6} | {s['name']:<32} | {s['darajah']:<10} | {s['reason']}")
        if len(senior_maintained_disabled) > 15:
            logger.info(f"   ... and {len(senior_maintained_disabled) - 15} more senior students maintained locked.")
        logger.info("-" * 72)

    # 2. Active Suspensions (< 14 days)
    if verbose or dry_run:
        logger.info(f"[SUSPENDED] ACTIVE SUSPENSIONS RUNNING ({len(active_suspensions)}):")
        for s in active_suspensions[:10]:
            logger.info(f"   * TRNO #{s['trno']:<6} | {s['name']:<30} | {s['darajah']:<10} | {s['days_elapsed']}d elapsed, {s['days_remaining']}d left")
        if len(active_suspensions) > 10:
            logger.info(f"   ... and {len(active_suspensions) - 10} more students in suspension window.")
        logger.info("-" * 72)

    # 3. Process Eligible Students
    enabled_success: List[str] = []
    enabled_failed: List[str] = []

    if eligible_to_enable:
        logger.info(f"[ACTION] RE-ENABLING {len(eligible_to_enable)} STUDENTS (COMPLETED 14-DAY SUSPENSION):")
        for s in eligible_to_enable:
            sid = s["trno"]
            if dry_run:
                logger.info(f"   [DRY-RUN WOULD ENABLE] TRNO #{sid:<6} | {s['name']:<30} | {s['darajah']:<8} | Blocked {s['days_elapsed']} days ago ({s['blocked_at']})")
                enabled_success.append(sid)
            else:
                unblocked = False
                note = f"14-day policy window completed ({s['days_elapsed']} days elapsed)"
                if account_lock_service:
                    try:
                        unblocked = account_lock_service.unblock_account(
                            sid,
                            admin_user="System (14-Day Retention Auto-Expiry)",
                            note=note
                        )
                    except Exception as u_err:
                        logger.error(f"   [ERROR] Account lock service failed for TRNO #{sid}: {u_err}")

                if not unblocked:
                    # Direct state fallback
                    acc = s["account_data"]
                    acc["status"] = "active"
                    acc["warning_count"] = 0
                    acc["unblocked_at"] = now.isoformat(timespec="seconds")
                    acc["unblocked_by"] = f"System (14-Day Retention Auto-Expiry) ({note})"
                    acc["blocked_reason"] = ""
                    acc.pop("review_pending", None)
                    acc.pop("review_flagged_at", None)
                    acc.pop("review_note", None)
                    accounts[sid] = acc
                    try:
                        from app.services.active_directory_service import ad_service
                        ad_service.enable_account(sid, note=note, actor="Retention Auto-Expiry")
                    except Exception:
                        pass
                    unblocked = True

                if unblocked:
                    enabled_success.append(sid)
                    logger.info(f"   [RE-ENABLED] TRNO #{sid:<6} | {s['name']:<30} | {s['darajah']:<8} | Restored to active after {s['days_elapsed']} days")
                else:
                    enabled_failed.append(sid)
                    logger.warning(f"   [FAILED] TRNO #{sid:<6} | Could not be re-enabled")
    else:
        logger.info("[INFO] No non-senior students currently exceed the 14-day suspension cutoff.")

    # Save accounts & sync blocked_trnos.txt if changes made
    if not dry_run and enabled_success:
        try:
            tmp = ACCOUNTS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(ACCOUNTS_FILE)
            logger.info(f"[SAVED] Updated accounts database ({ACCOUNTS_FILE.name}).")
        except Exception as s_err:
            logger.error(f"Failed writing accounts file: {s_err}")

        try:
            still_blocked = [
                t for t, a in accounts.items()
                if a.get("status") == "blocked" and t not in enabled_success
            ]
            BLOCKED_TRNOS_FILE.write_text(
                "\n".join(sorted(still_blocked)) + ("\n" if still_blocked else ""),
                encoding="utf-8"
            )
            logger.info(f"[SYNC] Synchronized raw blocklist ({BLOCKED_TRNOS_FILE.name}) with {len(still_blocked)} remaining blocked TRNOs.")
        except Exception as b_err:
            logger.warning(f"Failed synchronizing blocked_trnos.txt: {b_err}")

    logger.info("=" * 72)
    logger.info("SUMMARY RESULTS:")
    logger.info(f"  * Successfully Auto-Enabled : {len(enabled_success)} students")
    logger.info(f"  * Active Suspensions Kept  : {len(active_suspensions)} students")
    logger.info(f"  * Senior Directive Kept    : {len(senior_maintained_disabled)} students (Darajah 8-11)")
    if enabled_failed:
        logger.warning(f"  * Failed To Enable         : {len(enabled_failed)} students")
    logger.info("=" * 72)

    return {
        "ok": True,
        "dry_run": dry_run,
        "total_scanned": total_scanned,
        "total_blocked": total_blocked,
        "enabled_count": len(enabled_success),
        "enabled_trnos": enabled_success,
        "active_suspension_count": len(active_suspensions),
        "senior_locked_count": len(senior_maintained_disabled),
        "failed_count": len(enabled_failed),
    }


def main():
    parser = argparse.ArgumentParser(description="Auto-enable students who completed 14-day suspension (preserving Darajah 8-11 locks)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without modifying accounts or Active Directory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full student lists and details")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress console output (log to file only). Use for Task Scheduler runs.")
    args = parser.parse_args()

    # In quiet mode, redirect stdout to devnull so Task Scheduler gets no console noise.
    # Logging to the file (retention_auto_enable.log) is always active regardless.
    if args.quiet:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    # Sync any new violations from recent browsing history (last 3 days)
    if not args.dry_run:
        try:
            from tools.sync_real_violations import sync_violations_from_cache
            logger.info("[AutoEnable] Running real violation sync from recent browsing logs (3-day window)...")
            sync_violations_from_cache(days_back=3)
        except Exception as _sync_err:
            logger.warning(f"[AutoEnable] Real violation sync warning: {_sync_err}")

    result = run_sweep(dry_run=args.dry_run, verbose=args.verbose)

    # Restore stdout before exit (good hygiene)
    if args.quiet:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.stdout = sys.__stdout__

    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
