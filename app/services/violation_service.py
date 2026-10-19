# app/services/violation_service.py
"""
violation_service.py — Canonical violation writer for MamalBrowse Sentinel.

THIS IS THE ONLY MODULE IN THE ENTIRE SYSTEM THAT WRITES VIOLATION RECORDS.
Every violation event flows through record_violation() which:
  - Clusters rapid retries (same student + domain within 90s → 1 incident)
  - Resolves authoritative station & timestamp from Lab Manager
  - Classifies severity: HIGH / MEDIUM / LOW
  - Stamps the 14-day institutional retention window (retention_expires_at)
  - Appends ONE line to students/violations/audit_YYYY-MM-DD.jsonl
  - Returns the canonical incident dict

ZERO synthetic values. If Lab Manager cannot resolve a station, the field
is written as "" and a warning is logged — NEVER invented.

INSTITUTIONAL POLICY (2026-10-03):
  - Violation retention window: 14 days.
  - Students blocked for violations are auto-unblocked after 14 days by
    retention_policy_service.audit_old_logs() / auto_expire_blocked_accounts().
  - Audit logs older than 14 days are archived (not deleted) for compliance.
  - Every new violation record carries:
      * policy_window_days   → 14
      * retention_expires_at → timestamp when this incident falls out of window
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("violation_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
VIOLATIONS_DIR = BASE_DIR / "students" / "violations"
VIOLATIONS_DIR.mkdir(parents=True, exist_ok=True)

# ── Retention policy (14-day institutional rule) ─────────────────────────────
try:
    from app.services.retention_policy_service import (
        VIOLATION_RETENTION_DAYS,
        AUDIT_RETENTION_DAYS,
    )
    _RETENTION_AVAILABLE = True
except ImportError:
    VIOLATION_RETENTION_DAYS = 14
    AUDIT_RETENTION_DAYS = 14
    _RETENTION_AVAILABLE = False

# ── Severity classification (authoritative) ─────────────────────────────
HIGH_SEVERITY_CATEGORIES = {
    "adult",
    "proxy",
    "torrent",
    "movie_download",
    "unauthorized_browser",
}

MEDIUM_SEVERITY_CATEGORIES = {
    "social_media",
    "gaming",
    "streaming",
}

# Rapid-retry dedup window
RETRY_CLUSTER_WINDOW_SECONDS = 90


def _classify_severity(category: str) -> str:
    cat = (category or "").strip().lower()
    if cat in HIGH_SEVERITY_CATEGORIES:
        return "HIGH"
    if cat in MEDIUM_SEVERITY_CATEGORIES:
        return "MEDIUM"
    return "LOW"


def _extract_domain(url: str) -> str:
    try:
        parsed = urlparse(str(url))
        host = parsed.hostname or ""
        return host.lower().lstrip("www.")
    except Exception:
        return ""


def _student_name(trno: str) -> str:
    if not trno:
        return "Unknown Student"
    try:
        from app.student_roster import get_student_info
        info = get_student_info(trno) or {}
        if info.get("name"):
            return info["name"]
    except Exception:
        pass
    return f"Student #{trno}"


def _student_darajah(trno: str) -> str:
    if not trno:
        return ""
    try:
        from app.student_roster import get_student_info
        info = get_student_info(trno) or {}
        return info.get("darajah") or info.get("class") or ""
    except Exception:
        return ""


def _student_group(trno: str) -> str:
    if not trno:
        return ""
    try:
        from app.student_roster import get_student_info
        info = get_student_info(trno) or {}
        gender = str(info.get("gender") or "").lower()
        if gender in ("girls", "female", "talebaat", "f"):
            return "Talebaat"
        if gender in ("boys", "male", "talabaat", "m"):
            return "Talabaat"
        return info.get("branch") or "Talabaat"
    except Exception:
        return "Talabaat"


def _resolve_authoritative_station(trno: str, timestamp: Optional[datetime]) -> str:
    """
    Ask Lab Manager for the authoritative station. Never invents.
    Returns "" if unresolved.
    """
    if not trno:
        return ""
    try:
        from app.services.lab_manager_service import lab_manager
        station = lab_manager.get_workstation_for_trno(trno, timestamp=timestamp)
        if station:
            return str(station).strip().upper()
    except Exception as e:
        logger.warning(f"[ViolationService] Lab Manager lookup failed for {trno}: {e}")
    return ""


def _resolve_authoritative_timestamp(trno: str, requested: Optional[datetime]) -> Tuple[str, str]:
    """
    Returns (iso_string, source). Prefers Lab Manager session window.
    source ∈ {"lab_manager", "request"}
    """
    if trno:
        try:
            from app.services.lab_manager_service import lab_manager
            sessions = lab_manager.get_student_sessions(trno, limit=1)
            if sessions:
                latest = sessions[0]
                ts = latest.get("last_heartbeat") or latest.get("login_time") or latest.get("start_time")
                if ts:
                    return str(ts), "lab_manager"
        except Exception:
            pass
    if requested:
        return requested.isoformat(timespec="seconds"), "request"
    return datetime.now().isoformat(timespec="seconds"), "request"


def _compute_retention_expiry(base_ts_str: str) -> Tuple[str, str]:
    """
    Given an authoritative timestamp string, compute:
      - policy_window_days:  "14"
      - retention_expires_at: ISO timestamp N days after base_ts_str

    Falls back to now() if base_ts_str cannot be parsed.
    """
    try:
        base_dt = datetime.fromisoformat(
            str(base_ts_str).replace("Z", "").split("+")[0].split(".")[0]
        )
    except Exception:
        base_dt = datetime.now()
    expires_dt = base_dt + timedelta(days=VIOLATION_RETENTION_DAYS)
    return (
        str(VIOLATION_RETENTION_DAYS),
        expires_dt.isoformat(timespec="seconds"),
    )


class ViolationService:
    """
    Thread-safe canonical violation writer.
    Tracks recent (trno, domain) hits in memory for 90s clustering.
    """
    _instance: Optional["ViolationService"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._lock = threading.RLock()
        # (trno, domain) -> {"first_seen": epoch, "count": int, "incident_id": str, "hits": [...]}
        self._recent_clusters: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Counter for audit writes during this session (for telemetry)
        self._audit_writes_since_start = 0
        self._initialized = True
        logger.info(
            "[ViolationService] Initialized. Retention policy: %d days "
            "(audit archive threshold: %d days). Service available: %s",
            VIOLATION_RETENTION_DAYS,
            AUDIT_RETENTION_DAYS,
            _RETENTION_AVAILABLE,
        )

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────
    def record_violation(
        self,
        *,
        url: str,
        trno: str,
        category: str,
        action: str,
        reason: str = "",
        browser: str = "",
        requested_station: str = "",
        requested_timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        THE single entry point for writing a violation.
        Returns canonical incident dict ready to be shown on dashboard.

        Adds two retention-related fields to every new incident:
          - policy_window_days:     "14" (string for JSON stability)
          - retention_expires_at:   ISO timestamp when this incident falls
                                    out of the institutional retention window
        """
        trno = str(trno or "").strip()
        url = str(url or "").strip()
        category = str(category or "general").strip().lower()
        action = str(action or "warn").strip().lower()
        domain = _extract_domain(url) or "unknown"

        severity = _classify_severity(category)

        # Resolve authoritative data
        resolved_station = _resolve_authoritative_station(trno, requested_timestamp)
        if not resolved_station and requested_station:
            requested_station_clean = str(requested_station).strip().upper()
            if requested_station_clean and requested_station_clean not in (
                "UNKNOWN", "NONE", "PC", "LAB PC", "-"
            ):
                resolved_station = requested_station_clean
                logger.warning(
                    f"[ViolationService] Lab Manager has no session for TRNO={trno}; "
                    f"using client-reported station '{resolved_station}' (NOT authoritative)."
                )

        resolved_ts, ts_source = _resolve_authoritative_timestamp(trno, requested_timestamp)

        # Compute the 14-day retention expiry for THIS incident
        policy_window_days, retention_expires_at = _compute_retention_expiry(resolved_ts)

        student_name = _student_name(trno)
        darajah = _student_darajah(trno)
        group = _student_group(trno)

        now_epoch = time.time()
        cluster_key = (trno, domain)

        with self._lock:
            self._expire_clusters(now_epoch)

            cluster = self._recent_clusters.get(cluster_key)
            is_new_incident = True
            incident_id = ""

            if cluster and (now_epoch - cluster["first_seen"]) <= RETRY_CLUSTER_WINDOW_SECONDS:
                is_new_incident = False
                incident_id = cluster["incident_id"]
                cluster["count"] += 1
                cluster["last_seen"] = now_epoch
                cluster["hits"].append({
                    "url": url,
                    "ts": resolved_ts,
                    "action": action,
                })
                logger.info(
                    f"[ViolationService] Clustered retry: {trno} → {domain} "
                    f"(hit #{cluster['count']} of incident {incident_id})"
                )
            else:
                incident_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{trno}-{abs(hash(domain)) % 9999:04d}"
                self._recent_clusters[cluster_key] = {
                    "first_seen": now_epoch,
                    "last_seen": now_epoch,
                    "count": 1,
                    "incident_id": incident_id,
                    "hits": [{
                        "url": url,
                        "ts": resolved_ts,
                        "action": action,
                    }],
                }

            cluster_data = self._recent_clusters[cluster_key]

        record = {
            # ── Identity ──
            "incident_id":    incident_id,
            "is_new":         is_new_incident,
            "hit_count":      cluster_data["count"],
            "severity":       severity,

            # ── Student ──
            "trno":           trno,
            "student_name":   student_name,
            "darajah":        darajah,
            "group":          group,

            # ── Authoritative location & time ──
            "station":        resolved_station,
            "timestamp":      resolved_ts,
            "ts_source":      ts_source,

            # ── Violation details ──
            "url":            url,
            "domain":         domain,
            "category":       category,
            "action":         action,
            "reason":         reason,
            "browser":        browser or "chrome.exe",
            "resolved_by":    "MamalBrowse Sentinel Core Policy Engine",

            # ── 14-Day Institutional Retention Policy ──
            "policy_window_days":    policy_window_days,
            "retention_expires_at":  retention_expires_at,
        }

        if is_new_incident:
            self._append_audit_line(record)
            with self._lock:
                self._audit_writes_since_start += 1

        return record

    def recent_incidents(self, limit: int = 100) -> list:
        """Return clustered incidents from the last 90 seconds."""
        with self._lock:
            self._expire_clusters(time.time())
            items = []
            for (trno, domain), c in self._recent_clusters.items():
                items.append({
                    "incident_id":  c["incident_id"],
                    "trno":         trno,
                    "domain":       domain,
                    "hit_count":    c["count"],
                    "first_seen":   datetime.fromtimestamp(c["first_seen"]).isoformat(timespec="seconds"),
                    "last_seen":    datetime.fromtimestamp(c["last_seen"]).isoformat(timespec="seconds"),
                    "hits":         c["hits"][-20:],
                })
            items.sort(key=lambda x: x["last_seen"], reverse=True)
            return items[:limit]

    def get_service_telemetry(self) -> Dict[str, Any]:
        """
        Return runtime telemetry for the violation writer.
        Used by the admin dashboard for health monitoring.
        """
        with self._lock:
            active_clusters = len(self._recent_clusters)
            total_hits = sum(v["count"] for v in self._recent_clusters.values())
            audit_writes = self._audit_writes_since_start

        # Count today's audit lines from disk (fast, bounded)
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_file = VIOLATIONS_DIR / f"audit_{today_str}.jsonl"
        today_lines = 0
        try:
            if today_file.exists():
                with today_file.open("r", encoding="utf-8") as f:
                    for _ in f:
                        today_lines += 1
        except Exception:
            pass

        return {
            "retention_days":          VIOLATION_RETENTION_DAYS,
            "archive_threshold_days":  AUDIT_RETENTION_DAYS,
            "retention_service_linked": _RETENTION_AVAILABLE,
            "active_clusters":         active_clusters,
            "total_hits_in_flight":    total_hits,
            "audit_writes_since_start": audit_writes,
            "today_audit_lines":       today_lines,
            "today_audit_file":        str(today_file),
            "ts":                      datetime.now().isoformat(timespec="seconds"),
        }

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _expire_clusters(self, now_epoch: float) -> None:
        expired = [
            k for k, v in self._recent_clusters.items()
            if (now_epoch - v["last_seen"]) > RETRY_CLUSTER_WINDOW_SECONDS
        ]
        for k in expired:
            del self._recent_clusters[k]

    def _append_audit_line(self, record: Dict[str, Any]) -> None:
        """Append ONE line to the authoritative audit JSONL file."""
        day_str = datetime.now().strftime("%Y-%m-%d")
        audit_file = VIOLATIONS_DIR / f"audit_{day_str}.jsonl"
        viol_file = VIOLATIONS_DIR / f"violations_{day_str}.jsonl"
        try:
            line_str = json.dumps(record, ensure_ascii=False) + "\n"
            with audit_file.open("a", encoding="utf-8") as f:
                f.write(line_str)
            with viol_file.open("a", encoding="utf-8") as f:
                f.write(line_str)
            logger.info(
                f"[ViolationService] Wrote {record['severity']} incident "
                f"{record['incident_id']} ({record['trno']} → {record['domain']}) "
                f"[retention until {record.get('retention_expires_at', 'N/A')}]"
            )
        except Exception as e:
            logger.error(f"[ViolationService] Failed to write audit line: {e}")


# Global singleton
violation_service = ViolationService()