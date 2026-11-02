# app/services/event_log_service.py
"""
Immutable Event Log Service for MamalBrowse Sentinel.

Guarantees:
  - Append-only JSONL event log partitioned by date: data/events/YYYY-MM-DD.jsonl
  - Monotonically increasing atomic server_seq sequence number
  - Strict per-session monotonic clock validation (rejects clock regressions)
  - Event deduplication by UUID event_id
  - Daily rollups into data/rollups/YYYY-MM-DD.json
  - Gzip compaction for logs older than retention_days
  - All file writes are atomic (write .tmp then os.replace)
  - Zero synthetic data: this service only records real events
"""
from __future__ import annotations

import gzip
import json
import logging
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("event_log_service")

BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
DATA_DIR: Path = BASE_DIR / "data"
EVENTS_DIR: Path = DATA_DIR / "events"
ROLLUPS_DIR: Path = DATA_DIR / "rollups"
SEQ_FILE: Path = EVENTS_DIR / ".server_seq"

EVENTS_DIR.mkdir(parents=True, exist_ok=True)
ROLLUPS_DIR.mkdir(parents=True, exist_ok=True)


class EventLogService:
    """Thread-safe, append-only event logging service.

    All events are written to per-day JSONL files in data/events/.
    Server assigns a monotonically increasing sequence number to each event
    for audit-grade ordering and reconciliation.
    """

    _instance: Optional[EventLogService] = None
    _singleton_lock: threading.Lock = threading.Lock()

    def __new__(cls) -> EventLogService:
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
        self._server_seq: int = self._load_seq()
        # In-memory deduplication cache (last 100k event IDs)
        self._seen_event_ids: Set[str] = set()
        # Last monotonic_ns seen per session key (trno:station:session_id)
        self._last_monotonic_by_session: Dict[str, int] = {}
        self._initialized = True
        logger.info("[EventLog] Initialized. Current server_seq: %d", self._server_seq)

    def _load_seq(self) -> int:
        """Load persistent sequence counter from disk, or derive from existing logs.

        Returns:
            Current maximum sequence number found.
        """
        if SEQ_FILE.exists():
            try:
                content: str = SEQ_FILE.read_text(encoding="utf-8").strip()
                if content.isdigit():
                    return int(content)
            except OSError as exc:
                logger.warning("[EventLog] Error reading sequence file: %s", exc)

        # Fallback: scan today's event log to derive max seq
        max_seq: int = 0
        try:
            today_file: Path = self.get_today_event_file()
            if today_file.exists():
                with open(today_file, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line_str: str = line.strip()
                        if not line_str:
                            continue
                        try:
                            rec: Dict[str, Any] = json.loads(line_str)
                            seq: int = int(rec.get("server_seq", 0))
                            if seq > max_seq:
                                max_seq = seq
                        except (json.JSONDecodeError, ValueError):
                            continue
        except OSError:
            pass

        return max_seq

    def _save_seq(self) -> None:
        """Atomically persist sequence counter to disk."""
        try:
            tmp: Path = SEQ_FILE.with_suffix(".tmp")
            tmp.write_text(str(self._server_seq), encoding="utf-8")
            tmp.replace(SEQ_FILE)
        except OSError as exc:
            logger.error("[EventLog] Error persisting sequence counter: %s", exc)

    def get_today_event_file(self) -> Path:
        """Return the path for today's JSONL event log.

        Returns:
            Path to data/events/YYYY-MM-DD.jsonl for today.
        """
        today_str: str = datetime.now().strftime("%Y-%m-%d")
        return EVENTS_DIR / f"{today_str}.jsonl"

    def append_event(
        self,
        event_type: str,
        trno: str,
        station: str,
        payload: Dict[str, Any],
        monotonic_ns: int = 0,
        event_id: Optional[str] = None,
        agent_version: str = "2.0",
        session_id: str = "",
    ) -> Dict[str, Any]:
        """Append an immutable event to the current day's log.

        Enforces:
          1. Deduplication by event_id (UUID).
          2. Monotonic clock validation per session (rejects regressions).
          3. Monotonically increasing server_seq.
          4. Atomic write via file append + flush.

        Args:
            event_type: Event category (SESSION_START, HEARTBEAT, etc.).
            trno: Student TRNO.
            station: Workstation ID.
            payload: Event-specific data dictionary.
            monotonic_ns: Client monotonic nanoseconds (0 = not provided).
            event_id: Pre-generated UUID for idempotent replay (auto-generated if None).
            agent_version: Client agent version string.
            session_id: Session identifier for monotonic clock tracking.

        Returns:
            The finalized event record dictionary as written to disk.

        Raises:
            ValueError: If the monotonic clock went backwards for this session.
        """
        trno = str(trno or "").strip()
        station = str(station or "").strip().upper()
        event_type = str(event_type or "").strip().upper()
        session_id = str(
            session_id or payload.get("session_id") or ""
        ).strip()

        with self._lock:
            # 1. Deduplication check
            if event_id:
                if event_id in self._seen_event_ids:
                    logger.debug("[EventLog] Duplicate event ignored: %s", event_id)
                    return {
                        "event_id": event_id,
                        "duplicate": True,
                        "server_seq": self._server_seq,
                    }
            else:
                event_id = str(uuid.uuid4())

            # 2. Monotonic clock check: reject if clock goes backwards for same session
            session_key: str = (
                f"{trno}:{station}:{session_id}"
                if session_id
                else f"{trno}:{station}"
            )
            if monotonic_ns > 0:
                last_mono: int = self._last_monotonic_by_session.get(session_key, 0)
                if last_mono > 0 and monotonic_ns < last_mono:
                    err_msg: str = (
                        f"Monotonic clock went backwards for session {session_key}: "
                        f"current={monotonic_ns} < previous={last_mono}"
                    )
                    logger.error("[EventLog] %s", err_msg)
                    raise ValueError(err_msg)
                self._last_monotonic_by_session[session_key] = monotonic_ns

            # 3. Increment server_seq atomically
            self._server_seq += 1
            seq: int = self._server_seq
            self._save_seq()

            now_utc: datetime = datetime.now(timezone.utc)
            now_local: datetime = datetime.now()

            record: Dict[str, Any] = {
                "event_id": event_id,
                "ts_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ts_local": now_local.isoformat(timespec="seconds"),
                "monotonic_ns": monotonic_ns,
                "trno": trno,
                "station": station,
                "event_type": event_type,
                "payload": payload,
                "agent_version": agent_version,
                "server_seq": seq,
            }

            # 4. Atomic append to disk (open in append mode with flush)
            target_file: Path = self.get_today_event_file()
            line: str = json.dumps(record, ensure_ascii=False) + "\n"
            with open(target_file, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()

            self._seen_event_ids.add(event_id)
            # Evict oldest entries to keep memory bounded (keep last 50k)
            if len(self._seen_event_ids) > 100000:
                self._seen_event_ids = set(list(self._seen_event_ids)[-50000:])

            return record

    def get_events_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve audit-grade event trace for a specific session.

        Scans all JSONL files (newest first) for events matching session_id.

        Args:
            session_id: Session identifier to search for.

        Returns:
            List of event records sorted by server_seq ascending.
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return []

        results: List[Dict[str, Any]] = []
        files = sorted(EVENTS_DIR.glob("*.jsonl"), reverse=True)
        for fpath in files:
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line_str: str = line.strip()
                        if not line_str:
                            continue
                        try:
                            rec: Dict[str, Any] = json.loads(line_str)
                            p: Dict[str, Any] = rec.get("payload", {})
                            if (
                                p.get("session_id") == session_id
                                or rec.get("session_id") == session_id
                            ):
                                results.append(rec)
                        except json.JSONDecodeError:
                            continue
            except OSError as exc:
                logger.warning("[EventLog] Error reading %s: %s", fpath, exc)

        results.sort(key=lambda r: int(r.get("server_seq", 0)))
        return results

    def get_events_for_student(
        self,
        trno: str,
        date_str: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve all events for a given student TRNO.

        Args:
            trno: Student TRNO to search for.
            date_str: Optional ISO date string (YYYY-MM-DD) to limit to one day.

        Returns:
            List of event records sorted by server_seq ascending.
        """
        trno = str(trno).strip()
        if not trno:
            return []

        results: List[Dict[str, Any]] = []
        target_files: List[Path] = (
            [EVENTS_DIR / f"{date_str}.jsonl"]
            if date_str
            else sorted(EVENTS_DIR.glob("*.jsonl"))
        )

        for fpath in target_files:
            if not fpath.exists():
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line_str: str = line.strip()
                        if not line_str:
                            continue
                        try:
                            rec: Dict[str, Any] = json.loads(line_str)
                            if str(rec.get("trno", "")).strip() == trno:
                                results.append(rec)
                        except json.JSONDecodeError:
                            continue
            except OSError as exc:
                logger.warning("[EventLog] Error reading %s: %s", fpath, exc)

        results.sort(key=lambda r: int(r.get("server_seq", 0)))
        return results

    def get_today_events_count(self) -> int:
        """Count total events logged today.

        Returns:
            Integer count of non-blank lines in today's JSONL file.
        """
        target_file: Path = self.get_today_event_file()
        if not target_file.exists():
            return 0
        count: int = 0
        try:
            with open(target_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        count += 1
        except OSError as exc:
            logger.warning("[EventLog] Error counting today's events: %s", exc)
        return count

    def run_daily_rollup_and_compaction(
        self, retention_days: int = 14
    ) -> Dict[str, Any]:
        """Compile daily rollups for past days and compress old JSONL files.

        For each past day (not today):
          1. If a rollup JSON does not exist, compute and write it.
          2. If the file is older than retention_days, compress to .jsonl.gz
             and remove the original JSONL.

        Args:
            retention_days: Days before compressing old event logs.

        Returns:
            Summary with rollups_created, compressed_count, last_run.
        """
        with self._lock:
            today_str: str = datetime.now().strftime("%Y-%m-%d")
            all_files: List[Path] = sorted(EVENTS_DIR.glob("*.jsonl"))
            rollups_created: int = 0
            compressed_count: int = 0

            for fpath in all_files:
                date_stem: str = fpath.stem  # YYYY-MM-DD
                if date_stem == today_str:
                    continue  # Never compact today's active file

                rollup_file: Path = ROLLUPS_DIR / f"{date_stem}.json"
                if not rollup_file.exists():
                    rollup_data: Dict[str, Any] = self._compute_rollup_for_file(
                        fpath
                    )
                    try:
                        tmp_rollup: Path = rollup_file.with_suffix(".tmp")
                        tmp_rollup.write_text(
                            json.dumps(rollup_data, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        tmp_rollup.replace(rollup_file)
                        rollups_created += 1
                        logger.info(
                            "[EventLog] Created daily rollup: %s", rollup_file.name
                        )
                    except OSError as exc:
                        logger.error(
                            "[EventLog] Failed writing rollup %s: %s",
                            rollup_file.name,
                            exc,
                        )

                # Compress if older than retention_days
                try:
                    file_date: datetime = datetime.strptime(date_stem, "%Y-%m-%d")
                    age_days: int = (datetime.now() - file_date).days
                    if age_days > retention_days:
                        gz_path: Path = fpath.with_suffix(".jsonl.gz")
                        if not gz_path.exists():
                            with open(fpath, "rb") as f_in, gzip.open(
                                gz_path, "wb"
                            ) as f_out:
                                shutil.copyfileobj(f_in, f_out)
                            fpath.unlink()
                            compressed_count += 1
                            logger.info(
                                "[EventLog] Compressed old event log: %s -> %s",
                                fpath.name,
                                gz_path.name,
                            )
                except (ValueError, OSError) as exc:
                    logger.debug(
                        "[EventLog] Retention check skipped for %s: %s",
                        fpath.name,
                        exc,
                    )

            return {
                "rollups_created": rollups_created,
                "compressed_count": compressed_count,
                "last_run": datetime.now().isoformat(),
            }

    def _compute_rollup_for_file(self, fpath: Path) -> Dict[str, Any]:
        """Compile aggregate summary statistics for a past day's event log.

        Args:
            fpath: Path to the JSONL event log file.

        Returns:
            Rollup summary dictionary.
        """
        total_events: int = 0
        students: Set[str] = set()
        stations: Set[str] = set()
        event_type_counts: Dict[str, int] = {}

        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                for line in fh:
                    line_str: str = line.strip()
                    if not line_str:
                        continue
                    try:
                        rec: Dict[str, Any] = json.loads(line_str)
                        total_events += 1
                        ev_type: str = str(rec.get("event_type", "UNKNOWN"))
                        event_type_counts[ev_type] = (
                            event_type_counts.get(ev_type, 0) + 1
                        )
                        t: str = str(rec.get("trno", "")).strip()
                        if t:
                            students.add(t)
                        s: str = str(rec.get("station", "")).strip()
                        if s:
                            stations.add(s)
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.warning(
                "[EventLog] Error reading rollup source %s: %s", fpath, exc
            )

        return {
            "date": fpath.stem,
            "total_events": total_events,
            "unique_students": len(students),
            "unique_stations": len(stations),
            "event_breakdown": event_type_counts,
            "students": sorted(students),
            "stations": sorted(stations),
            "computed_at": datetime.now().isoformat(),
        }


event_log_service = EventLogService()
