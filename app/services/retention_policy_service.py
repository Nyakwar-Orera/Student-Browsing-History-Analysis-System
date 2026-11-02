# app/services/retention_policy_service.py
"""
Retention Policy Service — Enforces the institutional 14-day violation retention policy.

Policy:
  - Students in Darajah 1–7 blocked for policy violations are AUTOMATICALLY RE-ENABLED after 14 days.
  - Students in Darajah 8, 9, 10, and 11 have personal laptops and are NEVER re-enabled for lab PCs;
    they remain permanently locked per institutional directive.
  - Violation audit logs older than 14 days are archived (not deleted, for compliance).
  - Weekly log queries automatically use a 14-day window.

This module mutates blocked_at timestamps and unblocks accounts for auto-expiry.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("retention_policy_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
VIOLATIONS_DIR = BASE_DIR / "students" / "violations"
ARCHIVE_DIR = VIOLATIONS_DIR / "archive"

ACCOUNTS_FILE = DATA_DIR / "student_accounts.json"
BLOCKED_TRNOS_FILE = DATA_DIR / "blocked_trnos.txt"

# ═══ THE POLICY CONSTANT ═══════════════════════════════════════
VIOLATION_RETENTION_DAYS = 14  # Students stay blocked for 14 days
AUDIT_RETENTION_DAYS = 14      # Audit logs move to archive after 14 days
# ═══════════════════════════════════════════════════════════════


def is_senior_darajah(darajah: Any) -> bool:
    """Check if student belongs to Darajah 8, 9, 10, or 11."""
    d_clean = str(darajah or "").upper().strip()
    return bool(re.search(r'\b(8|9|10|11)\b', d_clean))


class RetentionPolicyService:
    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._lock = threading.RLock()
        self._initialized = True
        logger.info(
            "[RetentionPolicy] Initialized. Violation block duration=%d days, Audit retention=%d days",
            VIOLATION_RETENTION_DAYS, AUDIT_RETENTION_DAYS,
        )

    def auto_expire_blocked_accounts(self) -> Dict[str, Any]:
        """
        Check all blocked student accounts against the 14-day retention policy:
        - Students in Darajah 1–7 whose 14-day suspension window has elapsed
          are AUTOMATICALLY RE-ENABLED.
        - Students in Darajah 8, 9, 10, and 11 have personal laptops and are NEVER
          re-enabled for lab PCs — they remain permanently disabled.
        - Returns a full telemetry summary.
        """
        if not ACCOUNTS_FILE.exists():
            return {
                "expired_count": 0,
                "expired_trnos": [],
                "senior_locked_count": 0,
                "senior_locked_trnos": [],
                "active_suspension_count": 0,
                "active_suspensions": [],
            }

        with self._lock:
            try:
                accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8") or "{}")
            except Exception as e:
                logger.error(f"[RetentionPolicy] Could not read accounts: {e}")
                return {
                    "expired_count": 0,
                    "expired_trnos": [],
                    "senior_locked_count": 0,
                    "senior_locked_trnos": [],
                    "active_suspension_count": 0,
                    "active_suspensions": [],
                }

            now = datetime.now()
            cutoff = now - timedelta(days=VIOLATION_RETENTION_DAYS)
            unblocked_trnos: List[str] = []
            senior_locked_trnos: List[str] = []
            active_suspensions: List[Dict[str, Any]] = []

            # Account lock service for synchronized persistence & AD unblock
            try:
                from app.services.account_lock_service import account_lock_service
            except Exception as e:
                logger.warning(f"[RetentionPolicy] Could not import account_lock_service: {e}")
                account_lock_service = None

            for trno, acc in list(accounts.items()):
                if acc.get("status") not in ("blocked",):
                    continue

                sid = str(trno).strip()

                # Determine accurate Darajah
                dar = str(acc.get("darajah") or "").strip()
                if not dar or dar.upper() in ("UNKNOWN", "GENERAL"):
                    try:
                        from app.student_roster import load_student_roster
                        roster = load_student_roster()
                        dar = str((roster.get(sid) or {}).get("darajah") or "")
                    except Exception:
                        dar = ""

                # INSTITUTIONAL RULE: Darajah 8–11 have personal laptops and MUST NOT access lab PCs.
                # Maintain permanently disabled.
                if is_senior_darajah(dar):
                    senior_locked_trnos.append(sid)
                    continue

                blocked_at_str = acc.get("blocked_at") or acc.get("last_incident")
                if not blocked_at_str:
                    continue
                try:
                    blocked_at = datetime.fromisoformat(
                        str(blocked_at_str).replace("Z", "").split("+")[0].split(".")[0]
                    )
                except Exception:
                    continue

                days_blocked = (now - blocked_at).days
                if blocked_at <= cutoff:
                    # 14 days have completed -> AUTOMATICALLY UNBLOCK / RE-ENABLE
                    unblocked = False
                    if account_lock_service:
                        try:
                            unblocked = account_lock_service.unblock_account(
                                sid,
                                admin_user="System (14-Day Retention Auto-Expiry)",
                                note=f"14-day policy window completed ({days_blocked} days suspended)"
                            )
                        except Exception as unblock_err:
                            logger.error(f"[RetentionPolicy] Failed unblocking TRNO {sid}: {unblock_err}")

                    if not unblocked:
                        # Fallback direct unblock if service failed or returned False
                        now_str = now.isoformat(timespec="seconds")
                        acc["status"] = "active"
                        acc["warning_count"] = 0
                        acc["unblocked_at"] = now_str
                        acc["unblocked_by"] = "System (14-Day Retention Auto-Expiry)"
                        acc["blocked_reason"] = ""
                        acc.pop("review_pending", None)
                        acc.pop("review_flagged_at", None)
                        acc.pop("review_note", None)
                        accounts[sid] = acc
                        try:
                            from app.services.active_directory_service import ad_service
                            ad_service.enable_account(sid, note="14-day policy window completed", actor="Retention Auto-Expiry")
                        except Exception:
                            pass

                    unblocked_trnos.append(sid)
                    logger.info(
                        f"[RetentionPolicy] Student TRNO {sid} (Darajah {dar or '1-7'}) has completed "
                        f"14-day suspension ({days_blocked} days elapsed) — AUTOMATICALLY RE-ENABLED."
                    )
                else:
                    # Still within 14-day suspension window
                    days_remaining = max(0, VIOLATION_RETENTION_DAYS - days_blocked)
                    active_suspensions.append({
                        "trno": sid,
                        "name": acc.get("name", ""),
                        "darajah": dar,
                        "blocked_at": blocked_at_str,
                        "days_elapsed": days_blocked,
                        "days_remaining": days_remaining,
                    })

            # Save accounts if fallback direct modification occurred
            if unblocked_trnos and not account_lock_service:
                try:
                    tmp = ACCOUNTS_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(ACCOUNTS_FILE)
                except Exception as e:
                    logger.error(f"[RetentionPolicy] Could not save accounts: {e}")

            # Also ensure blocked_trnos.txt is synchronized
            try:
                blocked_trnos = [
                    t for t, a in accounts.items()
                    if a.get("status") == "blocked" and t not in unblocked_trnos
                ]
                BLOCKED_TRNOS_FILE.parent.mkdir(parents=True, exist_ok=True)
                BLOCKED_TRNOS_FILE.write_text(
                    "\n".join(sorted(blocked_trnos)) + ("\n" if blocked_trnos else ""),
                    encoding="utf-8"
                )
            except Exception as b_err:
                logger.warning(f"[RetentionPolicy] Could not update blocked_trnos.txt: {b_err}")

            return {
                "expired_count": len(unblocked_trnos),
                "expired_trnos": unblocked_trnos,
                "senior_locked_count": len(senior_locked_trnos),
                "senior_locked_trnos": senior_locked_trnos,
                "active_suspension_count": len(active_suspensions),
                "active_suspensions": active_suspensions,
            }

    def archive_old_audit_logs(self) -> Dict[str, Any]:
        """
        Move audit logs AND violation logs older than 14 days to archive folder.
        They are preserved for compliance but no longer shown in the weekly log.
        Handles both:
          - audit_YYYY-MM-DD.jsonl  (MamalBrowse Sentinel audit events)
          - violations_YYYY-MM-DD.jsonl  (raw policy violation records)
        """
        if not VIOLATIONS_DIR.exists():
            return {"archived_count": 0, "archived_files": []}

        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        cutoff_date = (datetime.now() - timedelta(days=AUDIT_RETENTION_DAYS)).date()
        archived: List[str] = []

        with self._lock:
            # Archive both audit_* and violations_* files older than retention window
            for pattern, prefix in [("audit_*.jsonl", "audit_"), ("violations_*.jsonl", "violations_")]:
                for log_file in VIOLATIONS_DIR.glob(pattern):
                    try:
                        date_str = log_file.stem.replace(prefix, "")
                        file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except Exception:
                        continue

                    if file_date < cutoff_date:
                        try:
                            dest = ARCHIVE_DIR / log_file.name
                            shutil.move(str(log_file), str(dest))
                            archived.append(log_file.name)
                            logger.info(f"[RetentionPolicy] Archived: {log_file.name}")
                        except Exception as e:
                            logger.warning(f"[RetentionPolicy] Could not archive {log_file.name}: {e}")

        return {"archived_count": len(archived), "archived_files": archived}

    def get_retention_window(self) -> Dict[str, Any]:
        """Return the current 14-day window bounds for the weekly log."""
        now = datetime.now()
        start = now - timedelta(days=VIOLATION_RETENTION_DAYS)
        return {
            "retention_days": VIOLATION_RETENTION_DAYS,
            "window_start": start.strftime("%Y-%m-%d"),
            "window_end": now.strftime("%Y-%m-%d"),
            "policy_text": (
                f"Violations are retained for {VIOLATION_RETENTION_DAYS} days. "
                f"Students in Darajah 1–7 blocked for violations are automatically re-enabled "
                f"after {VIOLATION_RETENTION_DAYS} days. Students in Darajah 8–11 have personal "
                f"laptops and remain permanently locked from lab PCs."
            ),
        }

    def run_all(self) -> Dict[str, Any]:
        """Run both auto-unblock expired accounts and archive operations. Called from scheduler."""
        expiry = self.auto_expire_blocked_accounts()
        archive = self.archive_old_audit_logs()
        expired_count = expiry.get("expired_count", 0)
        senior_locked = expiry.get("senior_locked_count", 0)
        active_suspensions = expiry.get("active_suspension_count", 0)

        if expired_count > 0:
            logger.info(
                f"[RetentionPolicy] Successfully auto-enabled {expired_count} student(s) past 14 days: "
                f"{', '.join(expiry.get('expired_trnos', [])[:10])}"
            )
        logger.info(
            f"[RetentionPolicy] Sweep status: {expired_count} auto-enabled, "
            f"{active_suspensions} active suspensions running, "
            f"{senior_locked} Darajah 8-11 seniors maintained permanently disabled."
        )

        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "expired_count": expired_count,
            "expired_trnos": expiry.get("expired_trnos", []),
            "senior_locked_count": senior_locked,
            "senior_locked_trnos": expiry.get("senior_locked_trnos", []),
            "active_suspension_count": active_suspensions,
            "active_suspensions": expiry.get("active_suspensions", []),
            "archived_count": archive.get("archived_count", 0),
            "retention_window": self.get_retention_window(),
        }


retention_policy_service = RetentionPolicyService()
