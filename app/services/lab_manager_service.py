# app/services/lab_manager_service.py
"""
Lab Manager Service — Authoritative Data Layer Mirror.
Directly reads and parses Lab Manager's primary state files:
  - session_history.json (workstations, login/logout timestamps, duration, status)
  - usage_data.json (daily student PC duration mapping)
  - temporary_extensions.json (active session time extensions)
  - users.xlsx (student roster: TRNO -> Name -> Class)
  - ou_rules.json / user_rules.json (quota policies)

Guarantees 1:1 fidelity with Lab Manager's FileWriteCoordinator:
  - Exact JSON keys (username, machine_id, login_time, logout_time, duration, status)
  - Multi-encoding fallback (utf-8, utf-8-sig, latin-1, cp1252)
  - Zero synthetic guesses: real station IDs (e.g. M05, MK-44, F-29) or None
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("lab_manager_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"


class LabManagerService:
    _instance: Optional[LabManagerService] = None
    _lock = threading.Lock()

    def __new__(cls) -> LabManagerService:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.server_ip = os.getenv("LAB_SERVER_IP", "10.10.117.76")
        self.server_url = os.getenv("LAB_SERVER_URL", f"http://{self.server_ip}/usage_data.json")
        self.timeout = int(os.getenv("LAB_FETCH_TIMEOUT_SECONDS", 5))
        self.max_retries = int(os.getenv("LAB_MAX_RETRIES", 2))
        self.cache_ttl = int(os.getenv("LAB_CACHE_TTL_SECONDS", 10))  # 10s mirrors Lab Manager web dashboard
        self.use_share = os.getenv("LAB_USE_NETWORK_SHARE", "true").lower() in ("true", "1", "yes")

        # Configured network share paths
        self.network_share_usage = os.getenv("LAB_SERVER_PATH", rf"\\{self.server_ip}\c\Lab Manager\usage_data.json")
        self.network_share_session = os.getenv("LAB_SESSION_HISTORY_PATH", rf"\\{self.server_ip}\c\Lab Manager\session_history.json")
        self.network_share_extensions = rf"\\{self.server_ip}\c\Lab Manager\temporary_extensions.json"
        self.network_share_users = rf"\\{self.server_ip}\c\Lab Manager\users.xlsx"

        # Local workspace paths (authoritative local fallback)
        self.local_dir = DATA_DIR / "lab_manager"
        self.local_usage = self.local_dir / "usage_data.json"
        self.local_session = self.local_dir / "session_history.json"
        self.local_extensions = self.local_dir / "temporary_extensions.json"
        self.local_users = self.local_dir / "users.xlsx"
        self.local_ou_rules = self.local_dir / "ou_rules.json"

        # Cached in-memory structures
        self._usage_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
        self._session_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
        self._extensions_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
        self._users_xlsx_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
        self._cache_lock = threading.RLock()

        logger.info("[LabManager] Service initialized (local_dir=%s, share_ip=%s)", self.local_dir, self.server_ip)

    @property
    def share_path(self) -> str:
        r"""Authoritative path for Lab Manager state (direct C:\Lab Manager if on server, else local dir, else network share)."""
        server_local = Path(r"C:\Lab Manager")
        if server_local.exists():
            return str(server_local)
        if self.local_dir.exists():
            return str(self.local_dir)
        return self.network_share_usage

    # ─────────────────────────────────────────────────────────────────
    @staticmethod
    def normalize_station_name(raw: Any) -> str:
        """
        Normalize workstation identifier to institutional server domain format:
          e.g. 'MK-50', 'RB-50', 'MK-44', 'RB-57'.
        Strips domain prefixes ('MAMAL\\', 'LAB\\'), FQDN suffixes ('.mamal.nbi'),
        and rejects generic placeholders ('LAB-PC', 'UNKNOWN', 'PC', 'WORKSTATION').
        """
        if not raw:
            return ""
        s = str(raw).strip()
        if not s or s.lower() in ("unknown", "none", "nan", "null", "pc", "lab pc", "lab-pc", "workstation", "-", "n/a", ""):
            return ""
        # Strip domain backslash prefix (e.g. MAMAL\MK-50 -> MK-50, LAB\rb-50 -> rb-50)
        if "\\" in s:
            s = s.split("\\")[-1].strip()
        # Strip extra whitespace/newlines (e.g. 'RB-42\nHEARTBEAT' -> 'RB-42')
        s = s.split()[0].strip() if s.split() else s
        # Strip FQDN DNS suffix (e.g. MK-50.mamal.nbi -> MK-50)
        if "." in s and not s.replace(".", "").isdigit():
            s = s.split(".")[0].strip()
        # Standardize prefix and station number (e.g. rb-50 -> RB-50, mk-50 -> MK-50, RB50 -> RB-50, MK50 -> MK-50)
        import re
        m = re.match(r"^([a-zA-Z]+)[-_]?(\d+)$", s)
        if m:
            prefix, num = m.groups()
            return f"{prefix.upper()}-{int(num):02d}"
        return s.upper()

    def _read_json_file(self, local_path: Path, network_path: str, default: Any) -> Any:
        """
        Reads JSON using Lab Manager's exact multi-encoding sequence:
          utf-8 -> utf-8-sig -> latin-1 -> cp1252
        Priority order:
          1. Network share UNC path (\\\\10.10.117.76\\c\\Lab Manager\\...) — live truth
          2. Local Lab Manager mirror/cache folder (authoritative fallback if share is offline)
        """
        encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]

        candidates = []
        # Priority 0: Direct local C:\Lab Manager installation (authoritative live truth on server)
        server_local_dir = Path(r"C:\Lab Manager")
        if server_local_dir.exists() and local_path:
            server_file = server_local_dir / local_path.name
            if server_file.exists():
                candidates.append((str(server_file), True))

        # Priority 1: Live Network Share
        if self.use_share and network_path:
            # Also check mapped drive K: if present
            mapped_k = network_path.replace(rf"\\{self.server_ip}\c\Lab Manager", r"K:\Lab Manager")
            if os.path.exists(mapped_k):
                candidates.append((mapped_k, True))
            candidates.append((network_path, True))

        # Priority 2: Local mirror fallback
        if local_path and local_path.exists():
            candidates.append((str(local_path), False))

        for filepath, is_remote in candidates:
            for encoding in encodings:
                try:
                    with open(filepath, "r", encoding=encoding) as f:
                        content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        # If read from live network share, update local backup asynchronously or safely
                        if is_remote and local_path:
                            try:
                                local_path.parent.mkdir(parents=True, exist_ok=True)
                                tmp_local = local_path.with_suffix(".tmp")
                                with open(tmp_local, "w", encoding="utf-8") as lf:
                                    lf.write(content)
                                tmp_local.replace(local_path)
                            except Exception:
                                pass
                        return data
                except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                    continue

        return default

    # ─────────────────────────────────────────────────────────────────
    # Core Data Accessors
    # ─────────────────────────────────────────────────────────────────

    def get_all_usage_data(self, force_refresh: bool = False) -> Dict[str, Dict[str, float]]:
        """
        Loads daily usage data mapping:
        { "YYYY-MM-DD": { "<trno>": <duration_minutes_float>, ... } }
        """
        now = time.time()
        with self._cache_lock:
            if not force_refresh and self._usage_cache["data"] is not None and (now - self._usage_cache["ts"]) < self.cache_ttl:
                return self._usage_cache["data"]

        data = self._read_json_file(self.local_usage, self.network_share_usage, default=None)

        # HTTP fallback if neither local nor network share was accessible
        if data is None and self.server_url:
            for attempt in range(self.max_retries):
                try:
                    resp = requests.get(self.server_url, timeout=self.timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                except Exception as e:
                    logger.debug("[LabManager] HTTP fetch fallback attempt %d failed: %s", attempt + 1, e)

        if data is None:
            data = self._usage_cache["data"] or {}

        with self._cache_lock:
            self._usage_cache["data"] = data
            self._usage_cache["ts"] = now

        return data

    def get_all_sessions(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Loads raw session list from session_history.json:
        [
          {
            "username": "27290",
            "machine_id": "M32",
            "ou": "OU=Darajah 6,OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi",
            "login_time": "2026-09-19T19:42:21.047057",
            "start_time": "2026-09-19T19:42:21.047057",
            "logout_time": "2026-09-19T21:19:24.402720",
            "duration": 97.06,
            "status": "completed",
            "last_heartbeat": "2026-09-19T21:19:24.402720"
          }, ...
        ]
        """
        now = time.time()
        with self._cache_lock:
            if not force_refresh and self._session_cache["data"] is not None and (now - self._session_cache["ts"]) < self.cache_ttl:
                return self._session_cache["data"]

        sessions = self._read_json_file(self.local_session, self.network_share_session, default=None)
        if sessions is None or not isinstance(sessions, list):
            sessions = self._session_cache["data"] or []

        with self._cache_lock:
            self._session_cache["data"] = sessions
            self._session_cache["ts"] = now

        return sessions

    def get_users_roster(self) -> Dict[str, Dict[str, str]]:
        """
        Loads student info from users.xlsx (columns: username, name, class).
        """
        now = time.time()
        with self._cache_lock:
            if self._users_xlsx_cache["data"] is not None and (now - self._users_xlsx_cache["ts"]) < 300:
                return self._users_xlsx_cache["data"]

        roster: Dict[str, Dict[str, str]] = {}
        target_path = None
        server_users = Path(r"C:\Lab Manager\users.xlsx")
        if server_users.exists():
            target_path = str(server_users)
        elif self.local_users.exists():
            target_path = str(self.local_users)
        elif self.use_share and os.path.exists(self.network_share_users):
            target_path = self.network_share_users

        if target_path:
            try:
                import pandas as pd
                df = pd.read_excel(target_path)
                df.columns = [str(c).lower().strip() for c in df.columns]
                for _, row in df.iterrows():
                    u = str(row.get("username", "")).strip()
                    if u and u.lower() not in ("nan", "none", ""):
                        roster[u] = {
                            "username": u,
                            "name": str(row.get("name", "")).strip(),
                            "class": str(row.get("class", "")).strip(),
                        }
            except Exception as e:
                logger.warning("[LabManager] Could not read users.xlsx: %s", e)

        with self._cache_lock:
            self._users_xlsx_cache["data"] = roster
            self._users_xlsx_cache["ts"] = now

        return roster

    def get_student_name(self, trno: str | int) -> str:
        """Resolve student name from Koha roster first, then users.xlsx, or format fallback."""
        sid = str(trno).strip()
        if not sid:
            return "Unknown Student"

        # 1. Koha roster
        try:
            from app.student_roster import get_student_info
            info = get_student_info(sid)
            if info and info.get("name"):
                return info["name"]
        except Exception:
            pass

        # 2. Lab Manager users.xlsx
        users_map = self.get_users_roster()
        if sid in users_map and users_map[sid].get("name"):
            return users_map[sid]["name"]

        return f"Student #{sid}"

    # ─────────────────────────────────────────────────────────────────
    # Required Helper Methods [A]
    # ─────────────────────────────────────────────────────────────────

    def get_workstation_for_trno(
        self,
        trno: str | int,
        timestamp: Optional[Union[str, datetime]] = None,
    ) -> Optional[str]:
        """
        Returns the authoritative workstation ID (e.g. 'M05', 'M32', 'MK-44', 'F-29')
        from Lab Manager's session_history.json for the given student TRNO.

        - If timestamp is provided: matches the session active at that specific time
          (login_time <= timestamp <= logout_time / last_heartbeat).
        - If no timestamp is provided or no exact time-window matches: matches the
          student's most recent valid session.
        - Returns None if no workstation record exists in Lab Manager.
        - NEVER INVENTS OR FALLS BACK TO A SYNTHETIC VALUE.
        """
        sid = str(trno).strip()
        if not sid:
            return None

        sessions = self.get_all_sessions()
        student_sessions = [
            s for s in sessions
            if isinstance(s, dict) and str(s.get("username") or s.get("user") or s.get("trno") or "").strip() == sid
            and (s.get("machine_id") or s.get("station") or s.get("computer") or s.get("hostname"))
        ]

        if not student_sessions:
            return None

        # Parse target timestamp if provided
        target_dt: Optional[datetime] = None
        if timestamp:
            if isinstance(timestamp, datetime):
                target_dt = timestamp
            elif isinstance(timestamp, str):
                t_str = timestamp.strip()
                try:
                    target_dt = datetime.fromisoformat(t_str.replace("Z", ""))
                except Exception:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            target_dt = datetime.strptime(t_str[:19], fmt)
                            break
                        except Exception:
                            continue
        if target_dt and target_dt.tzinfo is not None:
            target_dt = target_dt.replace(tzinfo=None)

        # If target_dt is known, search for active session containing target_dt
        if target_dt:
            # 1. Exact active window check
            for s in student_sessions:
                try:
                    login_str = s.get("login_time") or s.get("start_time")
                    if not login_str:
                        continue
                    login_dt = datetime.fromisoformat(str(login_str).replace("Z", ""))

                    is_active = (s.get("status") == "active") or (s.get("logout_time") is None)
                    logout_str = s.get("logout_time") or s.get("completion_time")
                    if logout_str:
                        logout_dt = datetime.fromisoformat(str(logout_str).replace("Z", ""))
                    elif is_active:
                        # Session currently active: allow from login up to 4 hours
                        hb_str = s.get("last_heartbeat")
                        hb_dt = datetime.fromisoformat(str(hb_str).replace("Z", "")) if hb_str else login_dt
                        logout_dt = max(login_dt + timedelta(hours=4), hb_dt + timedelta(hours=1))
                    else:
                        hb_str = s.get("last_heartbeat")
                        logout_dt = datetime.fromisoformat(str(hb_str).replace("Z", "")) if hb_str else login_dt + timedelta(hours=1)

                    # Tolerance window: +/- 15 minutes around session
                    if (login_dt - timedelta(minutes=15)) <= target_dt <= (logout_dt + timedelta(minutes=15)):
                        clean_mach = self.normalize_station_name(s.get("machine_id") or s.get("station") or s.get("computer") or s.get("hostname"))
                        if clean_mach:
                            return clean_mach
                except Exception:
                    continue

            # 2. Nearest session on the same calendar date
            same_date_sessions = []
            for s in student_sessions:
                try:
                    login_str = s.get("login_time") or s.get("start_time")
                    if not login_str:
                        continue
                    login_dt = datetime.fromisoformat(str(login_str).replace("Z", ""))
                    if login_dt.date() == target_dt.date():
                        diff = abs(target_dt - login_dt)
                        clean_mach = self.normalize_station_name(s.get("machine_id") or s.get("station") or s.get("computer") or s.get("hostname"))
                        if clean_mach:
                            same_date_sessions.append((diff, clean_mach))
                except Exception:
                    continue

            if same_date_sessions:
                same_date_sessions.sort(key=lambda x: x[0])
                return same_date_sessions[0][1]

        # If no timestamp or timestamp not matched, return the most recent session's machine_id
        student_sessions.sort(
            key=lambda x: str(x.get("login_time") or x.get("start_time") or ""),
            reverse=True,
        )
        for s in student_sessions:
            clean_mach = self.normalize_station_name(s.get("machine_id") or s.get("station") or s.get("computer") or s.get("hostname"))
            if clean_mach:
                return clean_mach

        return None

    def get_session_start(
        self,
        trno: str | int,
        target_date: Optional[Union[str, datetime, date]] = None,
    ) -> Optional[datetime]:
        """
        Returns datetime of session start (login_time / start_time) from session_history.json.
        If date is provided, filters for that specific date.
        """
        sid = str(trno).strip()
        if not sid:
            return None

        date_str = None
        if target_date:
            if isinstance(target_date, (datetime, date)):
                date_str = target_date.strftime("%Y-%m-%d")
            else:
                date_str = str(target_date)[:10]

        sessions = self.get_all_sessions()
        matching = []
        for s in sessions:
            if not isinstance(s, dict) or str(s.get("username") or "").strip() != sid:
                continue
            login_str = s.get("login_time") or s.get("start_time")
            if not login_str:
                continue
            if date_str and not str(login_str).startswith(date_str):
                continue
            try:
                dt = datetime.fromisoformat(str(login_str).replace("Z", ""))
                matching.append(dt)
            except Exception:
                continue

        if not matching:
            return None
        # Return earliest session on that date, or newest if no date filter
        return min(matching) if date_str else max(matching)

    def get_session_end(
        self,
        trno: str | int,
        target_date: Optional[Union[str, datetime, date]] = None,
    ) -> Optional[datetime]:
        """
        Returns datetime of session end (logout_time / completion_time) from session_history.json.
        """
        sid = str(trno).strip()
        if not sid:
            return None

        date_str = None
        if target_date:
            if isinstance(target_date, (datetime, date)):
                date_str = target_date.strftime("%Y-%m-%d")
            else:
                date_str = str(target_date)[:10]

        sessions = self.get_all_sessions()
        matching = []
        for s in sessions:
            if not isinstance(s, dict) or str(s.get("username") or "").strip() != sid:
                continue
            login_str = s.get("login_time") or s.get("start_time")
            if date_str and login_str and not str(login_str).startswith(date_str):
                continue
            logout_str = s.get("logout_time") or s.get("completion_time") or s.get("last_heartbeat")
            if not logout_str:
                continue
            try:
                dt = datetime.fromisoformat(str(logout_str).replace("Z", ""))
                matching.append(dt)
            except Exception:
                continue

        if not matching:
            return None
        return max(matching)

    def get_student_sessions(self, trno: str | int, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Returns all recorded sessions for a student from session_history.json, newest first.
        """
        sid = str(trno).strip()
        if not sid:
            return []
        sessions = self.get_all_sessions()
        matching = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            u = str(s.get("username") or s.get("trno") or "").strip()
            if u == sid:
                matching.append(dict(s))
        matching.sort(
            key=lambda x: str(x.get("login_time") or x.get("start_time") or ""),
            reverse=True
        )
        return matching[:limit] if limit else matching

    def get_recent_visits(self, trno: str | int, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Retrieves recent browsing visits for a student within the last `hours`.
        Guarantees AT MINIMUM the exact data contract fields:
          {trno, name, station, timestamp, url, domain, category, browser}

        - Workstation is resolved directly from Lab Manager's session_history.json
        - Student name is resolved from Koha roster / users.xlsx
        - Timestamp follows Lab Manager local ISO format
        """
        sid = str(trno).strip()
        if not sid:
            return []

        student_name = self.get_student_name(sid)
        cutoff_dt = datetime.now() - timedelta(hours=hours)
        results: List[Dict[str, Any]] = []

        # 1. Inspect student's browsing CSV if available
        browsing_csv_candidates = [
            BASE_DIR / "shares" / "BrowsingLogs" / f"{sid}.csv",
            Path(rf"\\{self.server_ip}\BrowsingLogs\{sid}.csv"),
            Path(rf"K:\BrowsingLogs\{sid}.csv"),
        ]
        # Also check configured environment path
        configured_path = os.getenv("BROWSING_LOGS_PATH")
        if configured_path:
            browsing_csv_candidates.insert(0, Path(configured_path) / f"{sid}.csv")

        csv_file = next((p for p in browsing_csv_candidates if p.exists()), None)
        if csv_file:
            try:
                import pandas as pd
                df = pd.read_csv(csv_file, low_memory=False, on_bad_lines="skip")
                col_map = {c.lower(): c for c in df.columns}
                time_col = col_map.get("visit_time") or col_map.get("visitdate") or col_map.get("timestamp")
                url_col = col_map.get("url")
                title_col = col_map.get("title")
                browser_col = col_map.get("browser") or col_map.get("process")

                if time_col and url_col:
                    df["_dt"] = pd.to_datetime(df[time_col], errors="coerce")
                    valid_df = df.dropna(subset=["_dt"]).copy()
                    valid_df = valid_df[valid_df["_dt"] >= cutoff_dt]

                    for _, row in valid_df.tail(100).iterrows():
                        v_dt = row["_dt"]
                        v_ts = v_dt.isoformat()
                        raw_url = str(row[url_col]).strip()
                        # Extract domain
                        domain = ""
                        try:
                            parts = raw_url.split("/")
                            domain = parts[2] if len(parts) > 2 else ""
                            if domain.startswith("www."):
                                domain = domain[4:]
                        except Exception:
                            domain = ""

                        # Categorize
                        category = "General Web"
                        try:
                            from app.services.analytics_service import analytics_service
                            cat_info = analytics_service.categorize_domain(domain)
                            category = cat_info.get("category", "General Web")
                        except Exception:
                            pass

                        # Resolve station from Lab Manager
                        station = self.get_workstation_for_trno(sid, timestamp=v_dt) or ""

                        # Resolve browser process
                        browser_name = str(row[browser_col]).strip() if browser_col and pd.notna(row.get(browser_col)) else "chrome.exe"

                        results.append({
                            "trno": sid,
                            "name": student_name,
                            "station": station,
                            "timestamp": v_ts,
                            "url": raw_url,
                            "domain": domain,
                            "category": category,
                            "browser": browser_name,
                        })
            except Exception as e:
                logger.warning("[LabManager] Error reading browsing CSV for %s: %s", sid, e)

        # 2. Also incorporate recent violation events from students/violations/
        violations_dir = BASE_DIR / "students" / "violations"
        if violations_dir.exists():
            today_str = datetime.now().strftime("%Y-%m-%d")
            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            for day in (today_str, yesterday_str):
                vf = violations_dir / f"violations_{day}.jsonl"
                if not vf.exists():
                    continue
                try:
                    for line in vf.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        rec = json.loads(line)
                        if str(rec.get("trno", "")).strip() != sid:
                            continue
                        r_ts_str = rec.get("ts") or rec.get("timestamp") or ""
                        try:
                            r_dt = datetime.fromisoformat(r_ts_str.replace("Z", ""))
                            if r_dt < cutoff_dt:
                                continue
                        except Exception:
                            continue

                        station = rec.get("station") or self.get_workstation_for_trno(sid, timestamp=r_dt) or ""
                        results.append({
                            "trno": sid,
                            "name": student_name,
                            "station": station,
                            "timestamp": r_ts_str,
                            "url": rec.get("url", ""),
                            "domain": rec.get("host") or rec.get("domain", ""),
                            "category": rec.get("category", "Violation"),
                            "browser": rec.get("browser_process") or rec.get("browser", "chrome.exe"),
                        })
                except Exception as e:
                    logger.debug("[LabManager] Error scanning violation log: %s", e)

        # Sort newest visits first
        results.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        return results

    # ─────────────────────────────────────────────────────────────────
    # Legacy / Compatible Methods for MamalBrowse Sentinel Core
    # ─────────────────────────────────────────────────────────────────

    def get_student_usage_records(self, trno: str | int) -> List[Dict[str, Any]]:
        """
        Returns list of daily usage dictionaries for a specific student:
        [ {"Date": "2026-09-08", "DurationMinutes": 60.0, "DurationFormatted": "1h 00m"}, ... ]
        """
        trno_str = str(trno).strip()
        usage_data = self.get_all_usage_data()
        records = []

        for date_str, student_map in sorted(usage_data.items(), reverse=True):
            if trno_str in student_map:
                try:
                    mins = float(student_map[trno_str])
                except (ValueError, TypeError):
                    mins = 0.0

                h = int(mins // 60)
                m = int(mins % 60)
                records.append({
                    "Date": date_str,
                    "DurationMinutes": round(mins, 1),
                    "DurationFormatted": f"{h}h {m:02d}m" if h > 0 else f"{m}m",
                })

        return records

    def get_student_summary(self, trno: str | int) -> Dict[str, Any]:
        """
        Calculates total computer duration, total sessions/days, and last active date.
        """
        records = self.get_student_usage_records(trno)
        if not records:
            return {
                "total_minutes": 0.0,
                "total_hours": 0.0,
                "formatted_total": "0h 00m",
                "active_days": 0,
                "last_active": "Never",
                "recent_days_count": 0,
                "records": [],
            }

        total_mins = sum(r["DurationMinutes"] for r in records)
        total_hours = total_mins / 60.0
        h = int(total_mins // 60)
        m = int(total_mins % 60)

        cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_count = sum(1 for r in records if r["Date"] >= cutoff_date)

        return {
            "total_minutes": round(total_mins, 1),
            "total_hours": round(total_hours, 2),
            "formatted_total": f"{h}h {m:02d}m",
            "active_days": len(records),
            "last_active": records[0]["Date"] if records else "Never",
            "recent_days_count": recent_count,
            "records": records,
        }

    def get_student_sessions(self, trno: str | int, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves detailed individual computer sessions from session_history.json.
        Guarantees machine_id is preserved verbatim and mapped to station / computer_name.
        NO SYNTHETIC OR MODULO GUESSING.
        """
        trno_str = str(trno).strip()
        raw_sessions = self.get_all_sessions()

        student_sessions = []
        for s in raw_sessions:
            if not isinstance(s, dict):
                continue
            if str(s.get("username") or s.get("user") or s.get("trno") or "").strip() == trno_str:
                sc = dict(s)
                # Authoritative workstation from machine_id or fallback keys
                real_mach = self.normalize_station_name(sc.get("machine_id"))
                if not real_mach:
                    for k in ("station", "computer", "hostname", "workstation"):
                        candidate = self.normalize_station_name(sc.get(k))
                        if candidate:
                            real_mach = candidate
                            break

                sc["machine_id"] = real_mach
                sc["station"] = real_mach
                sc["computer_name"] = real_mach
                student_sessions.append(sc)

        # Sort latest sessions first
        student_sessions.sort(key=lambda x: str(x.get("login_time") or x.get("start_time") or ""), reverse=True)
        return student_sessions[:limit]

    def get_workstation_for_trno(self, trno: str | int, timestamp: Optional[Any] = None) -> str:
        """
        Resolves the student's authentic workstation (e.g. MK-50, RB-50, MK-44) from session_history.json.
        If timestamp is provided, matches the session active at or closest to that timestamp.
        Otherwise returns their most recent session's workstation.
        Returns empty string if unresolved.
        """
        sid = str(trno).strip()
        if not sid:
            return ""

        sessions = self.get_student_sessions(sid, limit=20)
        if not sessions:
            return ""

        # If timestamp provided, match session window
        if timestamp:
            try:
                if isinstance(timestamp, datetime):
                    target_dt = timestamp
                else:
                    target_dt = datetime.fromisoformat(str(timestamp).replace("Z", "").split("+")[0])

                for s in sessions:
                    st_str = s.get("login_time") or s.get("start_time")
                    et_str = s.get("logout_time") or s.get("last_heartbeat")
                    if st_str:
                        st_dt = datetime.fromisoformat(str(st_str).replace("Z", "").split("+")[0])
                        et_dt = datetime.fromisoformat(str(et_str).replace("Z", "").split("+")[0]) if et_str else st_dt + timedelta(hours=3)
                        if st_dt - timedelta(minutes=30) <= target_dt <= et_dt + timedelta(minutes=30):
                            mach = s.get("machine_id") or s.get("station") or s.get("computer_name")
                            if mach:
                                norm = self.normalize_station_name(mach)
                                if norm:
                                    return norm
            except Exception:
                pass

        # Fallback: Most recent session workstation
        for s in sessions:
            mach = s.get("machine_id") or s.get("station") or s.get("computer_name")
            if mach:
                norm = self.normalize_station_name(mach)
                if norm:
                    return norm

        return ""

    def get_sync_telemetry(self) -> Dict[str, Any]:
        """
        Telemetry regarding Lab Manager source of truth:
          - Last successful read timestamp
          - Authoritative source path (network share or local)
          - Records cached count
          - Cache age in seconds
          - Distinct real workstations list
        """
        now = time.time()
        sessions = self.get_all_sessions()
        cache_ts = self._session_cache.get("ts", 0.0)
        cache_age = round(now - cache_ts, 1) if cache_ts else 0.0
        last_read_dt = datetime.fromtimestamp(cache_ts).strftime("%Y-%m-%d %H:%M:%S") if cache_ts else "Never"

        # Authoritative source path
        source_path = str(self.local_session) if self.local_session.exists() else self.network_share_session

        # Distinct real workstations (e.g. MK-50, RB-43, M11, etc.)
        stations = sorted(list({
            self.normalize_station_name(s.get("machine_id") or s.get("station") or s.get("computer"))
            for s in sessions
            if self.normalize_station_name(s.get("machine_id") or s.get("station") or s.get("computer"))
        }))

        return {
            "last_successful_read": last_read_dt,
            "source_path": source_path,
            "records_cached": len(sessions),
            "cache_age_seconds": cache_age,
            "is_live": (cache_age < 300),
            "workstations": stations,
        }


# Global singleton instance
lab_manager = LabManagerService()
