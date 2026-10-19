# app/services/retention_policy_service.py
"""
Retention Policy Service — Enforces the institutional 14-day violation retention policy.

Policy:
  - Students blocked for policy violations are auto-unblocked after 14 days.
  - Violation audit logs older than 14 days are archived (not deleted, for compliance).
  - Weekly log queries automatically use a 14-day window.

This is the ONLY module that should mutate blocked_at timestamps for auto-expiry.
"""
from __future__ import annotations

import json
import logging
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

# ═══ THE POLICY CONSTANT ═══════════════════════════════════════
VIOLATION_RETENTION_DAYS = 14  # Students stay blocked for 14 days
AUDIT_RETENTION_DAYS = 14      # Audit logs move to archive after 14 days
# ═══════════════════════════════════════════════════════════════


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
        Unblock any student whose block has exceeded the 14-day window.
        Called periodically (e.g., every hour) and at server startup.
        """
        if not ACCOUNTS_FILE.exists():
            return {"expired_count": 0, "expired_trnos": []}

        with self._lock:
            try:
                accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8") or "{}")
            except Exception as e:
                logger.error(f"[RetentionPolicy] Could not read accounts: {e}")
                return {"expired_count": 0, "expired_trnos": []}

            now = datetime.now()
            cutoff = now - timedelta(days=VIOLATION_RETENTION_DAYS)
            expired_trnos: List[str] = []

            for trno, acc in accounts.items():
                if acc.get("status") != "blocked":
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

                if blocked_at <= cutoff:
                    days_blocked = (now - blocked_at).days
                    acc["status"] = "active"
                    acc["warning_count"] = 0
                    acc["unblocked_at"] = now.isoformat(timespec="seconds")
                    acc["unblocked_by"] = f"RetentionPolicy (14-day auto-expiry after {days_blocked} days)"
                    acc["blocked_reason"] = (
                        f"[AUTO-EXPIRED] Original reason: {acc.get('blocked_reason', 'Policy violation')} "
                        f"({days_blocked} days served)"
                    )
                    expired_trnos.append(trno)
                    logger.info(
                        f"[RetentionPolicy] Auto-unblocked TRNO {trno} (blocked {days_blocked} days ago)"
                    )

            if expired_trnos:
                try:
                    tmp = ACCOUNTS_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(accounts, indent=2, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(ACCOUNTS_FILE)
                    # Also refresh blocked_trnos.txt
                    from app.services.account_lock_service import account_lock_service
                    account_lock_service._load_from_disk()
                except Exception as e:
                    logger.error(f"[RetentionPolicy] Could not save expired accounts: {e}")

            return {"expired_count": len(expired_trnos), "expired_trnos": expired_trnos}

    def archive_old_audit_logs(self) -> Dict[str, Any]:
        """
        Move audit logs older than 14 days to archive folder.
        They are preserved for compliance but no longer shown in the weekly log.
        """
        if not VIOLATIONS_DIR.exists():
            return {"archived_count": 0, "archived_files": []}

        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        cutoff_date = (datetime.now() - timedelta(days=AUDIT_RETENTION_DAYS)).date()
        archived: List[str] = []

        with self._lock:
            for audit_file in VIOLATIONS_DIR.glob("audit_*.jsonl"):
                try:
                    date_str = audit_file.stem.replace("audit_", "")
                    file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except Exception:
                    continue

                if file_date < cutoff_date:
                    try:
                        dest = ARCHIVE_DIR / audit_file.name
                        shutil.move(str(audit_file), str(dest))
                        archived.append(audit_file.name)
                        logger.info(f"[RetentionPolicy] Archived old audit log: {audit_file.name}")
                    except Exception as e:
                        logger.warning(f"[RetentionPolicy] Could not archive {audit_file.name}: {e}")

        return {"archived_count": len(archived), "archived_files": archived}

    def get_retention_window(self) -> Dict[str, Any]:
        """Return the current 14-day window bounds for the weekly log."""
        now = datetime.now()
        start = now - timedelta(days=VIOLATION_RETENTION_DAYS)
        return {
            "retention_days": VIOLATION_RETENTION_DAYS,
            "window_start": start.strftime("%Y-%m-%d"),
            "window_end": now.strftime("%Y-%m-%d"),
            "policy_text": f"Violations are retained for {VIOLATION_RETENTION_DAYS} days. "
                           f"Blocked accounts auto-unblock after {VIOLATION_RETENTION_DAYS} days.",
        }

    def run_all(self) -> Dict[str, Any]:
        """Run both auto-expire and archive operations. Called from scheduler."""
        expiry = self.auto_expire_blocked_accounts()
        archive = self.archive_old_audit_logs()
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "expired_count": expiry["expired_count"],
            "archived_count": archive["archived_count"],
            "retention_window": self.get_retention_window(),
        }


retention_policy_service = RetentionPolicyService()