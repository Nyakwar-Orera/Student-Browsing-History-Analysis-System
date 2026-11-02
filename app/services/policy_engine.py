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
    auto_block_categories: List[str] = field(default_factory=lambda: ["adult", "proxy_vpn", "torrent"])
    max_warnings_before_block: int = 5


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
                "auto_block_categories": ["adult", "proxy_vpn", "torrent"],
                "max_warnings_before_block": 5,
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
            auto_block_categories=[c.lower().strip() for c in al_raw.get("auto_block_categories", ["adult", "proxy_vpn", "torrent"])],
            max_warnings_before_block=int(al_raw.get("max_warnings_before_block", 5)),
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

        # Authentication / SSO subdomains are never treated as prohibited streaming or media
        if host in ("accounts.youtube.com", "accounts.google.com"):
            return False

        if host.startswith("www."):
            host_clean = host[4:]
        else:
            host_clean = host

        if candidate.startswith("www."):
            cand_clean = candidate[4:]
        else:
            cand_clean = candidate

        return host_clean == cand_clean or host_clean.endswith("." + cand_clean)

    @staticmethod
    def is_transient_or_redirect(url: str, referrer: str = "", dwell_sec: int = 0, hit_count: int = 1, host: str = "") -> bool:
        """
        Detect if an unwanted hit was driven by an ad redirect, pop-up, tracker embed,
        or transient link rather than deliberate active engagement.

        Institutional Fair-Enforcement Rule:
          - Many students get directed to Instagram, Pinterest, TikTok, etc. via third-party
            links, search engine redirects, or ad embeds without intending to use them.
          - If the student is NOT logged in or NOT spending sustained time (dwell_sec < 20
            and hit_count <= 2), treat as an accidental redirect / transient landing.
          - Accidental redirects issue a harmless advisory notice and NEVER immediately block
            or penalize the student's account.
          - Only sustained dwell time (dwell_sec >= 20), repeat browsing (hit_count >= 3),
            or active usage endpoints count towards progressive policy strikes.
        """
        u = str(url or "").lower()
        ref = str(referrer or "").lower()
        h = str(host or "").lower()

        # Common ad network and tracker signatures
        ad_signals = [
            "utm_medium=ad", "utm_medium=cpc", "utm_medium=banner", "utm_source=ad",
            "popunder", "popup", "adclick", "clickid=", "ad_id=", "adservice",
            "doubleclick", "googleadservices", "googlesyndication", "adsterra",
            "monetag", "adcash", "propellerads", "popads", "outbrain", "taboola",
            "trafficjunky", "exoclick", "juicyads", "redirect", "landing"
        ]
        if any(sig in u for sig in ad_signals) or any(sig in ref for sig in ad_signals):
            return True

        # Unauthenticated landing / login / gateway endpoints with brief dwell
        login_or_landing = any(p in u for p in [
            "/login", "/accounts/login", "/signin", "/signup", "/r/", "/?authuser=",
            "/?ref=", "getstarted", "help.instagram", "/about", "/terms", "/privacy"
        ])
        if login_or_landing and dwell_sec < 30 and hit_count <= 2:
            return True

        # Social media & chat transient protection:
        # If student briefly touched the site without spending sustained time (< 20s) and hit count <= 2
        is_social_or_chat = any(d in h for d in [
            "instagram", "pinterest", "reddit", "tumblr", "tiktok", "facebook",
            "twitter", "x.com", "threads", "discord", "snapchat", "telegram", "whatsapp",
            "kik", "viber", "janitorai", "character.ai", "crushon", "chai"
        ])
        if is_social_or_chat:
            if 0 <= dwell_sec < 20 and hit_count <= 2:
                return True

        # General brief transient hit
        if 0 < dwell_sec < 15 and hit_count <= 1:
            return True

        return False

    # Backwards-compatible alias
    is_ad_redirect = is_transient_or_redirect

    def is_outside_permitted_hours(self, now: Optional[datetime] = None) -> tuple:
        """
        Check if current time is outside allowed operational hours:
          - Monday through Saturday: 08:00 to 21:30
          - Sunday: 10:00 to 17:00
        Returns (is_outside: bool, reason_message: str)
        """
        now = now or datetime.now()
        day = now.strftime("%a").lower()[:3]
        t = now.time()

        if day in ("mon", "tue", "wed", "thu", "fri", "sat"):
            s = dtime(8, 0)
            e = dtime(21, 30)
            if not (s <= t <= e):
                return True, f"Lab workstation access is only permitted between 08:00 and 21:30 (Monday to Saturday). Current time is {now.strftime('%H:%M')}."
        elif day == "sun":
            s = dtime(10, 0)
            e = dtime(17, 0)
            if not (s <= t <= e):
                return True, f"Lab workstation access on Sunday is only permitted between 10:00 and 17:00. Current time is {now.strftime('%H:%M')}."
        return False, ""

    def _is_bookmarked_by_student(self, trno: str, host: str) -> bool:
        """
        Check if student has bookmarked this domain in Chrome.
        Policy Principle: Bookmarked sites are recognized study shortcuts
        and NEVER lead to account disability or violation penalties.
        """
        if not trno or not host:
            return False

        sid = str(trno).strip()
        h_clean = host.lower().lstrip("www.")

        if not hasattr(self, "_bookmark_cache"):
            self._bookmark_cache: Dict[str, Tuple[float, set]] = {}

        now = time.time()
        cached = self._bookmark_cache.get(sid)
        if cached and (now - cached[0]) < 300:  # 5-minute cache
            domains = cached[1]
        else:
            domains = set()
            try:
                import json as _json
                import urllib.parse as _up
                from pathlib import Path as _P
                bk_file = _P(r"\\10.10.117.76\StudentsBrowserHistory") / sid / "Bookmarks"
                if bk_file.exists():
                    bk_data = _json.loads(bk_file.read_text(encoding="utf-8", errors="replace"))

                    def _extract(node):
                        if isinstance(node, dict):
                            if node.get("type") == "url":
                                u = node.get("url", "")
                                try:
                                    nh = _up.urlparse(u).hostname or ""
                                    if nh:
                                        domains.add(nh.lower().lstrip("www."))
                                except Exception:
                                    pass
                            for child in node.get("children", []):
                                _extract(child)
                        elif isinstance(node, list):
                            for item in node:
                                _extract(item)

                    for r in bk_data.get("roots", {}).values():
                        _extract(r)
            except Exception:
                pass
            self._bookmark_cache[sid] = (now, domains)

        return (h_clean in domains) or (host.lower() in domains)

    @staticmethod
    def _is_youtube_entertainment(url: str, title: str = "") -> tuple:
        """
        Smart YouTube content classifier.
        Returns (is_entertainment: bool, reason: str)

        Educational signals (allow silently):
          - STEM, coding, history, geography, languages, academic lectures, tutorials
          - Khan Academy, Ted-Ed, Kurzgesagt, Veritasium, CS50, freeCodeCamp, etc.
          - Bohra community videos (Aljamea, waaz, bayan, sabaq, marsiya, madeh, Quran hifz/tilawat, Syedna, Burhani)
          - YouTube homepage, search, account management (neutral)

        Entertainment / Prohibited signals (strike warning):
          - YouTube Shorts (/shorts/, #shorts) - unless educational or Bohra community
          - Music, songs, music videos, albums, remixes, lyrics, lofi, beats
          - Movies, cinema, trailers, full movies, scenes, dramas, serials, web series
          - Gaming, gameplay, playthroughs, streams, Roblox, Minecraft, GTA, BGMI, PUBG, Fortnite, etc.
          - Comedy, pranks, roasts, memes, funny videos, standup
          - Vlogs, challenges, mukbang, hauls, unboxing, routines, reactions
          - Cartoons, anime, wrestling, WWE, sports highlights
        """
        import urllib.parse as _up
        u = str(url or "").lower().strip()
        t = str(title or "").lower().strip()

        # YouTube homepage, search, and account pages — always allow
        neutral_paths = ["/", "/feed", "/results", "/search", "/account", "/signin", "/settings"]
        try:
            parsed = _up.urlparse(u)
            path = parsed.path.rstrip("/")
            qs = _up.parse_qs(parsed.query)

            if not path or path in neutral_paths or path.startswith("/feed"):
                return False, ""

            # Build full content string from title, path, and URL query params
            query_vals = " ".join(" ".join(vals) for vals in qs.values()).lower()
            content_str = f"{t} {path} {query_vals} {u}"

            # ── 1. Authoritative Bohra Community Whitelist Keywords ──────────
            # Bohra community videos (Aljamea-tus-Saifiyah, Dawat, Duat Mutlaqeen, waaz, Quran)
            bohra_community_keywords = [
                # Bohra / Community Identity
                "bohra", "bohras", "dawoodi bohra", "dawoodi bohras", "dawoodi", "bohra community",
                "bhara", "bhara community", "al-dawoodi", "mumineen", "muminaat", "talabaat", "talebaat", "talabat",
                # Institutions & Organisations
                "aljamea", "al-jamea", "al jamea", "aljamea-tus-saifiyah", "aljameatus saifiyah", "aljamea tus saifiyah",
                "jamea", "jameah", "jameasaifiyah", "saifiyah", "saifee", "burhani", "dars burhani", "darseburhani",
                "darse burhani", "dars-e-burhani", "alvazarat", "al-vazarat", "al vazarat", "alvazaratus saifiyah",
                "jamiat", "anjuman", "shabab", "toloba", "talabul ilm", "its52", "fmb", "faiz", "faizeen",
                "mahad al zahra", "mahaduzzahra", "sautuliman", "saut ul iman",
                # Leadership & Personalities (Duat Mutlaqeen & Dai)
                "syedna", "sayedna", "dai al-mutlaq", "dai-al-mutlaq", "daialmutlaq", "al-dai",
                "mufaddal saifuddin", "mufaddal", "saifuddin", "aliqadr",
                "mohammed burhanuddin", "burhanuddin", "taher saifuddin",
                "maula", "moula", "huzurala", "huzur ala", "aqa maula", "mansoor al yemen", "baba saheb",
                # Religious Practices, Sermons & Discourses
                "waaz", "vaaz", "bayan", "sabaq", "majlis", "darees", "darus",
                "marsiya", "marasiya", "madeh", "madh", "nasihat", "salaam", "salam",
                "matam", "nauha", "noha", "qasida", "qasidah", "ilteja", "munajaat", "dua", "do'a",
                "roza", "ziyarat", "ziarat", "washeq", "tasbeeh",
                # Milestones, Occasions & Calendar
                "ashara", "ashara mubaraka", "asharamubaraka", "miqaat", "miqat", "urs", "urs mubarak",
                "milad", "milad mubarak", "chehlum", "moharram", "muharram", "eid", "eid ul fitr", "eid ul adha",
                "laylatul qadr", "shab-e-qadr", "pehliraat", "galla",
                # Quranic Studies & Recitation
                "hifz", "al-quran", "al quran", "quran", "qur'an", "tilawat", "tajweed", "tajwid",
                "tartil", "surah", "ayat", "sipara", "juz",
                # Language & Literature
                "lisan ud dawat", "lisan-ud-dawat", "lisan uddawat", "lisan", "dawat", "dawat-e-hadiyah", "dawatehadiyah",
                "nahjul balagha", "daaim al-islam", "da'a'im", "rasail ikhwan al-safa", "rasa'il",
                # Historic & Holy Figures / Places
                "karbala", "shahadat", "imam husain", "imam hussain", "husain", "hussain",
                "imam ali", "hazrat ali", "moula ali", "murtaza", "fatema", "fatima", "hasan", "hassan",
                "ahle bait", "ahl al-bayt", "panjetan", "panjetan pak", "najaf", "kufa", "cairo", "fatimid",
                "misr", "al-anwar", "al-hakim", "al-azhar"
            ]
            if any(kw in content_str for kw in bohra_community_keywords):
                return False, ""  # Bohra community content — permitted silently

            # ── 2. Comprehensive Educational Whitelist Keywords ──────────────
            educational_keywords = [
                # STEM & Academics
                "science", "chemistry", "physics", "biology", "math", "mathematics",
                "algebra", "calculus", "geometry", "trigonometry", "statistics",
                "probability", "arithmetic", "fraction", "equation", "formula",
                # Computer Science & Coding
                "programming", "coding", "software", "python", "javascript", "java",
                "c++", "c#", "html", "css", "sql", "database", "data structure",
                "algorithm", "web development", "machine learning", "artificial intelligence",
                "cybersecurity", "networking", "linux", "git", "github",
                # Humanities & Languages
                "history", "geography", "economics", "civics", "political science",
                "english grammar", "vocabulary", "literature", "essay writing",
                "arabic grammar", "learn arabic", "learn french", "pronunciation",
                "urdu grammar", "french lesson", "calligraphy",
                # General Academic terms
                "lecture", "tutorial", "lesson", "learn", "learning", "study",
                "education", "academic", "exam", "examination", "revision",
                "notes", "textbook", "chapter", "classroom", "course", "curriculum",
                "syllabus", "ncert", "cbse", "icse", "experiment", "demonstration",
                "documentary", "how to solve", "step by step", "how does it work",
                # Renowned Educational Channels & Creators
                "khan academy", "crash course", "ted-ed", "ted talk", "kurzgesagt",
                "3blue1brown", "veritasium", "minutephysics", "smarter every day",
                "numberphile", "computerphile", "mark rober", "vsauce", "scishow",
                "national geographic", "nat geo", "bbc earth", "bbc documentary",
                "freecodecamp", "cs50", "mit opencourseware", "stanford online",
                "harvard online", "nptel", "geeksforgeeks", "codewithharry",
                "apna college", "physics wallah", "edx", "coursera",
            ]
            if any(kw in content_str for kw in educational_keywords):
                return False, ""  # Educational — allow silently

            # ── 3. Shorts Check (if not already educational/community) ───────
            if "/shorts/" in u or "#shorts" in t:
                return True, f"YouTube Shorts: {t or path.split('/')[-1]}"

            # ── 4. Non-educational / Entertainment signals ───────────────────
            entertainment_signals = {
                # Music & Audio
                "official music video": "Music Video",
                "music video": "Music Video",
                "official video": "Music / Media Video",
                "official audio": "Music Audio Track",
                "lyric video": "Song Lyrics",
                "lyrics": "Song Lyrics",
                "full song": "Song / Music Track",
                "song": "Song / Music",
                "soundtrack": "Soundtrack / OST",
                "album": "Music Album",
                "remix": "Music Remix",
                "dj remix": "DJ Music",
                "slowed reverb": "Slowed & Reverb Music",
                "slowed and reverb": "Slowed Music",
                "lofi hip hop": "Lo-Fi Beats",
                "lofi music": "Lo-Fi Beats",
                "bass boosted": "Bass Boosted Music",
                "rap song": "Rap Music",
                "hip hop": "Hip Hop Music",
                "bollywood song": "Bollywood Song",
                "punjabi song": "Punjabi Song",
                "hindi song": "Hindi Song",
                "cover song": "Cover Song",
                # Movies & Series
                "full movie": "Full Movie Streaming",
                "official trailer": "Movie Trailer",
                "teaser": "Movie / Series Teaser",
                "movie clips": "Movie Clips",
                "movie scene": "Movie Scene",
                "cinema": "Cinema / Movie",
                "bollywood movie": "Bollywood Movie",
                "hollywood movie": "Hollywood Movie",
                "web series": "Web Series Episode",
                "season 1": "TV / Drama Series",
                "season 2": "TV / Drama Series",
                "season 3": "TV / Drama Series",
                "episode 1": "Drama / Show Episode",
                "episode 2": "Drama / Show Episode",
                "episode 3": "Drama / Show Episode",
                "drama serial": "Drama Serial",
                "hindi serial": "TV Serial",
                "soap opera": "Soap Opera",
                # Gaming
                "gameplay": "Gaming Playthrough",
                "playthrough": "Gaming Playthrough",
                "let's play": "Gaming Stream",
                "walkthrough part": "Gaming Walkthrough",
                "gaming stream": "Gaming Livestream",
                "roblox": "Roblox Gaming",
                "minecraft": "Minecraft Gaming",
                "gta 5": "GTA Gaming",
                "gta v": "GTA Gaming",
                "free fire": "Free Fire Gaming",
                "bgmi": "BGMI Gaming",
                "pubg": "PUBG Gaming",
                "fortnite": "Fortnite Gaming",
                "valorant": "Valorant Gaming",
                "call of duty": "Call of Duty Gaming",
                "fifa 2": "FIFA Gaming",
                "brawl stars": "Brawl Stars Gaming",
                "among us": "Among Us Gaming",
                "speedrun": "Gaming Speedrun",
                "esports": "Esports Tournament",
                # Comedy & Pranks
                "prank": "Prank Video",
                "funny moments": "Funny Moments / Compilation",
                "comedy video": "Comedy Video",
                "comedy show": "Comedy Show",
                "stand up comedy": "Stand-up Comedy",
                "standup comedy": "Stand-up Comedy",
                "roast": "Roast Video",
                "roasting": "Roast Video",
                "meme compilation": "Meme Compilation",
                "try not to laugh": "Laughter Challenge",
                "jokes": "Jokes Video",
                "laughter challenge": "Laughter Challenge",
                "vines": "Vines Compilation",
                "funny clips": "Funny Clips",
                # Vlogs & Social
                "vlog": "Personal Vlog",
                "daily vlog": "Daily Vlog",
                "family vlog": "Family Vlog",
                "room tour": "Lifestyle / Tour",
                "house tour": "Lifestyle / Tour",
                "morning routine": "Lifestyle Routine",
                "night routine": "Lifestyle Routine",
                "what i eat": "Lifestyle Video",
                "mukbang": "Mukbang Eating Show",
                "eating show": "Mukbang Show",
                "unboxing": "Commercial Unboxing",
                "shopping haul": "Shopping Haul",
                "haul": "Shopping Haul",
                "grwm": "Get Ready With Me",
                "get ready with me": "Lifestyle Video",
                "asmr": "ASMR Entertainment",
                # Challenges & Viral
                "challenge": "Viral Challenge",
                "24 hours in": "Viral Challenge",
                "last to leave": "Viral Challenge",
                "mrbeast": "MrBeast / Viral Entertainment",
                "reacting to": "Reaction Video",
                "reaction video": "Reaction Video",
                "tiktok compilation": "TikTok Compilation",
                "satisfying video": "Satisfying Compilation",
                # Cartoons & Anime
                "anime episode": "Anime Streaming",
                "cartoon": "Cartoon",
                "shinchan": "Shinchan Cartoon",
                "doraemon": "Doraemon Cartoon",
                "tom and jerry": "Tom & Jerry Cartoon",
                "dragon ball": "Anime Streaming",
                "naruto": "Anime Streaming",
                "one piece": "Anime Streaming",
                "demon slayer": "Anime Streaming",
                "jujutsu kaisen": "Anime Streaming",
                # Sports & Fights
                "wwe": "WWE Wrestling",
                "smackdown": "WWE Wrestling",
                "raw highlights": "WWE Wrestling",
                "wrestling": "Wrestling",
                "ufc": "UFC / MMA Fight",
                "boxing match": "Boxing Match",
                "cricket highlights": "Cricket Highlights",
                "football highlights": "Football Highlights",
                "ipl highlights": "IPL Highlights",
            }
            for kw, reason_lbl in entertainment_signals.items():
                if kw in t:
                    return True, f"{reason_lbl}: {t[:65]}"

            # /watch URLs — if explicit non-educational/entertainment signals were found above, it was returned.
            # General video watches without entertainment markers are permitted for study.
            return False, ""

        except Exception:
            pass

        return False, ""

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
        referrer: Optional[str] = None,
        dwell_sec: int = 0,
        hit_count: int = 1,
        title: str = "",           # Optional: browser window title for YouTube smart detection
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

        # 1b. Check permitted lab operational hours: Mon-Sat 08:00–21:30, Sun 10:00–17:00
        is_outside, hours_msg = self.is_outside_permitted_hours(now)
        if is_outside:
            return {
                "action": "block",
                "lock": True,
                "account_status": "restricted_hours",
                "category": "restricted_hours",
                "message": f"⏰ LAB CLOSED: {hours_msg}",
                "matched_rule": "operating_hours",
                "host": host,
                "url": url,
                "trno": sid,
                "station": resolved_station,
                "ts": resolved_ts,
                "allow_override": False,
            }

        # Check Teacher / Asateza Temporary Academic Whitelist Passes
        try:
            from app.services.temporary_whitelist_service import temporary_whitelist_service
            is_temp_allowed, temp_reason = temporary_whitelist_service.is_temporarily_allowed(
                url_or_host=url or host, trno=sid or "", darajah=darajah or "", station=resolved_station or ""
            )
            if is_temp_allowed:
                return self._decision("allow", "active", "academic_pass", temp_reason, host, "teacher_temporary_pass", station=resolved_station, ts=resolved_ts)
        except Exception as e:
            logger.debug(f"[PolicyEngine] Error checking temp whitelist: {e}")

        # 2. Per-student whitelist (always allows)
        if sid and sid in p.per_student_whitelist:
            if any(self._domain_matches(host, d) for d in p.per_student_whitelist[sid]):
                return self._decision("allow", "active", None, "Student whitelist permitted", host, "student_whitelist", station=resolved_station, ts=resolved_ts)

        # 2b. Chrome Browser Bookmarks Exemption
        # Institutional Rule: Sites saved in student bookmarks must NEVER lead to account disability
        if sid and self._is_bookmarked_by_student(sid, host):
            return self._decision("allow", "active", "bookmark_exemption", f"'{host}' is in student browser bookmarks — permitted under institutional policy.", host, "bookmark_exemption", station=resolved_station, ts=resolved_ts)

        # 3. Smart YouTube Content Evaluation
        # Educational videos and Bohra community videos are ALLOWED for all students.
        # YouTube Shorts is strictly BLOCKED.
        # Movies & entertainment time-passing strikes live warnings (NEVER immediate auto-block).
        is_youtube_host = any(self._domain_matches(host, yd) for yd in [
            "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"
        ])
        if is_youtube_host:
            title_val = title or ""
            is_entertainment, yt_reason = self._is_youtube_entertainment(url, title_val)
            is_shorts = "/shorts/" in url.lower() or "#shorts" in title_val.lower()

            # YouTube Shorts: Strictly blocked on lab workstations
            if is_shorts:
                shorts_msg = "🚫 YouTube Shorts is strictly blocked on lab workstations. Please use YouTube exclusively for academic study or Bohra community curriculum."
                decision = self._decision(
                    "block", "warned", "youtube_shorts",
                    shorts_msg, host, "youtube_shorts_blocked",
                    allow_override=False,
                    station=resolved_station,
                    ts=resolved_ts,
                )
                return self._handle_infraction(
                    decision, sid, url, host, resolved_station,
                    auto_block=False,  # Progressive strike, not immediate lockout
                    ts=resolved_ts, referrer=referrer,
                    dwell_sec=dwell_sec, hit_count=hit_count
                )

            # YouTube Movies / Time-Passing: Warn student (never immediate disablement)
            if is_entertainment and (dwell_sec >= 4 or hit_count >= 2):
                yt_msg = (
                    f"⚠️ YouTube Time-Passing Warning: Non-educational entertainment video detected ({yt_reason}). "
                    "Lab workstations are reserved for academic and Bohra community studies. "
                    "Watching movies, funny pranks, comedy, music, or gaming is considered time-passing and is prohibited during lab hours."
                )
                decision = self._decision(
                    "warn", "active", "youtube_movies",
                    yt_msg, host, "youtube_smart_detect",
                    allow_override=True,
                    station=resolved_station,
                    ts=resolved_ts,
                )
                return self._handle_infraction(
                    decision, sid, url, host, resolved_station,
                    auto_block=False,  # Warning only — NEVER immediate auto-block
                    ts=resolved_ts, referrer=referrer,
                    dwell_sec=dwell_sec, hit_count=hit_count
                )
            # Educational, Bohra community, or neutral YouTube — allow silently
            return self._decision(
                "allow", "active", "educational",
                "YouTube — educational & Bohra community access permitted.",
                host, "youtube_allowed",
                station=resolved_station, ts=resolved_ts
            )

        # 4. Global whitelist (always allows)
        if any(self._domain_matches(host, d) for d in p.whitelist_domains):
            return self._decision("allow", "active", None, "Global whitelist permitted", host, "global_whitelist", station=resolved_station, ts=resolved_ts)

        # 5. Explicit global blocklist
        # Determine category from domain_index for smarter auto_block decision
        IMMEDIATE_THREAT_CATS = {"adult", "proxy_vpn", "torrent", "video_chat", "anonymous_qa"}
        if any(self._domain_matches(host, d) for d in p.blocklist_domains):
            # Check if this domain maps to an immediate-threat category
            matched_cat = None
            for dom, rule in self._domain_index.items():
                if self._domain_matches(host, dom):
                    matched_cat = rule.name
                    break
            is_immediate_threat = matched_cat in IMMEDIATE_THREAT_CATS
            decision = self._decision(
                "block", "warned", matched_cat or "explicit_blocklist",
                f"'{host}' is blocked on all lab computers — restricted platform.",
                host, "explicit_blocklist",
                allow_override=False,
                station=resolved_station,
                ts=resolved_ts,
            )
            return self._handle_infraction(
                decision, sid, url, host, resolved_station,
                auto_block=is_immediate_threat,  # Only immediate-threat categories trigger instant lockout
                ts=resolved_ts, referrer=referrer, dwell_sec=dwell_sec, hit_count=hit_count
            )

        # 6. Per-Darajah extra restrictions
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
                    return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=False, ts=resolved_ts, referrer=referrer, dwell_sec=dwell_sec, hit_count=hit_count)

        # 7. Movie & Torrent Download Detection (file extensions, magnet links, torrents)
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
            return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=True, ts=resolved_ts, referrer=referrer, dwell_sec=dwell_sec, hit_count=hit_count)

        # 8. Category lookup
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
            return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=is_auto_block, ts=resolved_ts, referrer=referrer, dwell_sec=dwell_sec, hit_count=hit_count)

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
            return self._handle_infraction(decision, sid, url, host, resolved_station, auto_block=is_auto_block, ts=resolved_ts, referrer=referrer, dwell_sec=dwell_sec, hit_count=hit_count)

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
        referrer: Optional[str] = None,
        dwell_sec: int = 0,
        hit_count: int = 1,
    ) -> Dict[str, Any]:
        """Apply infraction, update warning count / account lock if student TRNO is present."""

        # ── Step A: Immediate-threat categories (auto_block enforced ONLY for these) ──
        IMMEDIATE_THREAT_CATEGORIES = {"adult", "proxy_vpn", "torrent", "video_chat", "anonymous_qa"}
        category = decision.get("category") or ""
        if auto_block and category not in IMMEDIATE_THREAT_CATEGORIES:
            # Downgrade to progressive-warning only (not immediate auto-block)
            auto_block = False

        # ── Step B: Transient / accidental redirect protection ─────────────────────
        # If student landed on a social/chat site via redirect, ad, popup, or very brief
        # dwell time (< 20s, hit_count <= 2) — advisory notice only, no strike recorded.
        # Note: YouTube Shorts is strictly prohibited and not treated as accidental redirect.
        is_transient = (
            category != "youtube_shorts"
            and self.is_transient_or_redirect(
                url, referrer=referrer or "", dwell_sec=dwell_sec,
                hit_count=hit_count, host=host
            )
        )
        if is_transient:
            auto_block = False
            decision["action"] = "warn"
            decision["account_status"] = "active"
            decision["message"] = (
                f"📌 Ad / Redirect Notice: '{host}' was opened via advertisement or popup redirect. "
                "The ad should be closed instantly! No violation penalty has been recorded. "
                "Please close this tab immediately and return to your work."
            )
            return decision

        if not trno or not self._policy.account_lock.enabled:
            return decision

        max_warnings = self._policy.account_lock.max_warnings_before_block
        kwargs = {
            "trno": trno,
            "url": url,
            "host": host,
            "category": category,
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

        strikes = account.get("warning_count", 1)
        rem = max(0, max_warnings - strikes)

        decision["account_status"] = account.get("status", "warned")
        decision["warning_count"] = strikes
        decision["max_warnings"] = max_warnings
        decision["warnings_remaining"] = rem

        if account.get("status") == "blocked":
            decision["action"] = "block"
            decision["lock"] = True
            # Build a clear, specific lockout message based on the violation category
            if category in ("youtube_movies", "streaming"):
                lockout_detail = (
                    "Reason: Repeated time-passing / entertainment content on YouTube "
                    "(movies, pranks, gaming, music videos, comedy) during study hours."
                )
            elif category in ("social_media", "messaging_chat", "gaming", "anonymous_qa", "video_chat", "ai_chatbots"):
                lockout_detail = (
                    f"Reason: Repeated access to restricted platform ({host}) — "
                    f"{category.replace('_', ' ').title()} policy."
                )
            elif category in ("adult", "proxy_vpn", "torrent"):
                lockout_detail = (
                    f"Reason: Access to strictly prohibited content ({category.replace('_', ' ')}) — "
                    "zero-tolerance policy."
                )
            else:
                lockout_detail = (
                    account.get("blocked_reason")
                    or f"Repeated policy violations ({category or host})."
                )
            decision["message"] = (
                f"🚨 ACCOUNT DISABLED & SUSPENDED — Strike {strikes}/{max_warnings}. "
                f"{lockout_detail} "
                "Your computer account and workstation have been locked. "
                "Contact your IT Administrator or Darajah Teacher to reinstate access."
            )
        else:
            # Progressive warning — explain clearly what was detected and what happens next
            if category in ("youtube_movies", "streaming"):
                warn_detail = (
                    "⏳ TIME-PASSING WARNING: You were watching entertainment/non-educational content on YouTube "
                    "(movies, pranks, gaming, comedy, music videos). "
                    "Only educational videos, lectures, tutorials, and Bohra community content are permitted."
                )
            elif category in ("social_media", "messaging_chat", "gaming", "anonymous_qa", "video_chat", "ai_chatbots"):
                warn_detail = (
                    f"🚫 RESTRICTED PLATFORM: '{host}' is blocked on all lab computers "
                    f"({category.replace('_', ' ').title()}). Close this tab immediately."
                )
            else:
                warn_detail = decision.get("message", "Restricted activity detected.")
            decision["message"] = (
                f"⚠️ Policy Warning — Strike {strikes} of {max_warnings}: {warn_detail} "
                f"You have {rem} warning(s) remaining before your account is automatically DISABLED and workstation locked."
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
