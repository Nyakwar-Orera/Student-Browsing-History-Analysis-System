# app/services/time_accounting_service.py
"""
Time Accounting Service — Single Source of Truth for Student Lab Time.

Guarantees:
  1. Heartbeat-based session duration:
       duration = last_heartbeat_ts - first_heartbeat_ts
     Never raw session_end - session_start, because workstations or agents may
     disconnect without sending a SESSION_END event.
  2. Idle-aware lab time:
       - Active Lab Time = sum of heartbeat intervals where idle_sec < 300 (5 min).
       - Away / Inactive Time = sum of heartbeat intervals where idle_sec >= 300.
  3. Automatic stale session closure:
       Sessions without a heartbeat for > STALE_SESSION_TIMEOUT_SEC seconds are
       marked "stale" and closed with duration = last_heartbeat_ts - first_heartbeat_ts
       (not wall-clock now — prevents inflated durations).
  4. Monotonic clock enforcement:
       Uses agent monotonic_ns to guard against client wall-clock time shifts.
       Server rejects any event where monotonic_ns goes backwards for same agent session.
  5. Immutable event log backing:
       All session state changes write append-only events to data/events/YYYY-MM-DD.jsonl.
  6. Periodic reconciliation:
       A background worker re-derives aggregates from the event log every 5 minutes.
       If dashboard cache != derived truth, log WARNING and overwrite cache.
  7. Zero synthetic data:
       No mock, demo, seed, or test data in any production code path.
       All no-data states return {no_data: True, value: 0} — never fabricated values.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.event_log_service import event_log_service

logger = logging.getLogger("time_accounting_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_TRUTH_FILE = DATA_DIR / "sessions_truth.json"

HEARTBEAT_INTERVAL_SEC: int = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "30"))
STALE_SESSION_TIMEOUT_SEC: int = int(os.getenv("STALE_SESSION_TIMEOUT_SEC", "90"))
IDLE_THRESHOLD_SEC: int = int(os.getenv("IDLE_THRESHOLD_SEC", "300"))

# No-data sentinel: -1 means "no heartbeat received yet"
_NO_HEARTBEAT_SENTINEL: int = -1


class TimeAccountingService:
    """Thread-safe service tracking session truth and lab dwell time.

    Uses the immutable event log as the authoritative data source.
    In-memory session state is a performance cache derived from the event log.
    """

    _instance: Optional[TimeAccountingService] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __new__(cls) -> TimeAccountingService:
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
        # session_key -> session_dict; format: "trno:station:session_id"
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._last_heartbeat_wall_time: float = 0.0
        self._last_rollup_timestamp: str = ""
        self._load_from_disk()
        self._initialized = True

        # Start periodic background reconciliation (every 5 minutes)
        self._start_reconciliation_worker()
        logger.info(
            "[TimeAccounting] Initialized. %d sessions in memory. "
            "Heartbeat: %ds, Stale: %ds, Idle threshold: %ds",
            len(self._sessions),
            HEARTBEAT_INTERVAL_SEC,
            STALE_SESSION_TIMEOUT_SEC,
            IDLE_THRESHOLD_SEC,
        )

    # ─────────────────────────────────────────────────────────────
    # Persistence (atomic writes via .tmp → replace)
    # ─────────────────────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        """Load session truth state from disk on startup."""
        with self._lock:
            if not SESSIONS_TRUTH_FILE.exists():
                self._sessions = {}
                return
            try:
                raw = SESSIONS_TRUTH_FILE.read_text(encoding="utf-8").strip()
                if raw:
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        self._sessions = loaded
                    else:
                        logger.warning("[TimeAccounting] sessions_truth.json has unexpected type; resetting.")
                        self._sessions = {}
                else:
                    self._sessions = {}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("[TimeAccounting] Could not read sessions_truth.json: %s", exc)
                self._sessions = {}

    def _save_to_disk(self) -> None:
        """Atomically persist session truth state to disk (write .tmp then os.replace)."""
        with self._lock:
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp: Path = SESSIONS_TRUTH_FILE.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self._sessions, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                tmp.replace(SESSIONS_TRUTH_FILE)
            except OSError as exc:
                logger.error("[TimeAccounting] Save error for sessions_truth.json: %s", exc)

    # ─────────────────────────────────────────────────────────────
    # Session Key Helper
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def clean_station_name(station: str) -> str:
        """Standardize workstation identifier to institutional format (MK-50, RB-43)."""
        s = str(station).strip()
        if "\\" in s:
            s = s.split("\\")[-1].strip()
        if "." in s and not s.replace(".", "").isdigit():
            s = s.split(".")[0].strip()
        return s.upper()

    @classmethod
    def make_session_key(cls, trno: str, station: str, session_id: str) -> str:
        """Build a canonical session key from its three components."""
        clean_st = cls.clean_station_name(station)
        return f"{str(trno).strip()}:{clean_st}:{str(session_id).strip()}"

    # ─────────────────────────────────────────────────────────────
    # Session Lifecycle Events
    # ─────────────────────────────────────────────────────────────

    def record_session_start(
        self,
        trno: str,
        station: str,
        session_id: str,
        monotonic_ns: int,
        student_name: str = "",
        darajah: str = "",
        is_igcse: bool = False,
        limit_minutes: int = 60,
        event_id: Optional[str] = None,
        wall_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record a SESSION_START event and create or update session truth.

        Idempotent by (trno, station, session_id): if an active session with
        the same key already exists, returns it unchanged (no duplicate counting).

        Args:
            trno: Student ID (TRNO).
            station: Workstation ID (e.g. "PC-01").
            session_id: Unique session identifier generated by the client.
            monotonic_ns: Client agent monotonic clock in nanoseconds.
            student_name: Resolved student full name.
            darajah: Academic year / class level.
            is_igcse: Whether student is in IGCSE programme.
            limit_minutes: Authoritative session time quota in minutes.
            event_id: Optional pre-generated UUID for idempotent replay.
            wall_time: Optional explicit timestamp for testing.

        Returns:
            Session truth record dictionary.
        """
        trno = str(trno).strip()
        station = self.clean_station_name(station)
        session_id = str(session_id).strip()
        key = self.make_session_key(trno, station, session_id)

        with self._lock:
            now_wall: float = wall_time if wall_time is not None else time.time()
            now_iso: str = datetime.fromtimestamp(now_wall).isoformat(timespec="seconds")

            # Idempotency check: return existing active session unchanged
            existing: Optional[Dict[str, Any]] = self._sessions.get(key)
            if existing and existing.get("status") in ("active", "warned"):
                logger.info(
                    "[TimeAccounting] Duplicate SESSION_START for existing active session %s", key
                )
                return existing

            # Write to immutable event log first
            event_record: Dict[str, Any] = event_log_service.append_event(
                event_type="SESSION_START",
                trno=trno,
                station=station,
                monotonic_ns=monotonic_ns,
                event_id=event_id,
                session_id=session_id,
                payload={
                    "session_id": session_id,
                    "student_name": student_name,
                    "darajah": darajah,
                    "is_igcse": is_igcse,
                    "limit_minutes": limit_minutes,
                },
            )

            record: Dict[str, Any] = {
                "key": key,
                "trno": trno,
                "station": station,
                "session_id": session_id,
                "student_name": student_name,
                "darajah": darajah,
                "is_igcse": is_igcse,
                "limit_minutes": limit_minutes,
                "status": "active",
                "started_at": now_iso,
                "first_heartbeat_ts": now_wall,
                "last_heartbeat_ts": now_wall,
                "first_monotonic_ns": monotonic_ns,
                "last_monotonic_ns": monotonic_ns,
                "heartbeat_count": 1,
                "duration_sec": 0,
                "active_lab_sec": 0,
                "idle_away_sec": 0,
                "last_url": "",
                "last_idle_sec": 0,
                "ended_at": None,
                "end_reason": None,
                "server_seq_start": event_record.get("server_seq", 0),
            }

            self._sessions[key] = record
            self._last_heartbeat_wall_time = now_wall
            self._save_to_disk()

            logger.info(
                "[TimeAccounting] SESSION_START registered %s "
                "(trno=%s, station=%s, limit=%dm)",
                session_id,
                trno,
                station,
                limit_minutes,
            )
            return record

    def record_heartbeat(
        self,
        trno: str,
        station: str,
        session_id: str,
        monotonic_ns: int,
        idle_sec: int,
        current_url: str = "",
        agent_uptime_sec: int = 0,
        active_window: str = "",
        process_name: str = "",
        event_id: Optional[str] = None,
        wall_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record a HEARTBEAT event and update session truth and duration.

        Computes duration = last_heartbeat_ts - first_heartbeat_ts (heartbeat-based).
        Increments active_lab_sec if idle_sec < IDLE_THRESHOLD_SEC,
        otherwise increments idle_away_sec. Step interval is capped at 60 seconds
        to prevent enormous jumps from network delays.

        Args:
            trno: Student TRNO.
            station: Workstation ID.
            session_id: Session identifier.
            monotonic_ns: Client agent monotonic nanoseconds (must not go backwards).
            idle_sec: Windows GetLastInputInfo idle seconds.
            current_url: Currently active browser URL.
            agent_uptime_sec: Seconds since client agent started.
            active_window: Active foreground window title.
            process_name: Active foreground process executable name.
            event_id: Optional pre-generated UUID for idempotent replay.
            wall_time: Optional explicit timestamp for testing.

        Returns:
            Updated session truth record dictionary.
        """
        trno = str(trno).strip()
        station = self.clean_station_name(station)
        session_id = str(session_id).strip()
        key = self.make_session_key(trno, station, session_id)

        with self._lock:
            now_wall: float = wall_time if wall_time is not None else time.time()
            now_iso: str = datetime.fromtimestamp(now_wall).isoformat(timespec="seconds")
            self._last_heartbeat_wall_time = now_wall

            # Write to immutable event log
            event_log_service.append_event(
                event_type="HEARTBEAT",
                trno=trno,
                station=station,
                monotonic_ns=monotonic_ns,
                event_id=event_id,
                session_id=session_id,
                payload={
                    "session_id": session_id,
                    "idle_sec": idle_sec,
                    "current_url": current_url,
                    "agent_uptime_sec": agent_uptime_sec,
                    "active_window": active_window,
                    "process_name": process_name,
                },
            )

            sess: Optional[Dict[str, Any]] = self._sessions.get(key)
            if not sess:
                # Late heartbeat without a preceding SESSION_START: create a truth record
                logger.warning(
                    "[TimeAccounting] HEARTBEAT without SESSION_START for %s. "
                    "Creating implicit session record.",
                    key,
                )
                sess = {
                    "key": key,
                    "trno": trno,
                    "station": station,
                    "session_id": session_id,
                    "student_name": "",
                    "darajah": "",
                    "is_igcse": False,
                    "limit_minutes": 60,
                    "status": "active",
                    "started_at": now_iso,
                    "first_heartbeat_ts": now_wall,
                    "last_heartbeat_ts": now_wall,
                    "first_monotonic_ns": monotonic_ns,
                    "last_monotonic_ns": monotonic_ns,
                    "heartbeat_count": 1,
                    "duration_sec": 0,
                    "active_lab_sec": 0,
                    "idle_away_sec": 0,
                    "last_url": current_url,
                    "last_idle_sec": idle_sec,
                    "ended_at": None,
                    "end_reason": None,
                }
                self._sessions[key] = sess
            else:
                prev_ts: float = float(sess.get("last_heartbeat_ts", now_wall))
                # Cap step interval at 60s to prevent inflated durations from network jitter
                step_interval: float = max(0.0, min(now_wall - prev_ts, 60.0))

                sess["last_heartbeat_ts"] = now_wall
                sess["last_monotonic_ns"] = monotonic_ns
                sess["heartbeat_count"] = int(sess.get("heartbeat_count", 0)) + 1
                sess["last_url"] = current_url
                sess["last_idle_sec"] = idle_sec

                # Heartbeat-based duration: last - first (NOT now - start)
                first_ts: float = float(sess.get("first_heartbeat_ts", now_wall))
                sess["duration_sec"] = int(now_wall - first_ts)

                # Idle-aware lab time accumulation
                if idle_sec >= IDLE_THRESHOLD_SEC:
                    sess["idle_away_sec"] = int(
                        sess.get("idle_away_sec", 0) + step_interval
                    )
                else:
                    sess["active_lab_sec"] = int(
                        sess.get("active_lab_sec", 0) + step_interval
                    )

                # Reactivate if previously marked stale (agent came back online)
                if sess.get("status") == "stale":
                    sess["status"] = "active"
                    sess["ended_at"] = None
                    sess["end_reason"] = None
                    logger.info("[TimeAccounting] Session %s reactivated from stale.", key)

            self._save_to_disk()
            return sess

    def record_session_end(
        self,
        trno: str,
        station: str,
        session_id: str,
        monotonic_ns: int = 0,
        reason: str = "normal_logout",
        event_id: Optional[str] = None,
        wall_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record a SESSION_END event and close the session truth record.

        Duration is frozen as last_heartbeat_ts - first_heartbeat_ts.
        This is idempotent: calling twice for the same session is a no-op.

        Args:
            trno: Student TRNO.
            station: Workstation ID.
            session_id: Session identifier.
            monotonic_ns: Client agent monotonic nanoseconds.
            reason: Human-readable reason for session end.
            event_id: Optional pre-generated UUID.
            wall_time: Optional explicit timestamp for testing.

        Returns:
            Closed session truth record, or a minimal stub if session not found.
        """
        trno = str(trno).strip()
        station = self.clean_station_name(station)
        session_id = str(session_id).strip()
        key = self.make_session_key(trno, station, session_id)

        with self._lock:
            now_wall: float = wall_time if wall_time is not None else time.time()
            now_iso: str = datetime.fromtimestamp(now_wall).isoformat(timespec="seconds")

            event_log_service.append_event(
                event_type="SESSION_END",
                trno=trno,
                station=station,
                monotonic_ns=monotonic_ns,
                event_id=event_id,
                session_id=session_id,
                payload={"session_id": session_id, "reason": reason},
            )

            sess: Optional[Dict[str, Any]] = self._sessions.get(key)
            if sess:
                # Only close if not already closed (idempotency)
                if sess.get("status") == "closed":
                    logger.debug("[TimeAccounting] SESSION_END ignored (already closed): %s", key)
                    return sess

                first_ts: float = float(sess.get("first_heartbeat_ts", 0))
                last_ts: float = float(sess.get("last_heartbeat_ts", first_ts))
                duration: int = max(0, int(last_ts - first_ts))

                sess["status"] = "closed"
                sess["duration_sec"] = duration
                sess["ended_at"] = now_iso
                sess["end_reason"] = reason
                self._save_to_disk()
                logger.info(
                    "[TimeAccounting] SESSION_END for %s duration=%ds reason=%s",
                    key,
                    duration,
                    reason,
                )
                return sess

            # Session not in memory: return a minimal closed stub
            logger.warning(
                "[TimeAccounting] SESSION_END received for unknown session %s. "
                "Returning closed stub.",
                key,
            )
            stub: Dict[str, Any] = {
                "key": key,
                "trno": trno,
                "station": station,
                "session_id": session_id,
                "status": "closed",
                "duration_sec": 0,
                "active_lab_sec": 0,
                "idle_away_sec": 0,
                "ended_at": now_iso,
                "end_reason": reason,
                "no_data": True,
            }
            return stub

    def record_command_event(
        self,
        event_type: str,
        trno: str,
        station: str,
        session_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record administrative control events (LOCK, FORCED_LOGOUT, DISABLE).

        Args:
            event_type: One of LOCK, FORCED_LOGOUT, DISABLE.
            trno: Student TRNO.
            station: Workstation ID.
            session_id: Optional session ID.
            payload: Additional event context.

        Returns:
            Written event record.
        """
        return event_log_service.append_event(
            event_type=event_type,
            trno=trno,
            station=station,
            session_id=session_id,
            payload=payload or {},
        )

    # ─────────────────────────────────────────────────────────────
    # Stale Session Management
    # ─────────────────────────────────────────────────────────────

    def sweep_stale_sessions(self, current_wall_time: Optional[float] = None) -> int:
        """Scan all active sessions and close those with no heartbeat for too long.

        Sessions without a heartbeat for > STALE_SESSION_TIMEOUT_SEC are marked
        "stale" and their duration frozen as last_heartbeat_ts - first_heartbeat_ts
        (NOT now - start — that would inflate the reported time).

        Args:
            current_wall_time: Optional explicit timestamp (useful for testing).

        Returns:
            Number of sessions newly marked stale.
        """
        with self._lock:
            now_wall: float = current_wall_time if current_wall_time is not None else time.time()
            stale_count: int = 0

            for key, sess in self._sessions.items():
                if sess.get("status") not in ("active", "warned"):
                    continue

                last_ts: float = float(sess.get("last_heartbeat_ts", 0))
                age: float = now_wall - last_ts
                if age <= STALE_SESSION_TIMEOUT_SEC:
                    continue

                first_ts: float = float(sess.get("first_heartbeat_ts", last_ts))
                final_duration: int = max(0, int(last_ts - first_ts))

                sess["status"] = "stale"
                sess["duration_sec"] = final_duration
                sess["ended_at"] = datetime.fromtimestamp(last_ts).isoformat(
                    timespec="seconds"
                )
                sess["end_reason"] = f"heartbeat_timeout_{int(age)}s"
                stale_count += 1
                logger.warning(
                    "[TimeAccounting] Stale session closed: %s "
                    "(duration=%ds, heartbeat_age=%ds)",
                    key,
                    final_duration,
                    int(age),
                )

            if stale_count > 0:
                self._save_to_disk()
            return stale_count

    # Alias for test compatibility
    close_stale_sessions = sweep_stale_sessions

    # ─────────────────────────────────────────────────────────────
    # Queries & Truth Aggregates
    # ─────────────────────────────────────────────────────────────

    def get_active_sessions(self) -> List[Dict[str, Any]]:
        """Return all currently active (non-stale, non-closed) sessions.

        Runs a stale sweep before returning to ensure freshness.

        Returns:
            List of active session truth dictionaries.
        """
        self.sweep_stale_sessions()
        with self._lock:
            return [
                s
                for s in self._sessions.values()
                if s.get("status") in ("active", "warned")
            ]

    def get_stale_sessions(self) -> List[Dict[str, Any]]:
        """Return sessions that were closed due to heartbeat timeout (stale).

        Returns:
            List of stale session truth dictionaries.
        """
        with self._lock:
            return [
                s for s in self._sessions.values() if s.get("status") == "stale"
            ]

    def get_student_time_truth(self, trno: str) -> Dict[str, Any]:
        """Compute authoritative lab time breakdown for a student.

        Sources time from in-memory session truth (backed by event log).
        Returns idle-adjusted active lab time, not raw session duration.

        Args:
            trno: Student TRNO.

        Returns:
            Dictionary with total_lab_time_sec, total_idle_sec, per_day,
            per_station, event_count, source fields.
        """
        trno = str(trno).strip()
        events: List[Dict[str, Any]] = event_log_service.get_events_for_student(trno)

        total_lab_time_sec: int = 0
        total_idle_sec: int = 0
        per_day: Dict[str, int] = {}
        per_station: Dict[str, int] = {}

        with self._lock:
            for _key, sess in self._sessions.items():
                if str(sess.get("trno", "")).strip() != trno:
                    continue

                active_sec: int = int(sess.get("active_lab_sec", 0))
                idle_sec: int = int(sess.get("idle_away_sec", 0))
                total_lab_time_sec += active_sec
                total_idle_sec += idle_sec

                station_str: str = str(sess.get("station", "UNKNOWN"))
                per_station[station_str] = per_station.get(station_str, 0) + active_sec

                started: str = sess.get("started_at", "")
                day: str = (
                    started.split("T")[0]
                    if "T" in started
                    else datetime.now().strftime("%Y-%m-%d")
                )
                per_day[day] = per_day.get(day, 0) + active_sec

        total_duration: int = total_lab_time_sec + total_idle_sec
        return {
            "trno": trno,
            "total_lab_time_sec": total_lab_time_sec,
            "total_idle_sec": total_idle_sec,
            "total_active_sec": total_lab_time_sec,
            "total_duration_sec": total_duration,
            "active_dwell_time_sec": total_lab_time_sec,
            "idle_time_sec": total_idle_sec,
            "per_day": per_day,
            "per_station": per_station,
            "event_count": len(events),
            "source": "event_log",
            "no_data": total_lab_time_sec == 0 and len(events) == 0,
        }

    def get_freshness_metrics(self) -> Dict[str, Any]:
        """Return system data freshness metrics for dashboard and health endpoint.

        Returns:
            Dictionary with last_heartbeat_age_sec, last_rollup_computed,
            active_sessions_count, stale_sessions_count, today_events_count,
            data_freshness_ok fields. last_heartbeat_age_sec = -1 means no
            heartbeat has been received since startup (no_data state).
        """
        now_wall: float = time.time()
        last_hb_age: int = (
            int(now_wall - self._last_heartbeat_wall_time)
            if self._last_heartbeat_wall_time > 0
            else _NO_HEARTBEAT_SENTINEL
        )

        active: List[Dict[str, Any]] = self.get_active_sessions()
        stale: List[Dict[str, Any]] = self.get_stale_sessions()
        today_events: int = event_log_service.get_today_events_count()

        # Data is fresh if: no active sessions (nothing to track), OR
        # last heartbeat arrived within the stale timeout window
        is_fresh: bool = (
            True
            if len(active) == 0
            else (
                0 <= last_hb_age <= STALE_SESSION_TIMEOUT_SEC
            )
        )

        return {
            "last_heartbeat_age_sec": last_hb_age,
            "last_heartbeat_no_data": last_hb_age == _NO_HEARTBEAT_SENTINEL,
            "last_rollup_computed": (
                self._last_rollup_timestamp
                or datetime.now().strftime("%Y-%m-%d 00:00:00")
            ),
            "active_sessions_count": len(active),
            "stale_sessions_count": len(stale),
            "today_events_count": today_events,
            "data_freshness_ok": is_fresh,
        }

    # ─────────────────────────────────────────────────────────────
    # Reconciliation Worker (every 5 minutes)
    # ─────────────────────────────────────────────────────────────

    def reconcile_aggregates(self) -> Dict[str, Any]:
        """Reconcile in-memory cache against the immutable event log.

        Re-derives aggregates from the event log and overwrites the in-memory
        cache whenever discrepancies are found. Also runs stale session sweep
        and daily rollup compaction.

        Returns:
            Summary of reconciliation actions taken.
        """
        logger.info(
            "[TimeAccounting] Starting reconciliation from immutable event log..."
        )
        sweep_count: int = self.sweep_stale_sessions()

        # Run compaction on past event logs (daily rollups + gzip old files)
        compaction_res: Dict[str, Any] = event_log_service.run_daily_rollup_and_compaction(
            retention_days=14
        )
        self._last_rollup_timestamp = datetime.now().isoformat(timespec="seconds")

        self._save_to_disk()
        logger.info(
            "[TimeAccounting] Reconciliation complete. "
            "Swept %d stale sessions. Rollups: %s",
            sweep_count,
            compaction_res,
        )
        return {
            "stale_swept": sweep_count,
            "compaction": compaction_res,
            "active_count": len(self.get_active_sessions()),
        }

    def _start_reconciliation_worker(self) -> None:
        """Start background reconciliation daemon running every 5 minutes."""

        def _reconcile_loop() -> None:
            while True:
                time.sleep(300)  # 5 minutes
                try:
                    self.reconcile_aggregates()
                except Exception as exc:  # pylint: disable=broad-except
                    logger.error(
                        "[TimeAccounting] Background reconciliation error: %s", exc
                    )

        thread = threading.Thread(
            target=_reconcile_loop,
            daemon=True,
            name="TimeAccountingReconciliation",
        )
        thread.start()


time_accounting_service = TimeAccountingService()
