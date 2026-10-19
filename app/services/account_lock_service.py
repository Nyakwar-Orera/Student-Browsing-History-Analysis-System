# app/services/account_lock_service.py
"""
account_lock_service.py — Student Account Lock & Suspension Manager for MamalBrowse Sentinel.
Maintains persistent student account states (active, warned, blocked) in data/student_accounts.json.
Provides thread-safe operations to record violations, issue warnings, suspend accounts,
and restore access for teachers/administrators.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("account_lock_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_FILE = DATA_DIR / "student_accounts.json"
BLOCKED_TRNOS_FILE = DATA_DIR / "blocked_trnos.txt"

from app.services.lab_manager_service import lab_manager


class AccountLockService:
    _instance: Optional[AccountLockService] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> AccountLockService:
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, storage_path: Path = ACCOUNTS_FILE) -> None:
        if getattr(self, "_initialized", False):
            return
        self._storage_path = Path(storage_path)
        self._lock = threading.RLock()
        self._accounts: Dict[str, Dict[str, Any]] = {}
        self._load_from_disk()
        self._initialized = True

    def _load_from_disk(self) -> None:
        with self._lock:
            if not self._storage_path.exists():
                self._accounts = {}
            else:
                try:
                    content = self._storage_path.read_text(encoding="utf-8")
                    if content.strip():
                        self._accounts = json.loads(content)
                    else:
                        self._accounts = {}
                except Exception as e:
                    logger.warning(f"[AccountLock] Error loading {self._storage_path}: {e}")
                    self._accounts = {}

            # Filter out any test accounts (TEST999, non-numeric)
            self._accounts = {
                k: v for k, v in self._accounts.items()
                if str(k).strip().isdigit() and "test" not in str(k).lower()
            }

            # Sync persistent blocks from blocked_trnos.txt
            if BLOCKED_TRNOS_FILE.exists():
                try:
                    for line in BLOCKED_TRNOS_FILE.read_text(encoding="utf-8").splitlines():
                        s = line.strip()
                        if not s or not s.isdigit() or "test" in s.lower():
                            continue
                        if s not in self._accounts or self._accounts[s].get("status") != "blocked":
                            now_str = datetime.now().isoformat(timespec="seconds")
                            meta = self._lookup_roster_metadata(s)
                            self._accounts[s] = {
                                "trno": s,
                                "name": meta.get("name") or f"Student #{s}",
                                "darajah": meta.get("darajah") or "General",
                                "status": "blocked",
                                "warning_count": 3,
                                "blocked_at": now_str,
                                "last_incident": now_str,
                                "blocked_reason": "Account suspended under institutional policy.",
                                "violations": [],
                            }
                except Exception as e:
                    logger.warning(f"[AccountLock] Error syncing from {BLOCKED_TRNOS_FILE}: {e}")

    def _save_to_disk(self) -> None:
        with self._lock:
            try:
                self._storage_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_file = self._storage_path.with_suffix(".tmp")
                tmp_file.write_text(
                    json.dumps(self._accounts, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                tmp_file.replace(self._storage_path)
                self._export_blocked_trnos_file()
            except Exception as e:
                logger.error(f"[AccountLock] Failed saving accounts: {e}")

    def _export_blocked_trnos_file(self) -> None:
        """Export raw blocked TRNO list to plaintext file for Lab Manager integration."""
        try:
            blocked_trnos = [
                trno for trno, acc in self._accounts.items()
                if acc.get("status") == "blocked"
            ]
            BLOCKED_TRNOS_FILE.parent.mkdir(parents=True, exist_ok=True)
            BLOCKED_TRNOS_FILE.write_text(
                "\n".join(sorted(blocked_trnos)) + ("\n" if blocked_trnos else ""),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[AccountLock] Could not write blocked_trnos.txt: {e}")

    def _lookup_roster_metadata(self, trno: str) -> Dict[str, str]:
        """Attempt to fetch student's name and darajah from Koha roster and Lab Manager."""
        name = ""
        darajah = "Unknown"
        try:
            from app.student_roster import load_student_roster
            roster = load_student_roster() or {}
            profile = roster.get(str(trno)) or {}
            name = profile.get("name") or profile.get("StudentName") or ""
            darajah = profile.get("darajah") or profile.get("class") or "Unknown"
        except Exception:
            pass

        if not name:
            name = lab_manager.get_student_name(trno)

        return {
            "name": name or f"Student {trno}",
            "darajah": darajah,
        }

    def _reload_if_needed(self) -> None:
        """Reload accounts from disk if student_accounts.json or blocked_trnos.txt was modified or cache is empty."""
        try:
            mtime = self._storage_path.stat().st_mtime if self._storage_path.exists() else 0
            bmtime = BLOCKED_TRNOS_FILE.stat().st_mtime if BLOCKED_TRNOS_FILE.exists() else 0
            cur_tag = (mtime, bmtime)
            if getattr(self, "_last_disk_tag", None) != cur_tag or not self._accounts:
                self._load_from_disk()
                self._last_disk_tag = cur_tag
        except Exception as e:
            logger.debug(f"[AccountLock] Fresh reload skipped: {e}")

    def get_account(self, trno: str | int) -> Dict[str, Any]:
        """Get or initialize account record for a given student TRNO."""
        self._reload_if_needed()
        sid = str(trno).strip()
        with self._lock:
            if sid not in self._accounts:
                meta = self._lookup_roster_metadata(sid)
                self._accounts[sid] = {
                    "trno": sid,
                    "name": meta["name"],
                    "darajah": meta["darajah"],
                    "status": "active",        # active | warned | blocked
                    "warning_count": 0,
                    "blocked_at": None,
                    "last_incident": None,
                    "last_station": None,
                    "blocked_reason": "",
                    "unblocked_at": None,
                    "unblocked_by": None,
                    "violations": [],
                }
            return dict(self._accounts[sid])

    def is_account_blocked(self, trno: str | int) -> bool:
        """Fast check if a student account is currently suspended."""
        self._reload_if_needed()
        sid = str(trno).strip()
        if not sid:
            return False
        with self._lock:
            acc = self._accounts.get(sid)
            return bool(acc and acc.get("status") == "blocked")

    def record_violation(
        self,
        trno: str | int,
        url: str,
        host: str = "",
        category: str | None = None,
        action: str = "block",
        station: str | None = None,
        auto_block_category: bool = False,
        max_warnings: int = 1,
        timestamp: str | None = None,
        domain: str | None = None,
        browser_process: str | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Record a policy infraction on a student account.
        Stores the violation record with the EXACT fields from Lab Manager:
          station, timestamp, url, domain, category, browser_process
        Ensures 'Last Incident' reflects the LATEST real timestamp from Lab Manager.
        """
        sid = str(trno).strip()
        if not sid:
            return {"status": "active", "warning_count": 0}

        # 1. Resolve real timestamp from Lab Manager session if not explicitly provided
        real_ts = str(timestamp or "").strip()
        if not real_ts:
            sessions = lab_manager.get_student_sessions(sid, limit=1)
            if sessions:
                real_ts = str(sessions[0].get("last_heartbeat") or sessions[0].get("login_time") or sessions[0].get("start_time") or "").strip()
        if not real_ts:
            s_start = lab_manager.get_session_start(sid)
            if s_start:
                real_ts = s_start.isoformat(timespec="seconds")

        # 2. Resolve real workstation from Lab Manager (no guessing)
        real_station = ""
        if station and str(station).strip().upper() not in ("UNKNOWN", "NONE", "PC", "LAB PC", "-", ""):
            real_station = str(station).strip().upper()
        else:
            lm_station = lab_manager.get_workstation_for_trno(sid, timestamp=real_ts)
            real_station = lm_station or ""

        # 3. Clean domain and browser process
        clean_domain = domain or host
        if not clean_domain and url:
            try:
                parts = str(url).split("/")
                clean_domain = parts[2] if len(parts) > 2 else ""
                if clean_domain.startswith("www."):
                    clean_domain = clean_domain[4:]
            except Exception:
                clean_domain = ""

        real_browser = (
            browser_process
            or kwargs.get("browser")
            or kwargs.get("process")
            or "chrome.exe"
        )

        with self._lock:
            acc = self.get_account(sid)

            # Store the violation record with the EXACT fields from Lab Manager
            violation_entry = {
                "station": real_station,
                "timestamp": real_ts,
                "url": url,
                "domain": clean_domain,
                "category": category or "general",
                "browser_process": real_browser,
                # Backwards-compatible aliases
                "ts": real_ts,
                "host": clean_domain,
                "action": action,
            }

            # Keep the last 50 violations
            violations = acc.get("violations", [])
            violations.append(violation_entry)
            acc["violations"] = violations[-50:]

            acc["warning_count"] = acc.get("warning_count", 0) + 1
            acc["last_incident"] = real_ts
            acc["last_station"] = real_station

            should_block = False
            reason = ""

            if auto_block_category:
                should_block = True
                reason = f"Accessed restricted site ({category or clean_domain}) — Immediate account suspension policy."
            elif acc["warning_count"] > max_warnings:
                should_block = True
                reason = f"Exceeded allowed policy warnings ({acc['warning_count']} strikes on restricted sites)."
            else:
                acc["status"] = "warned"

            if should_block:
                acc["status"] = "blocked"
                acc["blocked_at"] = real_ts
                acc["blocked_reason"] = reason
                logger.warning(f"[AccountLock] Student TRNO {sid} ACCOUNT BLOCKED: {reason} at station '{real_station}' [{real_ts}]")

            self._accounts[sid] = acc
            self._save_to_disk()
            return dict(acc)

    def unblock_account(self, trno: str | int, admin_user: str = "Administrator", note: str = "") -> bool:
        """Restore student account to 'active' status and reset warning count."""
        sid = str(trno).strip()
        with self._lock:
            if sid not in self._accounts:
                return False
            acc = self._accounts[sid]
            now_str = datetime.now().isoformat(timespec="seconds")
            acc["status"] = "active"
            acc["warning_count"] = 0
            acc["unblocked_at"] = now_str
            acc["unblocked_by"] = f"{admin_user}{f' ({note})' if note else ''}"
            acc["blocked_reason"] = ""
            self._accounts[sid] = acc
            self._save_to_disk()
            logger.info(f"[AccountLock] Student TRNO {sid} UNBLOCKED by {admin_user}")
            return True

    def block_account_manually(
        self,
        trno: str | int,
        reason: str = "Manual suspension by administrator",
        admin_user: str = "Administrator",
        station: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Manually suspend a student's account using real Lab Manager timestamp and station."""
        sid = str(trno).strip()
        with self._lock:
            acc = self.get_account(sid)

            # Get real timestamp from Lab Manager
            real_ts = str(timestamp or "").strip()
            if not real_ts:
                sessions = lab_manager.get_student_sessions(sid, limit=1)
                if sessions:
                    real_ts = str(sessions[0].get("last_heartbeat") or sessions[0].get("login_time") or sessions[0].get("start_time") or "").strip()
            if not real_ts:
                s_start = lab_manager.get_session_start(sid)
                if s_start:
                    real_ts = s_start.isoformat(timespec="seconds")

            # Get real workstation from Lab Manager
            real_station = station or lab_manager.get_workstation_for_trno(sid, timestamp=real_ts) or ""

            acc["status"] = "blocked"
            acc["blocked_at"] = real_ts
            acc["last_incident"] = real_ts
            acc["last_station"] = real_station
            acc["blocked_reason"] = f"Manual lockout by {admin_user}: {reason}"

            self._accounts[sid] = acc
            self._save_to_disk()
            logger.info(f"[AccountLock] Student TRNO {sid} MANUALLY BLOCKED by {admin_user} at station '{real_station}' [{real_ts}]")
            return dict(acc)

    def get_all_accounts(self) -> Dict[str, Dict[str, Any]]:
        self._reload_if_needed()
        with self._lock:
            return {k: dict(v) for k, v in self._accounts.items()}

    def get_blocked_students(self) -> List[Dict[str, Any]]:
        """Return list of all currently suspended student accounts with location/station and real Lab Manager timestamp."""
        self._reload_if_needed()
        with self._lock:
            blocked = []
            for acc in self._accounts.values():
                if acc.get("status") == "blocked":
                    item = dict(acc)
                    sid = str(item.get("trno", "")).strip()

                    # 1. Authoritative station from Lab Manager
                    station = item.get("last_station")
                    if not station or station in ("UNKNOWN", "NONE", "PC", "LAB PC", "-"):
                        station = lab_manager.get_workstation_for_trno(sid) or ""
                    if not station and item.get("violations"):
                        station = item["violations"][-1].get("station") or ""

                    item["station"] = station or ""
                    item["last_station"] = item["station"]

                    # 2. Ensure last_incident reflects the LATEST real timestamp from Lab Manager
                    last_inc = item.get("last_incident")
                    if not last_inc and item.get("violations"):
                        last_inc = item["violations"][-1].get("timestamp") or item["violations"][-1].get("ts")
                    if not last_inc:
                        last_inc = item.get("blocked_at") or ""
                    item["last_incident"] = last_inc
                    item["blocked_at"] = item.get("blocked_at") or last_inc

                    blocked.append(item)

            # Sort newest incident / block first
            blocked.sort(key=lambda a: str(a.get("last_incident") or a.get("blocked_at") or ""), reverse=True)
            return blocked

    def get_warned_students(self) -> List[Dict[str, Any]]:
        """Return list of students currently on warning status."""
        self._reload_if_needed()
        with self._lock:
            warned = [
                dict(acc) for acc in self._accounts.values()
                if acc.get("status") == "warned"
            ]
            warned.sort(key=lambda a: a.get("warning_count", 0), reverse=True)
            return warned


# Global singleton instance
account_lock_service = AccountLockService()
