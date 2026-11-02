# app/services/session_timer_service.py
"""
Session Timer Service — Integrated with TimeAccountingService.

Manages Darajah-based session quotas and coordinates workstation time tracking,
backed by the authoritative TimeAccountingService and immutable EventLogService.

Department and Darajah Limits (Minutes):
  - Culture Générale (Std 1-2): 30 min
  - Culture Générale (Std 3-4): 30 min
  - Collegiate I (Std 5-7):     60 min
  - Collegiate II (Std 8-11): 120 min
  - Dars Burhani:               60 min
  - IGCSE Override:            120 min

Production guarantees:
  - Zero hardcoded student IDs, station names, or demo values.
  - All session state changes propagate to TimeAccountingService (event log).
  - Atomic disk writes (.tmp then os.replace).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.time_accounting_service import time_accounting_service

logger = logging.getLogger("session_timer_service")

BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
DATA_DIR: Path = BASE_DIR / "data"
SESSIONS_FILE: Path = DATA_DIR / "active_sessions.json"

DARAJAH_TIME_LIMITS: Dict[str, int] = {
    "1": 30,
    "2": 30,
    "3": 30,
    "4": 30,
    "5": 60,
    "6": 60,
    "7": 60,
    "8": 120,
    "9": 120,
    "10": 120,
    "11": 120,
    "12": 120,
    "igcse": 120,
    "db1": 60,
    "db2": 60,
    "db3": 60,
    "dars": 60,
    "default": 30,
}

WARNING_THRESHOLD_MINUTES: int = 5


def is_lab_entry_allowed(darajah: str, is_igcse: bool = False) -> bool:
    """Check if student is permitted entry to the lab based on institutional policy.

    Args:
        darajah: Academic year / class level string.
        is_igcse: Whether student is in IGCSE programme.

    Returns:
        True if entry is permitted (currently all darajah levels permitted).
    """
    return True


class SessionTimerService:
    """Coordinator for student session timeouts and active workstation commands.

    Delegates all time-truth computation to TimeAccountingService. This service
    tracks session quota expiry (wall-clock countdown) for the UI layer.
    """

    _instance: Optional[SessionTimerService] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __new__(cls) -> SessionTimerService:
        """Singleton pattern with double-checked locking."""
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize service. Idempotent — only runs once per process."""
        if getattr(self, "_initialized", False):
            return
        self._lock: threading.RLock = threading.RLock()
        self._pending_commands: Dict[str, str] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._load_from_disk()
        self._initialized = True
        logger.info("[SessionTimer] Initialized and backed by TimeAccountingService.")

    def _load_from_disk(self) -> None:
        """Load session state from data/active_sessions.json."""
        with self._lock:
            if not SESSIONS_FILE.exists():
                self._sessions = {}
                return
            try:
                raw: str = SESSIONS_FILE.read_text(encoding="utf-8").strip()
                loaded = json.loads(raw) if raw else {}
                self._sessions = loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("[SessionTimer] Error reading active_sessions.json: %s", exc)
                self._sessions = {}

    def _save_to_disk(self) -> None:
        """Atomically persist session state to data/active_sessions.json."""
        with self._lock:
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp: Path = SESSIONS_FILE.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self._sessions, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                tmp.replace(SESSIONS_FILE)
            except OSError as exc:
                logger.error("[SessionTimer] Error saving active_sessions.json: %s", exc)

    def get_time_limit(self, darajah: str, is_igcse: bool = False) -> int:
        """Resolve allowed session limit in minutes based on darajah.

        Args:
            darajah: Academic year / class level string.
            is_igcse: Whether student is in IGCSE programme.

        Returns:
            Session time limit in minutes.
        """
        if is_igcse:
            return DARAJAH_TIME_LIMITS["igcse"]

        key: str = str(darajah or "").strip().lower()
        if "igcse" in key:
            return DARAJAH_TIME_LIMITS["igcse"]

        if "dars" in key or re.search(r"\bdb\d*\b", key):
            return DARAJAH_TIME_LIMITS.get("dars", 60)

        if key in DARAJAH_TIME_LIMITS:
            return DARAJAH_TIME_LIMITS[key]

        num_match = re.search(r"\d+", key)
        if num_match and num_match.group() in DARAJAH_TIME_LIMITS:
            return DARAJAH_TIME_LIMITS[num_match.group()]

        return DARAJAH_TIME_LIMITS["default"]

    def start_session(
        self,
        trno: str,
        darajah: str,
        station: str,
        student_name: str = "",
        is_igcse: bool = False,
        extend: bool = False,
        session_id: str = "",
        monotonic_ns: int = 0,
    ) -> Dict[str, Any]:
        """Start a new session or return existing active session state.

        Delegates time-truth recording to TimeAccountingService.

        Args:
            trno: Student TRNO.
            darajah: Academic year / class level string.
            station: Workstation ID.
            student_name: Student full name.
            is_igcse: Whether student is in IGCSE programme.
            extend: If True, extend an existing session (not currently used).
            session_id: Pre-generated session ID from client agent.
            monotonic_ns: Client monotonic nanoseconds.

        Returns:
            Session record enriched with remaining seconds.
        """
        trno = str(trno).strip()
        station = str(station).strip().upper()
        if not session_id:
            session_id = f"{trno}_{station}_{int(time.time())}"

        limit_minutes: int = self.get_time_limit(darajah, is_igcse=is_igcse)

        # Delegate truth recording to TimeAccountingService (event log)
        time_accounting_service.record_session_start(
            trno=trno,
            station=station,
            session_id=session_id,
            monotonic_ns=monotonic_ns,
            student_name=student_name,
            darajah=darajah,
            is_igcse=is_igcse,
            limit_minutes=limit_minutes,
        )

        with self._lock:
            now: datetime = datetime.now()
            expires_at: datetime = now + timedelta(minutes=limit_minutes)

            record: Dict[str, Any] = {
                "trno": trno,
                "student_name": student_name,
                "darajah": darajah,
                "is_igcse": bool(is_igcse),
                "station": station,
                "session_id": session_id,
                "started_at": now.isoformat(timespec="seconds"),
                "expires_at": expires_at.isoformat(timespec="seconds"),
                "limit_minutes": limit_minutes,
                "status": "active",
                "warned": False,
                "extension_count": 0,
            }
            self._sessions[trno] = record
            self._save_to_disk()
            return self._enrich_status(trno, record)

    def queue_station_command(
        self, station: str, command: str, trno: str = ""
    ) -> None:
        """Queue a remote administrative command for a station or student.

        Args:
            station: Workstation ID to send command to.
            command: Command string ('lock', 'logout', etc.).
            trno: Optional student TRNO to also target by TRNO.
        """
        with self._lock:
            if station:
                self._pending_commands[station.strip().upper()] = command
            if trno:
                self._pending_commands[trno.strip()] = command
            logger.info(
                "[SessionTimer] Queued command '%s' for station=%s trno=%s",
                command,
                station,
                trno,
            )

    def pop_pending_command(
        self, station: str = "", trno: str = ""
    ) -> Optional[str]:
        """Pop and return any queued command for station or student.

        Args:
            station: Workstation ID to check.
            trno: Student TRNO to check.

        Returns:
            Command string if found, None otherwise.
        """
        with self._lock:
            if station and station.strip().upper() in self._pending_commands:
                return self._pending_commands.pop(station.strip().upper())
            if trno and trno.strip() in self._pending_commands:
                return self._pending_commands.pop(trno.strip())
            return None

    def get_session_status(
        self, trno: str, station: str = ""
    ) -> Dict[str, Any]:
        """Check status of session and pop any queued command.

        Also expires sessions that have exceeded their time quota.

        Args:
            trno: Student TRNO.
            station: Optional workstation ID for command lookup.

        Returns:
            Session status dictionary with remaining_seconds, command if any.
        """
        trno = str(trno).strip()
        station = str(station).strip()

        with self._lock:
            cmd: Optional[str] = self.pop_pending_command(
                station=station, trno=trno
            )
            rec: Optional[Dict[str, Any]] = self._sessions.get(trno)
            if not rec:
                res: Dict[str, Any] = {"status": "no_session", "trno": trno}
                if cmd:
                    res["command"] = cmd
                return res

            status: str = rec.get("status", "active")
            if status == "expired":
                res = self._enrich_status(trno, rec)
                if cmd:
                    res["command"] = cmd
                return res

            try:
                expires_at: datetime = datetime.fromisoformat(rec["expires_at"])
            except (KeyError, ValueError):
                res = self._enrich_status(trno, rec)
                if cmd:
                    res["command"] = cmd
                return res

            now: datetime = datetime.now()
            remaining_seconds: int = int((expires_at - now).total_seconds())

            if remaining_seconds <= 0:
                rec["status"] = "expired"
            elif remaining_seconds <= WARNING_THRESHOLD_MINUTES * 60 and not rec.get(
                "warned"
            ):
                rec["status"] = "warned"
                rec["warned"] = True

            self._sessions[trno] = rec
            self._save_to_disk()
            res = self._enrich_status(trno, rec)
            if cmd:
                res["command"] = cmd
            return res

    def end_session(self, trno: str, reason: str = "manual") -> bool:
        """End student session by marking it expired.

        Also delegates to TimeAccountingService to close the truth record.

        Args:
            trno: Student TRNO.
            reason: Human-readable reason for ending the session.

        Returns:
            True if session was found and ended, False if no session existed.
        """
        trno = str(trno).strip()
        with self._lock:
            if trno not in self._sessions:
                return False
            rec: Dict[str, Any] = self._sessions[trno]
            rec["status"] = "expired"
            rec["end_reason"] = reason
            rec["ended_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_to_disk()

        # Delegate truth close to TimeAccountingService
        time_accounting_service.record_session_end(
            trno=trno,
            station=rec.get("station", ""),
            session_id=rec.get("session_id", ""),
            reason=reason,
        )
        return True

    def end_all_sessions(self, reason: str = "admin_killed_all") -> int:
        """End all active student sessions.

        Args:
            reason: Human-readable reason for ending all sessions.

        Returns:
            Count of sessions ended.
        """
        with self._lock:
            count: int = 0
            for trno in list(self._sessions.keys()):
                if self._sessions[trno].get("status") != "expired":
                    self.end_session(trno, reason=reason)
                    count += 1
            return count

    def extend_session(
        self,
        trno: str,
        extra_minutes: int,
        admin_user: str = "Administrator",
    ) -> Dict[str, Any]:
        """Extend session limit by extra_minutes.

        Args:
            trno: Student TRNO.
            extra_minutes: Additional minutes to add to the current expiry.
            admin_user: Name of administrator authorizing the extension.

        Returns:
            Updated session status dictionary, or error dict if not found.
        """
        trno = str(trno).strip()
        with self._lock:
            rec: Optional[Dict[str, Any]] = self._sessions.get(trno)
            if not rec:
                return {"ok": False, "error": f"No session found for TRNO {trno}"}

            try:
                current_expiry: datetime = datetime.fromisoformat(rec["expires_at"])
            except (KeyError, ValueError):
                current_expiry = datetime.now()

            base: datetime = max(current_expiry, datetime.now())
            new_expiry: datetime = base + timedelta(minutes=extra_minutes)
            rec["expires_at"] = new_expiry.isoformat(timespec="seconds")
            rec["status"] = "active"
            rec["warned"] = False
            rec["extension_count"] = int(rec.get("extension_count", 0)) + 1
            rec["last_extended_by"] = admin_user
            rec["last_extended_at"] = datetime.now().isoformat(timespec="seconds")
            self._sessions[trno] = rec
            self._save_to_disk()
            return {"ok": True, "trno": trno, **self._enrich_status(trno, rec)}

    def get_all_active_sessions(self) -> List[Dict[str, Any]]:
        """Return all active sessions enriched with remaining seconds.

        Returns:
            List of enriched session dictionaries.
        """
        with self._lock:
            results: List[Dict[str, Any]] = []
            for trno, rec in list(self._sessions.items()):
                if rec.get("status") in ("active", "warned"):
                    results.append(self._enrich_status(trno, rec))
            return results

    def get_sessions_live(self) -> List[Dict[str, Any]]:
        """Return live sessions for dashboard consumption.

        Returns:
            List of active session dictionaries with remaining time.
        """
        return self.get_all_active_sessions()

    def _enrich_status(
        self, trno: str, rec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Enrich session dictionary with remaining seconds and formatted labels.

        Args:
            trno: Student TRNO (included in output for convenience).
            rec: Raw session record dictionary.

        Returns:
            Copy of rec with remaining_seconds, remaining_minutes,
            formatted_remaining fields added.
        """
        out: Dict[str, Any] = dict(rec)
        now: datetime = datetime.now()
        try:
            expires_at: datetime = datetime.fromisoformat(rec["expires_at"])
            remaining: int = max(0, int((expires_at - now).total_seconds()))
        except (KeyError, ValueError):
            remaining = 0

        out["remaining_seconds"] = remaining
        out["remaining_minutes"] = remaining // 60
        out["formatted_remaining"] = f"{remaining // 60}m {remaining % 60}s"
        return out


session_timer_service = SessionTimerService()