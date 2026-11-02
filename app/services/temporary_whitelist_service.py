# app/services/temporary_whitelist_service.py
"""
temporary_whitelist_service.py — Teacher (Asateza) Temporary Academic Whitelist Pass System.
Allows teachers and administrators to issue time-limited exceptions for specific students,
Darajah cohorts, or workstations for legitimate research, competitions, and projects.

Stored in data/temporary_whitelists.json
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("temporary_whitelist_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
PASSES_FILE = DATA_DIR / "temporary_whitelists.json"


class TemporaryWhitelistService:
    _instance: Optional[TemporaryWhitelistService] = None
    _lock = threading.RLock()

    def __new__(cls) -> TemporaryWhitelistService:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()
        self._initialized = True

    def _load(self) -> List[Dict[str, Any]]:
        with self._lock:
            if not PASSES_FILE.exists():
                return []
            try:
                content = PASSES_FILE.read_text(encoding="utf-8").strip()
                if not content:
                    return []
                passes = json.loads(content)
                # Purge expired passes older than 24 hours
                now = time.time()
                active_and_recent = [p for p in passes if (p.get("expires_at", 0) > now - 86400)]
                if len(active_and_recent) != len(passes):
                    self._save(active_and_recent)
                return active_and_recent
            except Exception as e:
                logger.warning(f"[TempWhitelist] Could not read passes file: {e}")
                return []

    def _save(self, passes: List[Dict[str, Any]]) -> None:
        with self._lock:
            try:
                tmp = PASSES_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(passes, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(PASSES_FILE)
            except Exception as e:
                logger.error(f"[TempWhitelist] Failed to write passes file: {e}")

    def grant_pass(
        self,
        domain_or_url: str,
        scope_type: str = "all",  # 'all', 'student', 'darajah', 'station'
        scope_value: str = "",     # e.g. TRNO "26832", Darajah "7 A M", Station "M-24"
        duration_minutes: int = 120,
        reason: str = "Authorized academic project research",
        teacher_name: str = "Teacher / Administrator",
    ) -> Dict[str, Any]:
        """Grant a time-limited temporary academic pass."""
        with self._lock:
            target = str(domain_or_url or "").strip().lower()
            if "://" in target:
                parsed = urlparse(target)
                target_clean = parsed.hostname or target
            else:
                target_clean = target.split("/")[0].strip()

            now = time.time()
            duration_sec = max(15 * 60, int(duration_minutes) * 60)
            expires_at = now + duration_sec

            pass_record = {
                "id": str(uuid.uuid4())[:8],
                "target_domain": target_clean,
                "target_raw": domain_or_url.strip(),
                "scope_type": scope_type.lower().strip() or "all",
                "scope_value": str(scope_value or "").strip(),
                "reason": reason.strip() or "Academic Project",
                "granted_by": teacher_name.strip() or "Administrator",
                "created_at": now,
                "created_display": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"),
                "expires_at": expires_at,
                "expires_display": datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M"),
                "duration_minutes": duration_minutes,
                "active": True,
            }

            passes = self._load()
            passes.append(pass_record)
            self._save(passes)
            logger.info(f"[TempWhitelist] Granted pass {pass_record['id']} for '{target_clean}' ({scope_type}={scope_value}) by {teacher_name}")
            return pass_record

    def revoke_pass(self, pass_id: str) -> bool:
        """Revoke an active pass immediately."""
        with self._lock:
            passes = self._load()
            updated = []
            found = False
            for p in passes:
                if p.get("id") == pass_id:
                    p["active"] = False
                    p["expires_at"] = time.time() - 1
                    found = True
                updated.append(p)
            if found:
                self._save(updated)
                logger.info(f"[TempWhitelist] Revoked pass {pass_id}")
            return found

    def get_active_passes(self) -> List[Dict[str, Any]]:
        """Return all currently valid, unexpired passes."""
        with self._lock:
            passes = self._load()
            now = time.time()
            active = []
            for p in passes:
                if p.get("active", True) and p.get("expires_at", 0) > now:
                    remaining_mins = max(1, int((p["expires_at"] - now) / 60))
                    item = dict(p)
                    item["remaining_minutes"] = remaining_mins
                    active.append(item)
            return sorted(active, key=lambda x: x.get("expires_at", 0), reverse=True)

    def is_temporarily_allowed(
        self,
        url_or_host: str,
        trno: str = "",
        darajah: str = "",
        station: str = "",
    ) -> Tuple[bool, str]:
        """
        Check if an active temporary whitelist pass exists for this URL and context.
        Returns (is_allowed: bool, reason_or_message: str).
        """
        now = time.time()
        passes = self.get_active_passes()
        if not passes:
            return False, ""

        h = str(url_or_host or "").lower().strip()
        if "://" in h:
            parsed = urlparse(h)
            host = (parsed.hostname or "").lower()
        else:
            host = h.split("/")[0].strip().lower()

        trno_str = str(trno or "").strip().lower()
        dar_str = str(darajah or "").strip().lower()
        st_str = str(station or "").strip().lower()

        for p in passes:
            target = p.get("target_domain", "").lower()
            if not target:
                continue

            # Check domain match (exact or subdomain)
            match_domain = (host == target) or host.endswith("." + target) or (target in h)
            if not match_domain:
                continue

            # Check scope
            stype = p.get("scope_type", "all")
            sval = str(p.get("scope_value", "")).lower()

            matched_scope = False
            if stype == "all":
                matched_scope = True
            elif stype == "student" and (sval == trno_str):
                matched_scope = True
            elif stype == "darajah" and (sval in dar_str or dar_str in sval):
                matched_scope = True
            elif stype == "station" and (sval == st_str):
                matched_scope = True

            if matched_scope:
                remaining_m = p.get("remaining_minutes", 1)
                msg = f"Teacher Pass Granted: {p.get('reason')} (Authorized by {p.get('granted_by')} · {remaining_m}m remaining)"
                return True, msg

        return False, ""


temporary_whitelist_service = TemporaryWhitelistService()
