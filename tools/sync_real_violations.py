# tools/sync_real_violations.py
"""
Synchronize REAL policy violations from historical browsing cache and Lab Manager.
Guarantees:
  1. 100% Real Data: Ingests real visits from data/browsing_history_cache.csv for the 14-day window.
  2. Authoritative Workstation: Resolves machine_id (e.g. 'RB-46', 'MK-50') from Lab Manager session history.
  3. Ad-Redirect vs. Deliberate Browsing:
     - Ad redirects / transient hits: WARNED (not blocked).
     - Deliberate time spent on Instagram or prohibited sites: BLOCKED.
  4. Populates students/violations/audit_YYYY-MM-DD.jsonl & data/student_accounts.json.
"""
from __future__ import annotations
import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sync_real_violations")

from app.services.lab_manager_service import lab_manager
from app.services.policy_engine import policy_engine
from app.services.violation_service import violation_service
from app.services.account_lock_service import account_lock_service

DATA_DIR = ROOT_DIR / "data"
CACHE_CSV = DATA_DIR / "browsing_history_cache.csv"
SESSION_HISTORY_FILE = DATA_DIR / "lab_manager" / "session_history.json"
VIOLATIONS_DIR = ROOT_DIR / "students" / "violations"
VIOLATIONS_DIR.mkdir(parents=True, exist_ok=True)

# Increase CSV field size limit for large URLs and long tokens
_max_int = sys.maxsize
while True:
    try:
        csv.field_size_limit(_max_int)
        break
    except OverflowError:
        _max_int = int(_max_int / 10)



def load_session_lookup() -> Dict[str, List[Dict[str, Any]]]:
    """Build student TRNO -> list of (start_dt, end_dt, machine_id) for fast workstation lookup."""
    lookup: Dict[str, List[Dict[str, Any]]] = {}
    if not SESSION_HISTORY_FILE.exists():
        logger.warning(f"Session history not found at {SESSION_HISTORY_FILE}")
        return lookup

    try:
        data = json.loads(SESSION_HISTORY_FILE.read_text(encoding="utf-8") or "[]")
        for sess in data:
            u = str(sess.get("username") or "").strip()
            mach = lab_manager.normalize_station_name(sess.get("machine_id"))
            st_raw = sess.get("start_time") or sess.get("login_time")
            et_raw = sess.get("logout_time") or sess.get("last_heartbeat") or st_raw
            if not u or not mach or not st_raw:
                continue

            try:
                st = datetime.fromisoformat(str(st_raw).replace("Z", "").split("+")[0])
                et = datetime.fromisoformat(str(et_raw).replace("Z", "").split("+")[0])
                if et < st:
                    et = st + timedelta(minutes=60)
                # Add 5-minute buffer on either side for workstation clock variations
                lookup.setdefault(u, []).append({
                    "start": st - timedelta(minutes=5),
                    "end": et + timedelta(minutes=5),
                    "station": mach,
                })
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Error loading session history: {e}")

    logger.info(f"Loaded workstation history for {len(lookup)} students.")
    return lookup
def resolve_station_from_lookup(trno: str, visit_dt: datetime, lookup: Dict[str, List[Dict[str, Any]]]) -> str:
    """Find exact workstation where student was logged in at visit_dt."""
    sessions = lookup.get(str(trno).strip(), [])
    for s in sessions:
        if s["start"] <= visit_dt <= s["end"]:
            return s["station"]
    if sessions:
        # Fallback to closest session
        return sessions[-1]["station"]
    return ""
def _build_enabled_since_map() -> Dict[str, datetime]:
    """Build a map of TRNO -> datetime from which violations should be counted.
    If a student was re-enabled (via Windows AD or Sentinel), only violations AFTER enabled_since count.
    Also syncs with Active Directory: if a student is enabled in Windows AD, past history is cleared.
    """
    try:
        import json as _json
        import re as _re
        accounts_path = ROOT_DIR / "data" / "student_accounts.json"
        if not accounts_path.exists():
            return {}
        data = _json.loads(accounts_path.read_text(encoding="utf-8"))
        result: Dict[str, datetime] = {}

        # Check Active Directory status for out-of-sync enabled students
        ad_disabled_trnos = set()
        try:
            from app.services.active_directory_service import ad_service
            disabled_users = ad_service.get_all_disabled_students()
            ad_disabled_trnos = {str(u.get("trno", "")).strip() for u in disabled_users if u.get("trno")}
        except Exception as ad_err:
            logger.debug(f"[Sync] Could not fetch AD disabled list: {ad_err}")

        modified = False
        now_dt = datetime.now()
        now_str = now_dt.isoformat(timespec="seconds")

        for trno, acc in data.items():
            sid = str(trno).strip()
            dar = str(acc.get("darajah", ""))
            is_senior = bool(_re.search(r'\b(8|9|10|11)\b', dar))

            # If enabled in Windows AD directly, forget past history and reset
            if not is_senior and ad_disabled_trnos and sid not in ad_disabled_trnos:
                if acc.get("status") == "blocked" or acc.get("violations"):
                    logger.info(f"[Sync] TRNO {sid} was enabled via Windows Active Directory — resetting history.")
                    acc["status"] = "active"
                    acc["warning_count"] = 0
                    acc["violations"] = []
                    acc["blocked_at"] = None
                    acc["last_incident"] = None
                    acc["blocked_reason"] = ""
                    acc["unblocked_at"] = now_str
                    acc["unblocked_by"] = "Windows Active Directory (Direct)"
                    acc["enabled_since"] = now_str
                    modified = True

            es = acc.get("enabled_since")
            if es:
                try:
                    result[sid] = datetime.fromisoformat(str(es).replace("Z", ""))
                except Exception:
                    pass

        if modified:
            try:
                accounts_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
                logger.info("[Sync] student_accounts.json updated with Windows AD re-enabled students.")
            except Exception as w_err:
                logger.warning(f"[Sync] Could not save updated accounts: {w_err}")

        logger.info(f"[Sync] enabled_since loaded for {len(result)} students.")
        return result
    except Exception as e:
        logger.warning(f"[Sync] Could not load enabled_since map: {e}")
        return {}


def _get_student_bookmarked_domains(trno: str, cache: Dict[str, set]) -> set:
    """Return the set of domains the student has bookmarked in Chrome.
    Reads \\\\10.10.117.76\\StudentsBrowserHistory\\<TRNO>\\Bookmarks (Chrome JSON).
    Falls back to empty set on any error.
    """
    if trno in cache:
        return cache[trno]

    domains: set = set()
    try:
        import json as _json
        import urllib.parse as _up
        bookmark_path = Path(r"\\10.10.117.76\StudentsBrowserHistory") / trno / "Bookmarks"
        if not bookmark_path.exists():
            cache[trno] = domains
            return domains

        bk = _json.loads(bookmark_path.read_text(encoding="utf-8", errors="replace"))

        def _extract(node):
            if isinstance(node, dict):
                if node.get("type") == "url":
                    u = node.get("url", "")
                    try:
                        h = _up.urlparse(u).hostname or ""
                        if h:
                            # strip www. prefix for broad matching
                            domains.add(h.lower().lstrip("www."))
                    except Exception:
                        pass
                for child in node.get("children", []):
                    _extract(child)
            elif isinstance(node, list):
                for item in node:
                    _extract(item)

        roots = bk.get("roots", {})
        for root_node in roots.values():
            _extract(root_node)
    except Exception:
        pass

    cache[trno] = domains
    return domains


def sync_violations_from_cache(days_back: int = 14) -> Dict[str, Any]:
    """Scan browsing_history_cache.csv for policy infractions in the last N days.
    
    Policy rules applied:
    - enabled_since: violations before a student's re-enable date are ignored.
    - bookmarks: sites a student has bookmarked are never counted as violations.
    - ad_redirect: transient ad hits (<20s dwell, <=2 hits) are warnings only.
    """
    if not CACHE_CSV.exists():
        logger.error(f"Browsing cache not found at {CACHE_CSV}")
        return {"error": "browsing_history_cache.csv not found"}

    cutoff_date = (datetime.now() - timedelta(days=days_back)).date()
    now = datetime.now()

    logger.info("Building Lab Manager workstation lookup...")
    session_lookup = load_session_lookup()

    # Load per-student re-enable dates (violations before this date are ignored)
    enabled_since_map = _build_enabled_since_map()

    # Bookmark cache: trno -> set of bookmarked domains (populated lazily)
    bookmark_cache: Dict[str, set] = {}

    logger.info(f"Scanning {CACHE_CSV} for violations since {cutoff_date}...")
    
    # Track student domains for dwell/hit clustering
    student_domain_hits: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    with CACHE_CSV.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except Exception:
                continue

            vt_str = str(row.get("visit_time") or "").strip()
            if not vt_str:
                continue
            try:
                v_dt = datetime.fromisoformat(vt_str.replace("Z", "").split("+")[0])
            except Exception:
                continue

            if v_dt.date() < cutoff_date:
                continue

            trno = str(row.get("TRNO") or "").strip()
            url = str(row.get("url") or "").strip()
            domain = str(row.get("domain") or "").lower().strip()
            if not trno or not url:
                continue

            # ── Policy: skip visits before student's re-enable timestamp / Oct 31, 2026 ──
            EFFECTIVE_POLICY_CUTOFF = datetime(2026, 10, 31, 0, 0, 0)
            es_dt = enabled_since_map.get(trno) or EFFECTIVE_POLICY_CUTOFF
            if v_dt < es_dt:
                continue

            # Preliminary domain check
            host = policy_engine._host_of(url)

            # ── Policy: skip if the URL domain is in the student's bookmarks ──
            bare_host = host.lower().lstrip("www.")
            bookmarked = _get_student_bookmarked_domains(trno, bookmark_cache)
            if bare_host and bookmarked and (bare_host in bookmarked or host.lower() in bookmarked):
                continue
            lower_u = url.lower()

            # Legitimate Google SSO authentication endpoints are never infractions
            if host in ("accounts.youtube.com", "accounts.google.com") or "/accounts/setsid" in lower_u:
                continue

            # Smart YouTube Content Check: Educational & Bohra community videos are fully allowed
            if any(policy_engine._domain_matches(host, yd) for yd in ("youtube.com", "m.youtube.com", "youtu.be")):
                is_ent, yt_reason = policy_engine._is_youtube_entertainment(url, row.get("title", ""))
                if not is_ent:
                    continue  # Educational or Bohra community video — permitted, ignore!
                is_prohibited = True
            else:
                cat_rule = None
                for dom, rule in policy_engine._domain_index.items():
                    if policy_engine._domain_matches(host, dom):
                        cat_rule = rule
                        break

                is_prohibited = (
                    cat_rule is not None and cat_rule.action == "block"
                ) or any(
                    policy_engine._domain_matches(host, d) for d in policy_engine._policy.blocklist_domains
                )

                # Check movie/torrent file extensions
                if any(lower_u.endswith(ext) or (ext + "?") in lower_u for ext in [".mkv", ".mp4", ".torrent"]):
                    is_prohibited = True

            if is_prohibited:
                key = (trno, domain or host)
                student_domain_hits.setdefault(key, []).append({
                    "trno": trno,
                    "url": url,
                    "domain": domain or host,
                    "dt": v_dt,
                    "title": row.get("title", ""),
                    "class": row.get("Class", ""),
                    "group": row.get("group", ""),
                })

    logger.info(f"Found {len(student_domain_hits)} unique student-domain violation clusters.")

    # Process and write violations
    total_written = 0
    blocked_count = 0
    warned_count = 0
    date_files: Dict[str, List[Dict[str, Any]]] = {}

    for (trno, domain), hits in student_domain_hits.items():
        # Sort by timestamp
        hits.sort(key=lambda h: h["dt"])
        hit_count = len(hits)
        first_hit = hits[0]
        last_hit = hits[-1]
        
        # Calculate dwell time on domain
        dwell_sec = max(0, int((last_hit["dt"] - first_hit["dt"]).total_seconds()))

        # Determine if ad-directed vs. deliberate time spent
        sample_url = first_hit["url"]
        is_ad = policy_engine.is_ad_redirect(sample_url, dwell_sec=dwell_sec, hit_count=hit_count)

        # Workstation resolution
        station = resolve_station_from_lookup(trno, first_hit["dt"], session_lookup)

        # Extract specific activity / movie / video / search query
        specific_item = ""
        generic_words = {"youtube", "google", "bing", "watch", "video", "search", "chrome", "home", "account suspension", "welcome", "index", "sign in", "login", ""}
        for h in hits:
            t = str(h.get("title") or "").strip()
            if t and t.lower() not in generic_words and len(t) > 3:
                cleaned_t = re.sub(r'\s*[-|–—]\s*(YouTube|Google|Netflix|123Movies|Stream|Watch).*$', '', t, flags=re.I).strip()
                if cleaned_t and cleaned_t.lower() not in generic_words and len(cleaned_t) > 3:
                    specific_item = f'"{cleaned_t}"'
                    break
            u = str(h.get("url") or "")
            if not specific_item and u:
                try:
                    import urllib.parse
                    parsed = urllib.parse.urlparse(u)
                    qs = urllib.parse.parse_qs(parsed.query)
                    for qp in ("search_query", "q", "query", "k", "s"):
                        if qp in qs and qs[qp]:
                            val = urllib.parse.unquote_plus(qs[qp][0]).strip()
                            if val and val.lower() not in generic_words and len(val) > 2:
                                specific_item = f'"{val}"'
                                break
                    if not specific_item and any(ext in parsed.path.lower() for ext in (".mkv", ".mp4", ".avi", ".torrent", ".iso")):
                        filename = parsed.path.split("/")[-1]
                        if filename:
                            specific_item = f'"{urllib.parse.unquote(filename)}"'
                            break
                    if not specific_item and "v=" in parsed.query and "v" in qs and qs["v"]:
                        vid = qs["v"][0]
                        if vid and len(vid) >= 6:
                            specific_item = f"Video ID: {vid}"
                except Exception:
                    pass

        # Category resolution
        lower_dom = domain.lower()
        cat = "restricted"
        for dom_key, rule in policy_engine._domain_index.items():
            if policy_engine._domain_matches(domain, dom_key):
                cat = rule.name
                break

        is_yt_domain = any(policy_engine._domain_matches(domain, yd) for yd in ("youtube.com", "m.youtube.com", "youtu.be"))
        is_shorts = "/shorts/" in sample_url.lower() or "#shorts" in (first_hit.get("title") or "").lower()

        if is_shorts:
            cat = "youtube_shorts"
        elif is_yt_domain:
            cat = "youtube_movies"
        elif any(ext in sample_url.lower() for ext in (".mkv", ".mp4", ".torrent", ".avi", ".mov", ".iso")) or any(k in lower_dom for k in ("movie", "film", "123movies", "fmovies", "yts", "torrent", "pirat")):
            cat = "movie_download"
        elif any(k in lower_dom for k in ("netflix", "twitch", "prime", "stream", "video", "dailymotion")):
            cat = "streaming"
        elif any(k in lower_dom for k in ("instagram", "threads", "tiktok", "facebook", "twitter", "snapchat", "whatsapp", "pinterest")):
            cat = "social_media"
        elif any(k in lower_dom for k in ("roblox", "crazygames", "steam", "poki")):
            cat = "gaming"

        # Policy decision & specific reason formatting
        if is_ad:
            action = "warn"
            cat = "ad_redirect"
            warned_count += 1
            reason = f"Accessed site ({domain}) via ad redirect. Ad must be closed instantly — harmless advisory notice, zero penalty strikes."
        elif is_shorts:
            action = "block"
            blocked_count += 1
            reason = f"YouTube Shorts is strictly blocked on lab workstations: {specific_item or domain}."
        elif is_yt_domain or cat == "youtube_movies":
            action = "warn"
            warned_count += 1
            reason = f"YouTube Time-Passing Warning: {specific_item or domain}. Educational and Bohra community content is permitted."
        else:
            action = "block"
            blocked_count += 1
            if cat == "streaming" or "netflix" in lower_dom or "dailymotion" in lower_dom:
                if specific_item:
                    reason = f"Streaming: {specific_item} on {domain} — {hit_count} hit(s), {dwell_sec}s active time."
                else:
                    reason = f"Streaming video playback on {domain} — {hit_count} hit(s), {dwell_sec}s active time."
            elif cat == "movie_download" or "torrent" in lower_dom or "movie" in lower_dom:
                if specific_item:
                    reason = f"Movie download / torrent: {specific_item} from {domain} — {hit_count} hit(s), {dwell_sec}s active time."
                else:
                    reason = f"Downloading movie / torrent media on {domain} — {hit_count} hit(s), {dwell_sec}s active time."
            elif cat == "social_media" or any(s in lower_dom for s in ("instagram", "facebook", "tiktok", "twitter", "threads", "pinterest")):
                target = f": {specific_item}" if specific_item else ""
                reason = f"Social media access{target} on {domain} — {hit_count} hit(s), {dwell_sec}s active time."
            elif cat == "gaming" or "roblox" in lower_dom or "crazygames" in lower_dom:
                target = f": {specific_item}" if specific_item else ""
                reason = f"Online gaming{target} on {domain} — {hit_count} hit(s), {dwell_sec}s active time."
            else:
                target = f" ({specific_item})" if specific_item else ""
                reason = f"Deliberate access to prohibited site ({domain}{target}) — {hit_count} page hit(s), {dwell_sec}s active time."

        # Build canonical incident
        inc_date_str = first_hit["dt"].strftime("%Y-%m-%d")
        import hashlib
        inc_hash = hashlib.md5(f"{trno}:{domain}:{first_hit['dt'].strftime('%Y%m%d%H%M')}".encode()).hexdigest()[:6]
        inc_id = f"{first_hit['dt'].strftime('%Y%m%d%H%M%S')}-{trno}-{inc_hash}"

        # 14-day expiry
        retention_exp = (first_hit["dt"] + timedelta(days=14)).isoformat(timespec="seconds")

        # Resolve authentic student name from Lab Manager / roster
        real_student_name = lab_manager.get_student_name(trno)
        if not real_student_name or real_student_name.lower().startswith("student #"):
            from app.student_roster import get_student_info
            s_prof = get_student_info(trno) or {}
            real_student_name = s_prof.get("name") or f"Student #{trno}"

        incident = {
            "incident_id": inc_id,
            "is_new": True,
            "hit_count": hit_count,
            "severity": "HIGH" if action == "block" else "MEDIUM",
            "trno": trno,
            "student_name": real_student_name,
            "activity_title": specific_item.strip('"') if specific_item else (first_hit.get("title") or domain),
            "darajah": first_hit.get("class", "General"),
            "group": first_hit.get("group", "Talabaat"),
            "station": station,
            "timestamp": first_hit["dt"].isoformat(timespec="seconds"),
            "ts_source": "Lab Manager Session History",
            "url": sample_url,
            "domain": domain,
            "category": cat,
            "action": action,
            "reason": reason,
            "browser": "chrome.exe",
            "resolved_by": "MamalBrowse Sentinel Real Ingestion Engine",
            "policy_window_days": "14",
            "retention_expires_at": retention_exp,
        }

        date_files.setdefault(inc_date_str, []).append(incident)
        total_written += 1

        # Update student_accounts.json state
        if action == "block":
            account_lock_service.record_violation(
                trno=trno,
                url=sample_url,
                host=domain,
                category=cat,
                action="block",
                station=station,
                auto_block_category=(cat not in ("youtube_movies", "streaming", "ad_redirect") and not is_yt_domain),
                timestamp=first_hit["dt"].isoformat(timespec="seconds"),
                domain=domain,
            )
        else:
            # Policy Rule: Ad redirects NEVER count as violation strikes against student accounts
            if not is_ad:
                account_lock_service.record_violation(
                    trno=trno,
                    url=sample_url,
                    host=domain,
                    category=cat,
                    action="warn",
                    station=station,
                    auto_block_category=False,
                    timestamp=first_hit["dt"].isoformat(timespec="seconds"),
                    domain=domain,
                )

    # Write daily audit files with strict deduplication
    for day_str, incs in date_files.items():
        audit_file = VIOLATIONS_DIR / f"audit_{day_str}.jsonl"
        viol_file = VIOLATIONS_DIR / f"violations_{day_str}.jsonl"
        
        # Load existing incident IDs and signatures to avoid duplicate appends
        existing_ids = set()
        existing_signatures = set()
        if audit_file.exists():
            for line in audit_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        parsed = json.loads(line)
                        existing_ids.add(parsed.get("incident_id"))
                        sig = (
                            str(parsed.get("trno", "")).strip(),
                            str(parsed.get("timestamp") or "")[:16],
                            str(parsed.get("domain") or "").lower().strip()
                        )
                        existing_signatures.add(sig)
                    except Exception:
                        pass

        with audit_file.open("a", encoding="utf-8") as af, viol_file.open("a", encoding="utf-8") as vf:
            for inc in incs:
                inc_sig = (
                    str(inc.get("trno", "")).strip(),
                    str(inc.get("timestamp") or "")[:16],
                    str(inc.get("domain") or "").lower().strip()
                )
                if inc["incident_id"] not in existing_ids and inc_sig not in existing_signatures:
                    existing_ids.add(inc["incident_id"])
                    existing_signatures.add(inc_sig)
                    line = json.dumps(inc, ensure_ascii=False) + "\n"
                    af.write(line)
                    vf.write(line)

    logger.info(
        f"Real Violation Sync Complete! Ingested {total_written} real incident(s) across {len(date_files)} days "
        f"({blocked_count} blocked, {warned_count} warned)."
    )
    return {
        "ok": True,
        "total_incidents": total_written,
        "blocked": blocked_count,
        "warned": warned_count,
        "days_synced": list(date_files.keys()),
    }


if __name__ == "__main__":
    res = sync_violations_from_cache(days_back=14)
    print(json.dumps(res, indent=2))
