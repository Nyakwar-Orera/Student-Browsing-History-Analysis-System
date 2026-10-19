# app/services/policy_engine.py
"""
policy_engine.py — Website access policy & student account enforcement engine for MamalBrowse Sentinel.
Supports category blocklists, per-Darajah rules, time windows, per-student whitelist/overrides,
and automated student account suspension upon visiting restricted sites (e.g., Instagram).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import yaml

from app.services.account_lock_service import account_lock_service
from app.services.lab_manager_service import lab_manager

logger = logging.getLogger("policy_engine")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
POLICY_FILE = BASE_DIR / "config" / "access_policy.yaml"
RULES_CACHE_TTL = 30  # seconds


# ─────────────────────────────────────────────────────────────────
# Data Model
# ─────────────────────────────────────────────────────────────────

@dataclass
class CategoryRule:
    name: str
    domains: List[str]
    action: str = "block"          # block | warn | allow
    message: str = ""
    allow_override: bool = False   # can student request teacher override?


@dataclass
class TimeWindow:
    """Study hours window where specified categories are restricted."""
    days: List[str]                 # ["mon", "tue", ...]
    start: str                      # "08:00"
    end: str                        # "14:30"
    blocked_categories: List[str]   # ["social_media", "streaming"]


@dataclass
class AccountLockConfig:
    enabled: bool = True
    auto_block_categories: List[str] = field(default_factory=lambda: ["social_media", "gaming", "adult"])
    max_warnings_before_block: int = 1


@dataclass
class Policy:
    default_action: str = "allow"
    account_lock: AccountLockConfig = field(default_factory=AccountLockConfig)
    categories: Dict[str, CategoryRule] = field(default_factory=dict)
    whitelist_domains: Set[str] = field(default_factory=set)
    blocklist_domains: Set[str] = field(default_factory=set)
    time_windows: List[TimeWindow] = field(default_factory=list)
    per_darajah_extra_block: Dict[str, List[str]] = field(default_factory=dict)
    per_student_whitelist: Dict[str, List[str]] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────
# Policy Engine
# ─────────────────────────────────────────────────────────────────

class PolicyEngine:
    def __init__(self, policy_path: Path = POLICY_FILE):
        self._path = Path(policy_path)
        self._policy = Policy()
        self._loaded_at = 0.0
        self._lock = threading.RLock()
        self._domain_index: Dict[str, CategoryRule] = {}
        self.reload(force=True)

    # ---------- loading & parsing ----------

    def reload(self, force: bool = False) -> None:
        with self._lock:
            if not force and (time.time() - self._loaded_at < RULES_CACHE_TTL):
                return
            if not self._path.exists():
                self._bootstrap_default()
                self._loaded_at = time.time()
                return
            try:
                raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
                self._policy = self._parse(raw)
                self._build_index()
                self._loaded_at = time.time()
            except Exception as e:
                logger.error(f"[PolicyEngine] Failed to load policy file: {e}")

    def _bootstrap_default(self) -> None:
        """Write default policy file if none exists."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        default_yaml = {
            "default_action": "allow",
            "account_lock": {
                "enabled": True,
                "auto_block_categories": ["social_media", "gaming", "adult"],
                "max_warnings_before_block": 1,
            },
            "categories": {
                "social_media": {
                    "action": "block",
                    "message": "Social media sites like Instagram are prohibited during lab hours. Your account has been flagged.",
                    "allow_override": True,
                    "domains": [
                        "instagram.com", "www.instagram.com", "facebook.com",
                        "www.facebook.com", "m.facebook.com", "tiktok.com",
                        "www.tiktok.com", "snapchat.com", "twitter.com",
                        "x.com", "reddit.com", "www.reddit.com", "threads.net",
                    ],
                },
                "streaming": {
                    "action": "block",
                    "message": "Video streaming is restricted during academic sessions.",
                    "allow_override": True,
                    "domains": [
                        "youtube.com", "www.youtube.com", "m.youtube.com",
                        "netflix.com", "twitch.tv", "hotstar.com", "primevideo.com",
                    ],
                },
                "gaming": {
                    "action": "block",
                    "message": "Online gaming platforms are restricted on lab workstations.",
                    "allow_override": False,
                    "domains": [
                        "roblox.com", "www.roblox.com", "miniclip.com",
                        "poki.com", "crazygames.com", "krunker.io",
                    ],
                },
                "adult": {
                    "action": "block",
                    "message": "Access prohibited under institutional policy.",
                    "allow_override": False,
                    "domains": [],
                },
                "educational": {
                    "action": "allow",
                    "message": "Educational resource permitted.",
                    "allow_override": False,
                    "domains": [
                        "khanacademy.org", "coursera.org", "wikipedia.org",
                        "en.wikipedia.org", "arxiv.org", "jstor.org",
                        "ncert.nic.in", "nptel.ac.in", "stackoverflow.com",
                    ],
                },
            },
            "whitelist_domains": [
                "koha.ajsn.local", "10.10.117.76", "152.55.221.78",
                "localhost", "127.0.0.1",
            ],
            "blocklist_domains": [],
            "time_windows": [
                {
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
                    "start": "08:00",
                    "end": "14:30",
                    "blocked_categories": ["social_media", "streaming", "gaming"],
                }
            ],
            "per_darajah_extra_block": {
                "1": ["whatsapp.com", "web.whatsapp.com"],
                "2": ["whatsapp.com", "web.whatsapp.com"],
            },
            "per_student_whitelist": {},
        }
        try:
            self._path.write_text(yaml.safe_dump(default_yaml, sort_keys=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[PolicyEngine] Could not bootstrap default YAML: {e}")
        self._policy = self._parse(default_yaml)
        self._build_index()

    def _parse(self, raw: dict) -> Policy:
        cats: Dict[str, CategoryRule] = {}
        for name, cfg in (raw.get("categories") or {}).items():
            cats[name] = CategoryRule(
                name=name,
                domains=[str(d).lower().strip() for d in (cfg.get("domains") or [])],
                action=cfg.get("action", "block"),
                message=cfg.get("message", ""),
                allow_override=bool(cfg.get("allow_override", False)),
            )

        windows = [
            TimeWindow(
                days=[str(d).lower().strip() for d in w.get("days", [])],
                start=w.get("start", "00:00"),
                end=w.get("end", "23:59"),
                blocked_categories=w.get("blocked_categories", []),
            )
            for w in (raw.get("time_windows") or [])
        ]

        al_raw = raw.get("account_lock") or {}
        account_lock_cfg = AccountLockConfig(
            enabled=bool(al_raw.get("enabled", True)),
            auto_block_categories=[c.lower().strip() for c in al_raw.get("auto_block_categories", ["social_media", "gaming", "adult"])],
            max_warnings_before_block=int(al_raw.get("max_warnings_before_block", 1)),
        )

        return Policy(
            default_action=raw.get("default_action", "allow"),
            account_lock=account_lock_cfg,
            categories=cats,
            whitelist_domains={str(d).lower().strip() for d in (raw.get("whitelist_domains") or [])},
            blocklist_domains={str(d).lower().strip() for d in (raw.get("blocklist_domains") or [])},
            time_windows=windows,
            per_darajah_extra_block={
                str(k): [str(d).lower().strip() for d in v]
                for k, v in (raw.get("per_darajah_extra_block") or {}).items()
            },
            per_student_whitelist={
                str(k): [str(d).lower().strip() for d in v]
                for k, v in (raw.get("per_student_whitelist") or {}).items()
            },
        )

    def _build_index(self) -> None:
        idx: Dict[str, CategoryRule] = {}
        for cat in self._policy.categories.values():
            for dom in cat.domains:
                idx[dom] = cat
        self._domain_index = idx

    # ---------- matching helpers ----------

    @staticmethod
    def _host_of(url_or_host: str) -> str:
        s = str(url_or_host or "").strip()
        if "://" in s:
            parsed = urlparse(s)
            host = parsed.hostname or parsed.netloc or ""
            return host.lower()
        if "/" in s:
            s = s.split("/")[0]
        if ":" in s:
            s = s.split(":")[0]
        return s.lower().strip()

    @staticmethod
    def _domain_matches(host: str, candidate: str) -> bool:
        host = host.lower()
        candidate = candidate.lower()
        if host.startswith("www."):
            host_clean = host[4:]
        else:
            host_clean = host

        if candidate.startswith("www."):
            cand_clean = candidate[4:]
        else:
            cand_clean = candidate

        return host_clean == cand_clean or host_clean.endswith("." + cand_clean)

    def _active_windows(self, now: Optional[datetime] = None) -> List[TimeWindow]:
        now = now or datetime.now()
        day = now.strftime("%a").lower()[:3]
        t = now.time()
        active = []
        for w in self._policy.time_windows:
            if day not in w.days:
                continue
            try:
                s = dtime.fromisoformat(w.start)
                e = dtime.fromisoformat(w.end)
            except ValueError:
                continue
            if s <= t <= e:
                active.append(w)
        return active

    # ---------- evaluation ----------

    def evaluate(
        self,
        url: str,
        *,
        trno: Optional[str] = None,
        darajah: Optional[str] = None,
        station: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate access for a given URL and student.
        Handles student account suspension check, domain rules, time windows,
        violation recording, and automated account locking.

        Returns decision dict:
          {
            "action": "allow" | "warn" | "block",
            "account_status": "active" | "warned" | "blocked",
            "category": str | None,
            "message": str,
            "matched_rule": str,
            "host": str,
            "ts": str,
            "allow_override": bool
          }
        """
        self.reload()
        host = self._host_of(url)
        p = self._policy
        sid = str(trno).strip() if trno else None

        # Resolve real workstation ID and real timestamp from Lab Manager (no guessing)
        resolved_station = ""
        resolved_ts = ""

        if sid:
            lm_station = lab_manager.get_workstation_for_trno(sid, timestamp=now)
            if lm_station:
                resolved_station = lm_station
            else:
                logger.warning(
                    f"[PolicyEngine] Station could NOT be resolved from Lab Manager for TRNO={sid}, URL={url}. "
                    f"Provided station was: '{station or ''}'. Will NOT invent a value."
                )
                if station and str(station).strip().upper() not in ("UNKNOWN", "NONE", "PC", "LAB PC", "-"):
                    resolved_station = str(station).strip().upper()
                else:
                    resolved_station = ""

            # Real timestamp from Lab Manager session if available
            sessions = lab_manager.get_student_sessions(sid, limit=1)
            if sessions:
                latest = sessions[0]
                resolved_ts = str(latest.get("last_heartbeat") or latest.get("login_time") or latest.get("start_time") or "").strip()

        if not resolved_ts:
            if now:
                resolved_ts = now.isoformat(timespec="seconds") if isinstance(now, datetime) else str(now)
            elif sid:
                # Resolve session start from Lab Manager
                s_start = lab_manager.get_session_start(sid)
                if s_start:
                    resolved_ts = s_start.isoformat(timespec="seconds")

        # 1. First check: Is this student's account already locked / suspended?
        if sid and p.account_lock.enabled:
            if account_lock_service.is_account_blocked(sid):
                account_info = account_lock_service.get_account(sid)
                return {
                    "action": "block",
                    "account_status": "blocked",
                    "category": "account_suspended",
                    "message": (
                        f"Account Locked: Student {sid} is currently suspended from lab web access. "
                        f"Reason: {account_info.get('blocked_reason') or 'Policy infraction'}. "
                        "Please consult your teacher or lab administrator to unblock."
                    ),
                    "matched_rule": "account_lock",
                    "host": host,
                    "url": url,
                    "trno": sid,
                    "station": resolved_station,
                    "ts": resolved_ts,
                    "allow_override": True,
                }

        # 2. Per-student whitelist (always allows)
        if sid and sid in p.per_student_whitelist:
            if any(self._domain_matches(host, d) for d in p.per_student_whitelist[sid]):
                return self._decision("allow", "active", None, "Student whitelist permitted", host, "student_whitelist", station=resolved_station, ts=resolved_ts)

        # 3. Global whitelist (always allows)
        if any(self._domain_matches(host, d) for d in p.whitelist_domains):
            return self._decision("allow", "active", None, "Global whitelist permitted", host, "global_whitelist", station=resolved_station, ts=resolved_ts)

        # 4. Explicit global blocklist
        if any(self._domain_matches(host, d) for d in p.blocklist_domains):
            decision = self._decision(
                "block", "warned", "explicit_blocklist",
                "This website is blocked on all lab computers.", host, "explicit_blocklist",
                allow_override=False,
                station=resolved_station,
                ts=resolved_ts,
            )
            return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=True, ts=resolved_ts)

        # 5. Per-Darajah extra restrictions
        if darajah and str(darajah) in p.per_darajah_extra_block:
            for d in p.per_darajah_extra_block[str(darajah)]:
                if self._domain_matches(host, d):
                    decision = self._decision(
                        "block", "warned", "darajah_rule",
                        f"Access restricted for Darajah {darajah}.", host, f"darajah_{darajah}",
                        allow_override=True,
                        station=resolved_station,
                        ts=resolved_ts,
                    )
                    return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=False, ts=resolved_ts)

        # 5b. Movie & Torrent Download Detection (file extensions, magnet links, torrents)
        lower_url = (url or "").lower()
        is_movie_or_torrent = False
        movie_cat = "movie_download"
        movie_reason = "Movie/media download"

        if lower_url.startswith("magnet:") or ".torrent" in lower_url:
            is_movie_or_torrent = True
            movie_cat = "torrent"
            movie_reason = "BitTorrent peer-to-peer download attempt"
        elif any(lower_url.endswith(ext) or (ext + "?") in lower_url or (ext + "#") in lower_url for ext in [".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".iso"]):
            # Only flag if not an explicitly whitelisted domain
            if not any(self._domain_matches(host, d) for d in p.whitelist_domains):
                is_movie_or_torrent = True
                movie_cat = "movie_download"
                movie_reason = f"Unauthorized video/movie file download ({lower_url.split('?')[0].split('/')[-1]})"

        if is_movie_or_torrent:
            decision = self._decision(
                "block", "warned", movie_cat,
                f"Downloading movies or torrent files is strictly prohibited in the lab ({movie_reason}). Account suspended.",
                host, f"ext_{movie_cat}",
                allow_override=False,
                station=resolved_station,
                ts=resolved_ts,
            )
            return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=True, ts=resolved_ts)

        # 6. Category lookup
        cat_rule: Optional[CategoryRule] = None
        for dom, rule in self._domain_index.items():
            if self._domain_matches(host, dom):
                cat_rule = rule
                break

        # 7. Time-window enforcement
        active_windows = self._active_windows(now)
        if cat_rule and any(cat_rule.name in w.blocked_categories for w in active_windows):
            is_auto_block = cat_rule.name in p.account_lock.auto_block_categories
            decision = self._decision(
                "block", "warned", cat_rule.name,
                cat_rule.message or f"{cat_rule.name.replace('_', ' ').title()} is restricted during study hours.",
                host, "time_window",
                allow_override=cat_rule.allow_override,
                station=resolved_station,
                ts=resolved_ts,
            )
            return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=is_auto_block, ts=resolved_ts)

        # 8. Category default action
        if cat_rule:
            if cat_rule.action == "allow":
                return self._decision("allow", "active", cat_rule.name, cat_rule.message or "Permitted category", host, f"cat_{cat_rule.name}", station=resolved_station, ts=resolved_ts)

            is_auto_block = cat_rule.name in p.account_lock.auto_block_categories
            decision = self._decision(
                cat_rule.action, "warned", cat_rule.name,
                cat_rule.message or f"Access to {cat_rule.name.replace('_', ' ')} is restricted.",
                host, f"cat_{cat_rule.name}",
                allow_override=cat_rule.allow_override,
                station=resolved_station,
                ts=resolved_ts,
            )
            return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=is_auto_block, ts=resolved_ts)

        # 9. Default action
        return self._decision(p.default_action, "active", None, "Default policy", host, "default", station=resolved_station, ts=resolved_ts)

    def _handle_infraction(
        self,
        decision: Dict[str, Any],
        trno: Optional[str],
        url: str,
        host: str,
        station: Optional[str],
        auto_block: bool = False,
        ts: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply infraction, update warning count / account lock if student TRNO is present."""
        if not trno or not self._policy.account_lock.enabled:
            return decision

        max_warnings = self._policy.account_lock.max_warnings_before_block
        kwargs = {
            "trno": trno,
            "url": url,
            "host": host,
            "category": decision.get("category"),
            "action": decision.get("action", "block"),
            "station": station,
            "auto_block_category": auto_block,
            "max_warnings": max_warnings,
        }
        try:
            account = account_lock_service.record_violation(
                **kwargs,
                timestamp=ts or decision.get("ts"),
            )
        except TypeError:
            account = account_lock_service.record_violation(**kwargs)

        decision["account_status"] = account.get("status", "warned")
        decision["warning_count"] = account.get("warning_count", 0)

        if account.get("status") == "blocked":
            decision["action"] = "block"
            decision["message"] = (
                f"ACCOUNT SUSPENDED: Visiting unwanted site '{host}' has resulted in your student account ({trno}) "
                f"being locked. {account.get('blocked_reason') or ''} Contact your teacher or IT admin."
            )

        return decision

    @staticmethod
    def _decision(
        action: str,
        account_status: str,
        category: Optional[str],
        message: str,
        host: str,
        matched_rule: str,
        allow_override: bool = False,
        station: str = "",
        ts: str = "",
    ) -> Dict[str, Any]:
        return {
            "action": action,
            "account_status": account_status,
            "category": category,
            "message": message,
            "host": host,
            "matched_rule": matched_rule,
            "allow_override": allow_override,
            "station": station,
            "ts": ts,
        }

    def export_dns_blocklist(self, out_path: Optional[Path] = None) -> Path:
        """Export all blocked domains for Pi-hole or network gateway consumption."""
        out = out_path or (BASE_DIR / "data" / "dns_blocklist.txt")
        domains: Set[str] = set(self._policy.blocklist_domains)
        for cat in self._policy.categories.values():
            if cat.action == "block":
                domains.update(cat.domains)
        for extra in self._policy.per_darajah_extra_block.values():
            domains.update(extra)
        # Remove any whitelisted domains
        domains = domains - self._policy.whitelist_domains
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(sorted(domains)) + "\n", encoding="utf-8")
        return out


# Global singleton instance
policy_engine = PolicyEngine()
