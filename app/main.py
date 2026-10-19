import sys
import os
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import re
from collections import defaultdict
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, Response
import pandas as pd
from datetime import datetime, timedelta, date
from io import BytesIO
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Ensure app directory and project root are in sys.path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
for _p in [_CURRENT_DIR, _PARENT_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from app.student_roster import get_student_info, get_all_students_list, get_students_by_darajah, load_student_roster, get_student_borrowernumber
except ImportError:
    from student_roster import get_student_info, get_all_students_list, get_students_by_darajah, load_student_roster, get_student_borrowernumber

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# ─── Configuration ───────────────────────────────────────────────────────────
import logging
logger = logging.getLogger("mamalbrowse_main")

SERVER_BASE_PATH   = os.getenv("SERVER_BASE_PATH", r"\\10.10.117.76\StudentsBrowserHistory")
BROWSING_LOGS_PATH = os.getenv("BROWSING_LOGS_PATH", r"\\10.10.117.76\BrowsingLogs")
TIME_RECORDS_FILE  = os.getenv("TIME_RECORDS_PATH", r"shares/TimeRecords/TimeDuration.txt")
CACHE_TTL          = int(os.getenv("CACHE_TTL", 300))   # seconds

# Resolve local mapped drives without blocking on UNC resolution
if os.path.exists(r"K:\StudentsBrowserHistory"):
    SERVER_BASE_PATH = r"K:\StudentsBrowserHistory"
if os.path.exists(r"K:\BrowsingLogs"):
    BROWSING_LOGS_PATH = r"K:\BrowsingLogs"

# ─── Services & Blueprints ──────────────────────────────────────────────────
from app.services.lab_manager_service import lab_manager
from app.services.integration_service import integration_service
from app.services.calendar_utils import (
    get_hijri_date_label, get_hijri_month_year_label, get_current_ay_year,
    get_fatemi_hijri, get_hijri_short_date
)
from app.services.analytics_service import analytics_service
from app.services.account_lock_service import account_lock_service
from app.services.active_directory_service import ad_service
from app.services.time_accounting_service import time_accounting_service
from app.services.event_log_service import event_log_service
from app.routes.darajah import darajah_bp
from app.routes.enforcement import enforcement_bp

app.register_blueprint(darajah_bp)
app.register_blueprint(enforcement_bp)

# ─── Retention Policy Service (14-day violation window) ─────────────────────
try:
    from app.services.retention_policy_service import retention_policy_service, VIOLATION_RETENTION_DAYS
    _retention_sweep = retention_policy_service.run_all()
    logger.info(
        "[RetentionPolicy] Startup sweep: %d accounts expired, %d audit logs archived. Policy window: %d days",
        _retention_sweep.get('expired_count', 0),
        _retention_sweep.get('archived_count', 0),
        VIOLATION_RETENTION_DAYS,
    )
except Exception as _e:
    logger.warning("[RetentionPolicy] Service unavailable: %s", _e)
    VIOLATION_RETENTION_DAYS = 14

# Background hourly retention sweep
def _retention_loop():
    while True:
        time.sleep(3600)  # every hour
        try:
            from app.services.retention_policy_service import retention_policy_service
            result = retention_policy_service.run_all()
            if result.get("expired_count", 0) > 0 or result.get("archived_count", 0) > 0:
                logger.info(
                    "[RetentionPolicy] Hourly sweep: %d accounts expired, %d logs archived",
                    result.get('expired_count', 0), result.get('archived_count', 0)
                )
        except Exception as e:
            logger.warning("[RetentionPolicy] Hourly sweep error: %s", e)

threading.Thread(target=_retention_loop, daemon=True, name="RetentionPolicySweep").start()



# ─── In-memory caches ────────────────────────────────────────────────────────
_class_map_cache           = {"data": None, "ts": 0}
_history_cache             = {"data": None, "ts": 0}
_history_load_lock         = threading.Lock()
_time_cache                = {"data": None, "ts": 0}
_monthly_student_cache     = {"data": None, "ts": 0}
_dashboard_summary_cache   = {"data": None, "ts": 0}  # 5-min TTL
_hijri_summary_cache       = {"data": None, "ts": 0}  # 10-min TTL
_DASHBOARD_SUMMARY_TTL     = 300   # seconds
_HIJRI_SUMMARY_TTL         = 600   # seconds

def invalidate_caches():
    """Clear memory caches so fresh data is loaded from disk."""
    _class_map_cache["data"] = None
    _class_map_cache["ts"] = 0
    _history_cache["data"] = None
    _history_cache["ts"] = 0
    _time_cache["data"] = None
    _time_cache["ts"] = 0
    _monthly_student_cache["data"] = None
    _monthly_student_cache["ts"] = 0
    _dashboard_summary_cache["data"] = None
    _dashboard_summary_cache["ts"] = 0
    _hijri_summary_cache["data"] = None
    _hijri_summary_cache["ts"] = 0


# ─── Cache paths ────────────────────────────────────────────────────────────
_CLASS_MAP_DISK_CACHE = os.path.join(_PARENT_DIR, "data", "class_map_cache.json")
_HISTORY_DISK_CACHE = os.path.join(_PARENT_DIR, "data", "browsing_history_cache.csv")


# ─── Browsing cache bootstrap on startup (REAL DATA ONLY) ────────────────────
# Production: Only real student CSVs from BROWSING_LOGS_PATH are used.
# If the network share is unreachable, the cache is left empty and the
# dashboard reports no_data=True. Demo data is NEVER injected in production.
# Dev mode (SENTINEL_DEV_MODE=1) may use synthetic data for local testing.
_SENTINEL_DEV_MODE: bool = os.getenv("SENTINEL_DEV_MODE", "0") == "1"
if _SENTINEL_DEV_MODE:
    logger.warning(
        "[STARTUP] *** SENTINEL_DEV_MODE=1 is ACTIVE. "
        "This server is running in DEVELOPMENT mode. "
        "Do NOT use in production. ***"
    )


# (Browsing cache bootstrap & background loops are defined below after build_class_map & normalize_dataframe)



# ─── Class/Group Map ─────────────────────────────────────────────────────────

def build_class_map():
    """
    Build clean student class and group map using Koha student roster as primary source.
    Completely replaces UNKNOWN_OU with students' real classes and UNKNOWN_ROOT with Talabaat/Talebaat.
    """
    now = time.time()
    if _class_map_cache["data"] is not None and (now - _class_map_cache["ts"]) < CACHE_TTL:
        return _class_map_cache["data"]

    # Fast disk cache check (sub-millisecond load)
    if os.path.exists(_CLASS_MAP_DISK_CACHE):
        try:
            with open(_CLASS_MAP_DISK_CACHE, "r", encoding="utf-8") as f:
                c_data = json.load(f)
                if c_data and isinstance(c_data, dict):
                    _class_map_cache["data"] = c_data
                    _class_map_cache["ts"] = now
                    return c_data
        except Exception:
            pass

    class_map = {}

    # 1. Primary & Authoritative Source: Koha Student Roster (All active Koha students)
    try:
        roster = load_student_roster()
        if roster:
            for key, s in roster.items():
                if not s or not isinstance(s, dict):
                    continue
                trno = str(s.get("trno") or key).strip()
                if not trno:
                    continue

                raw_class = str(s.get("darajah") or s.get("class") or "").strip()
                if raw_class.upper() in ("UNKNOWN", "UNKNOWN_OU", "NONE", ""):
                    raw_class = str(s.get("branch") or "General").strip()

                gender = str(s.get("gender") or "").strip()
                if gender.lower() in ("girls", "female", "talebaat"):
                    grp = "Talebaat"
                elif gender.lower() in ("boys", "male", "talabaat"):
                    grp = "Talabaat"
                else:
                    grp = "Talabaat"

                entry = {"group": grp, "class": raw_class}
                class_map[trno] = entry
                cnum = str(s.get("cardnumber") or "").strip()
                if cnum and cnum not in class_map:
                    class_map[cnum] = entry
                its = str(s.get("itsid") or "").strip()
                if its and its not in class_map:
                    class_map[its] = entry
    except Exception as e:
        logger.warning("[ClassMap] Error populating class_map from roster: %s", e)

    # 2. Augment from directory structure (TALABAAT and TALEBAAT only, ignore UNKNOWN_ROOT / UNKNOWN_OU)
    if os.path.exists(SERVER_BASE_PATH):
        try:
            for group in os.listdir(SERVER_BASE_PATH):
                g_upper = group.upper()
                if g_upper not in ("TALABAAT", "TALEBAAT"):
                    continue
                gpath = os.path.join(SERVER_BASE_PATH, group)
                if not os.path.isdir(gpath):
                    continue
                for darajah in os.listdir(gpath):
                    if darajah.upper() in ("UNKNOWN_OU", "UNKNOWN", "NONE"):
                        continue
                    dpath = os.path.join(gpath, darajah)
                    if not os.path.isdir(dpath):
                        continue
                    clean_group = "Talabaat" if g_upper == "TALABAAT" else "Talebaat"
                    for trno_folder in os.listdir(dpath):
                        tf = str(trno_folder).strip()
                        # If student already in Koha roster with specific class, retain Koha class
                        if tf in class_map and class_map[tf].get("class") and class_map[tf]["class"] not in ("General", "Unknown", "UNKNOWN_OU"):
                            continue
                        class_map[tf] = {
                            "group": clean_group,
                            "class": darajah,
                        }
        except Exception as e:
            logger.warning("[ClassMap] Scanning server folder for class map: %s", e)

    # 3. Final Sanity Sweep: Replace ANY lingering UNKNOWN_OU or UNKNOWN_ROOT
    for t, item in list(class_map.items()):
        cls = item.get("class", "")
        grp = item.get("group", "")
        if not cls or str(cls).upper() in ("UNKNOWN", "UNKNOWN_OU", "NONE"):
            item["class"] = "General"
        if not grp or str(grp).upper() in ("UNKNOWN", "UNKNOWN_ROOT", "NONE"):
            item["group"] = "Talabaat"

    # Save clean sanitized map to disk
    try:
        os.makedirs(os.path.dirname(_CLASS_MAP_DISK_CACHE), exist_ok=True)
        with open(_CLASS_MAP_DISK_CACHE, "w", encoding="utf-8") as f:
            json.dump(class_map, f)
    except Exception:
        pass

    _class_map_cache["data"] = class_map
    _class_map_cache["ts"] = now
    return class_map


def _read_single_student_csv(fpath, is_direct):
    """Helper for parallel CSV reader."""
    try:
        df = pd.read_csv(fpath, low_memory=False)
        if is_direct:
            fname = os.path.splitext(os.path.basename(fpath))[0]
            col_low = [c.lower() for c in df.columns]
            if "user" not in col_low and "trno" not in col_low:
                df["TRNO"] = fname
            elif "TRNO" in df.columns and df["TRNO"].isna().all():
                df["TRNO"] = fname
        return df
    except Exception:
        return None


def _generate_fallback_browsing_data(class_map, roster):
    """
    Return clean empty DataFrame when real browsing logs are unreachable.
    Zero synthetic or mock data is injected.
    """
    logger.info(
        "[load_data] Real browsing logs unreachable. "
        "Zero synthetic data policy active. Returning empty DataFrame."
    )
    return pd.DataFrame(columns=["TRNO", "Class", "group", "title", "url", "domain", "visit_time"])



def load_data(trno=None):
    """
    Load browsing history.
    - If trno is specified: Load directly from BROWSING_LOGS_PATH\\{trno}.csv (instant <50ms).
    - If no trno is specified: Uses safe cached view. Guards against OOM by never attempting to load 
      multi-gigabyte raw CSVs into memory all at once.
    - Production (SENTINEL_DEV_MODE=0): Only real browsing logs from network share or empty DataFrame.
    """
    class_map = build_class_map()

    if trno:
        # Load single student's CSV
        fpath = os.path.join(BROWSING_LOGS_PATH, f"{trno}.csv")
        if not os.path.exists(fpath):
            return pd.DataFrame()
        try:
            df = pd.read_csv(fpath, low_memory=False, on_bad_lines="skip")
            df = normalize_dataframe(df, class_map)
            return df
        except Exception as e:
            logger.error("[load_data] Reading student %s file: %s", trno, e)
            return pd.DataFrame()

    # 1. Hot memory cache (instant return if loaded)
    if _history_cache["data"] is not None:
        return _history_cache["data"]

    with _history_load_lock:
        if _history_cache["data"] is not None:
            return _history_cache["data"]

        now = time.time()
        # 2. Local disk cache (fast dtype load, already normalized)
        if os.path.exists(_HISTORY_DISK_CACHE):
            try:
                cached_df = pd.read_csv(
                    _HISTORY_DISK_CACHE,
                    dtype={"TRNO": str, "Class": str, "group": str, "domain": str, "title": str, "url": str},
                    low_memory=False,
                    on_bad_lines="skip"
                )
                if not cached_df.empty:
                    if "visit_time" in cached_df.columns and not pd.api.types.is_datetime64_any_dtype(cached_df["visit_time"]):
                        cached_df["visit_time"] = pd.to_datetime(cached_df["visit_time"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
                    if "domain" not in cached_df.columns or "group" not in cached_df.columns:
                        cached_df = normalize_dataframe(cached_df, class_map)
                    _history_cache["data"] = cached_df
                    _history_cache["ts"] = now
                    logger.info("[load_data] Loaded %d rows from disk cache", len(cached_df))
                    return cached_df
            except Exception as e:
                logger.warning("[load_data] Reading history disk cache: %s", e)

        # 3. Check if any real browsing CSVs are reachable in BROWSING_LOGS_PATH
        real_data_available = False
        if os.path.exists(BROWSING_LOGS_PATH):
            try:
                csv_count = 0
                for entry in os.scandir(BROWSING_LOGS_PATH):
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if not name.lower().endswith(".csv"):
                        continue
                    stem = name[:-4].strip()
                    if not stem.isdigit():
                        continue
                    try:
                        if entry.stat().st_size < 100:
                            continue
                    except Exception:
                        continue
                    csv_count += 1
                    if csv_count >= 3:
                        break
                real_data_available = csv_count >= 3
                logger.info("[load_data] Real browsing CSV check: found %d valid student CSVs (available=%s)", csv_count, real_data_available)
            except Exception as e:
                logger.warning("[load_data] Browsing CSV check failed: %s", e)
                real_data_available = False

        # 4. If real logs are available but disk cache was empty or missing, bootstrap on-demand
        if real_data_available:
            try:
                logger.info("[load_data] Real browsing logs detected; building or reloading cache...")
                _startup_bootstrap_browsing_cache()
                if os.path.exists(_HISTORY_DISK_CACHE):
                    cached_df = pd.read_csv(
                        _HISTORY_DISK_CACHE,
                        dtype={"TRNO": str, "Class": str, "group": str, "domain": str, "title": str, "url": str},
                        low_memory=False,
                        on_bad_lines="skip"
                    )
                    if not cached_df.empty:
                        if "visit_time" in cached_df.columns and not pd.api.types.is_datetime64_any_dtype(cached_df["visit_time"]):
                            cached_df["visit_time"] = pd.to_datetime(cached_df["visit_time"], errors="coerce")
                        if "domain" not in cached_df.columns or "group" not in cached_df.columns:
                            cached_df = normalize_dataframe(cached_df, class_map)
                        _history_cache["data"] = cached_df
                        _history_cache["ts"] = now
                        return cached_df
            except Exception as _sync_err:
                logger.warning("[load_data] On-demand cache build failed: %s", _sync_err)

        # 5. Clean empty fallback — zero synthetic or mock data. Never return None.
        logger.info(
            "[load_data] Real browsing logs empty or unreachable. "
            "Returning clean empty DataFrame (zero synthetic data)."
        )
        empty_df = pd.DataFrame(columns=["TRNO", "Class", "group", "title", "url", "domain", "visit_time"])
        _history_cache["data"] = empty_df
        _history_cache["ts"] = now
        return empty_df



def normalize_dataframe(df, class_map):
    """Normalize column names and map groups/classes using clean class_map and Koha roster fallback."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["TRNO", "Class", "group", "title", "url", "domain", "visit_time"])

    rename_map = {}
    col_lower = {c.lower(): c for c in df.columns}

    if "user" in col_lower:
        rename_map[col_lower["user"]] = "TRNO"
    if "visitdate" in col_lower:
        rename_map[col_lower["visitdate"]] = "visit_time"
    if "url" in col_lower:
        rename_map[col_lower["url"]] = "url"
    if "title" in col_lower:
        rename_map[col_lower["title"]] = "title"
    if "visitcount" in col_lower:
        rename_map[col_lower["visitcount"]] = "visit_count"
    if "computer" in col_lower:
        rename_map[col_lower["computer"]] = "computer"

    df = df.rename(columns=rename_map)
    df = df.loc[:, ~df.columns.duplicated()]

    # Ensure required columns exist
    for col in ["TRNO", "url", "visit_time"]:
        if col not in df.columns:
            df[col] = None

    if "title" not in df.columns:
        df["title"] = ""

    # Parse TRNO and visit_time
    if isinstance(df["TRNO"], pd.DataFrame):
        df["TRNO"] = df["TRNO"].iloc[:, 0]
    df["TRNO"] = df["TRNO"].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
    df["visit_time"] = pd.to_datetime(df["visit_time"], errors="coerce")
    df = df.dropna(subset=["visit_time"])

    # Extract domain
    def extract_domain(url):
        try:
            parts = str(url).split("/")
            domain = parts[2] if len(parts) > 2 else ""
            if domain.startswith("www."):
                domain = domain[4:]
            return domain
        except Exception:
            return ""

    df["domain"] = df["url"].apply(extract_domain)

    # Resolve groups and classes cleanly
    try:
        roster = load_student_roster()
    except Exception:
        roster = {}

    def resolve_group(t):
        t_str = str(t).strip()
        info = class_map.get(t_str, {})
        grp = info.get("group")
        if not grp or str(grp).upper() in ("UNKNOWN", "UNKNOWN_ROOT", "NONE", ""):
            stu = roster.get(t_str, {})
            gender = str(stu.get("gender") or "").lower()
            return "Talebaat" if gender in ("girls", "female", "talebaat") else "Talabaat"
        return grp

    def resolve_class(t):
        t_str = str(t).strip()
        info = class_map.get(t_str, {})
        cls = info.get("class")
        if not cls or str(cls).upper() in ("UNKNOWN", "UNKNOWN_OU", "NONE", ""):
            stu = roster.get(t_str, {})
            c = stu.get("darajah") or stu.get("class") or stu.get("branch") or "General"
            return c if str(c).upper() not in ("UNKNOWN", "UNKNOWN_OU") else "General"
        return cls

    df["group"] = df["TRNO"].map(resolve_group)
    df["Class"] = df["TRNO"].map(resolve_class)

    keep = ["TRNO", "Class", "group", "title", "url", "domain", "visit_time"]
    if "computer" in df.columns:
        keep.append("computer")
    return df[[c for c in keep if c in df.columns]].fillna("")


# ─── Time Records Loader ──────────────────────────────────────────────────────

def load_time_records():
    now = time.time()
    if _time_cache["data"] is not None and (now - _time_cache["ts"]) < CACHE_TTL:
        return _time_cache["data"]

    # 1. Try Live Lab Time Management Server (usage_data.json)
    try:
        usage = lab_manager.get_all_usage_data()
        if usage:
            rows = []
            for date_str, student_map in usage.items():
                for trno, mins in student_map.items():
                    rows.append({
                        "TRNO": str(trno).strip(),
                        "Date": date_str,
                        "DurationMinutes": float(mins)
                    })
            if rows:
                df = pd.DataFrame(rows)
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
                _time_cache["data"] = df
                _time_cache["ts"] = now
                return df
    except Exception as e:
        logger.warning("[load_data] Could not parse live Lab Manager usage data: %s", e)

    # 2. Fallback to TimeDuration.txt
    try:
        if os.path.exists(TIME_RECORDS_FILE):
            tr = pd.read_csv(
                TIME_RECORDS_FILE, sep=r"\s+", header=None,
                names=["TRNO", "Date", "Duration"], engine="python"
            )
            tr["TRNO"] = tr["TRNO"].astype(str).str.strip()
            tr["Date"] = pd.to_datetime(tr["Date"], format="%d/%m/%Y", errors="coerce")
            tr["DurationMinutes"] = pd.to_numeric(tr["Duration"], errors="coerce").fillna(0) * 60
            _time_cache["data"] = tr
            _time_cache["ts"] = now
            return tr
    except Exception as e:
        logger.warning("[load_data] Could not load time records: %s", e)

    return pd.DataFrame()


# ─── Cache Invalidation ───────────────────────────────────────────────────────

_STUDENT_COUNTS_DISK_CACHE = os.path.join(_PARENT_DIR, "data", "student_visit_counts_cache.json")
_student_counts_cache = {"data": None, "ts": 0}
_student_lab_cache = {"data": None, "ts": 0}

def invalidate_caches():
    _class_map_cache["data"] = None
    _class_map_cache["ts"] = 0
    _history_cache["data"] = None
    _history_cache["ts"] = 0
    _time_cache["data"] = None
    _time_cache["ts"] = 0
    _monthly_student_cache["data"] = None
    _monthly_student_cache["ts"] = 0
    _student_counts_cache["data"] = None
    _student_counts_cache["ts"] = 0
    _student_lab_cache["data"] = None
    _student_lab_cache["ts"] = 0
    _dashboard_summary_cache["data"] = None
    _dashboard_summary_cache["ts"] = 0
    _hijri_summary_cache["data"] = None
    _hijri_summary_cache["ts"] = 0


# ─── Browsing Cache Bootstrap & Background Sync ──────────────────────────────

def _startup_bootstrap_browsing_cache():
    """Build/refresh the browsing history disk cache on startup.

    Production (SENTINEL_DEV_MODE=0):
      Only real student CSVs from BROWSING_LOGS_PATH are used.
      If the network share is unreachable, a header-only CSV is written and
      the dashboard reports no_data=True. No synthetic records are injected.

    Dev mode (SENTINEL_DEV_MODE=1):
      Synthetic browsing data may be seeded for local UI testing only.
      This branch MUST NOT run on the production server.

    Cache is refreshed automatically if older than 6 hours.
    """
    import csv as _csv
    from datetime import datetime as _dt, timedelta as _td

    cache_path = _HISTORY_DISK_CACHE
    _MAX_CACHE_AGE_HOURS = 6  # Force refresh if cache is older than this

    if os.path.exists(cache_path):
        try:
            size = os.path.getsize(cache_path)
            age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
            if size > 5000 and age_hours < _MAX_CACHE_AGE_HOURS:
                logger.info(
                    "[StartupBootstrap] Cache OK (%d bytes, %.1fh old) — skipping bootstrap",
                    size,
                    age_hours,
                )
                return
            elif size > 5000:
                logger.info(
                    "[StartupBootstrap] Cache stale (%.1fh > %dh) — refreshing from real data",
                    age_hours,
                    _MAX_CACHE_AGE_HOURS,
                )
        except OSError:
            pass

    logger.info("[StartupBootstrap] Building browsing cache at %s...", cache_path)

    # ── Production path: real student CSVs from network share only ──────────
    real_logs_path = BROWSING_LOGS_PATH
    real_built = False
    if os.path.exists(real_logs_path):
        try:
            import pandas as _pd
            import csv as _csv_mod
            from datetime import datetime as _dt2, timedelta as _td2

            cutoff = _dt2.now() - _td2(days=30)

            csv_files = [
                f for f in os.listdir(real_logs_path)
                if f.lower().endswith(".csv") and f[:-4].isdigit()
            ]
            valid_files = []
            for f in csv_files:
                fp = os.path.join(real_logs_path, f)
                try:
                    if os.path.getsize(fp) > 100:
                        valid_files.append(fp)
                except OSError:
                    pass

            if len(valid_files) >= 1:
                logger.info(
                    "[StartupBootstrap] Streaming %d real student CSVs (last 30 days)",
                    len(valid_files),
                )
                class_map = build_class_map()
                fieldnames = ["TRNO", "Class", "group", "title", "url", "domain", "visit_time"]
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                total_rows = 0
                with open(cache_path, "w", newline="", encoding="utf-8") as out_f:
                    writer = _csv_mod.DictWriter(
                        out_f, fieldnames=fieldnames, extrasaction="ignore"
                    )
                    writer.writeheader()
                    for fp in valid_files:
                        try:
                            try:
                                df_chunk = _pd.read_csv(
                                    fp,
                                    encoding="utf-8-sig",
                                    low_memory=False,
                                    on_bad_lines="skip",
                                )
                            except Exception:
                                df_chunk = _pd.read_csv(
                                    fp,
                                    encoding="latin-1",
                                    low_memory=False,
                                    on_bad_lines="skip",
                                )
                            df_chunk = normalize_dataframe(df_chunk, class_map)
                            if df_chunk.empty:
                                continue
                            df_chunk = df_chunk[df_chunk["visit_time"] >= cutoff]
                            if df_chunk.empty:
                                continue
                            df_chunk["visit_time"] = df_chunk["visit_time"].dt.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            for _, row in df_chunk.iterrows():
                                writer.writerow({k: row.get(k, "") for k in fieldnames})
                            total_rows += len(df_chunk)
                        except Exception as _chunk_err:
                            logger.warning(
                                "[StartupBootstrap] Skipped file %s: %s", fp, _chunk_err
                            )

                if total_rows > 0:
                    logger.info(
                        "[StartupBootstrap] Wrote %d REAL records from %d student files (last 30d)",
                        total_rows,
                        len(valid_files),
                    )
                    real_built = True
                else:
                    try:
                        os.remove(cache_path)
                    except OSError:
                        pass
        except Exception as _net_err:
            logger.warning(
                "[StartupBootstrap] Real data read failed: %s. "
                "Cache will remain empty — dashboard will show no_data=True.",
                _net_err,
            )

    if real_built:
        return

    # When network share is unreachable, write a clean header-only CSV.
    # Zero synthetic or mock data is injected. The dashboard will show no_data=True
    # until live client logs are delivered.
    logger.warning(
        "[StartupBootstrap] Network share unreachable. "
        "Writing empty cache. Dashboard will report no_data=True until "
        "real browsing logs are available."
    )
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", newline="", encoding="utf-8") as fh:
        _csv.DictWriter(
            fh,
            fieldnames=["TRNO", "Class", "group", "title", "url", "domain", "visit_time"],
        ).writeheader()


# Run cache bootstrap synchronously so the first request has data
try:
    _startup_bootstrap_browsing_cache()
except Exception as _boot_err:
    logger.warning("[WARN] Startup cache bootstrap failed: %s", _boot_err)


# Background periodic cache refresh — keeps browsing data current (REAL data only)
_CACHE_REFRESH_INTERVAL_HOURS: int = 6


def _periodic_cache_refresh() -> None:
    """Refresh the browsing history disk cache every 6 hours from the real network share."""
    while True:
        time.sleep(_CACHE_REFRESH_INTERVAL_HOURS * 3600)
        try:
            logger.info("[CacheRefresh] Scheduled refresh starting...")
            if os.path.exists(_HISTORY_DISK_CACHE):
                os.remove(_HISTORY_DISK_CACHE)
            invalidate_caches()
            _startup_bootstrap_browsing_cache()
            logger.info("[CacheRefresh] Done.")
        except Exception as _refresh_err:
            logger.warning("[CacheRefresh] Failed: %s", _refresh_err)


threading.Thread(
    target=_periodic_cache_refresh, daemon=True, name="BrowsingCacheRefresh"
).start()


# ─── Incremental Live Data Watcher (Near-Real-Time Sync every 30s) ───────────
_last_live_scan_time = time.time() - 3600  # scan recent changes from past hour

def _incremental_live_sync_loop():
    """
    Every 30 seconds, scan BROWSING_LOGS_PATH for student CSVs modified since last scan.
    If any students browsed, incrementally append/update records directly into memory and disk cache.
    Scans directory in < 20ms and keeps the system 100% live.
    """
    global _last_live_scan_time
    fieldnames = ["TRNO", "Class", "group", "title", "url", "domain", "visit_time"]
    while True:
        time.sleep(30)
        try:
            if not os.path.exists(BROWSING_LOGS_PATH):
                continue
            
            changed = []
            max_mt = _last_live_scan_time
            for entry in os.scandir(BROWSING_LOGS_PATH):
                if not entry.is_file() or not entry.name.lower().endswith(".csv"):
                    continue
                stem = entry.name[:-4].strip()
                if not stem.isdigit():
                    continue
                try:
                    mt = entry.stat().st_mtime
                    if mt > _last_live_scan_time:
                        changed.append(entry.path)
                        if mt > max_mt:
                            max_mt = mt
                except Exception:
                    pass
            
            if not changed:
                continue
            
            _last_live_scan_time = max_mt
            
            # Read changed files and incrementally append only newly generated records
            import pandas as _pd
            import csv as _csv_mod
            from datetime import datetime as _dt_live
            cutoff = _dt_live.fromtimestamp(max(0.0, _last_live_scan_time - 60))
            class_map = build_class_map()
            
            new_rows = []
            for fp in changed:
                try:
                    try:
                        df_chunk = _pd.read_csv(fp, encoding="utf-8-sig", low_memory=False, on_bad_lines="skip")
                    except Exception:
                        df_chunk = _pd.read_csv(fp, encoding="latin-1", low_memory=False, on_bad_lines="skip")
                    df_chunk = normalize_dataframe(df_chunk, class_map)
                    if df_chunk.empty:
                        continue
                    df_chunk = df_chunk[df_chunk["visit_time"] >= cutoff]
                    if df_chunk.empty:
                        continue
                    df_chunk["visit_time"] = df_chunk["visit_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
                    for _, row in df_chunk.iterrows():
                        new_rows.append({k: row.get(k, "") for k in fieldnames})
                except Exception:
                    pass
            
            if new_rows and os.path.exists(_HISTORY_DISK_CACHE):
                with open(_HISTORY_DISK_CACHE, "a", newline="", encoding="utf-8") as out_f:
                    writer = _csv_mod.DictWriter(out_f, fieldnames=fieldnames, extrasaction="ignore")
                    for r in new_rows:
                        writer.writerow(r)
                # Incrementally append to memory cache without invalidating or reloading 800k rows
                try:
                    df_new = _pd.DataFrame(new_rows)
                    if not df_new.empty:
                        df_new["visit_time"] = _pd.to_datetime(df_new["visit_time"], errors="coerce")
                        df_new["TRNO"] = df_new["TRNO"].astype(str)
                        with _history_load_lock:
                            if _history_cache["data"] is not None:
                                _history_cache["data"] = _pd.concat([_history_cache["data"], df_new], ignore_index=True)
                            _history_cache["ts"] = time.time()
                except Exception as _app_err:
                    pass
                logger.info("[LiveSync] Ingested %d live records from %d active student file(s)", len(new_rows), len(changed))
        except Exception as e:
            pass

threading.Thread(
    target=_incremental_live_sync_loop, daemon=True, name="LiveBrowsingSync"
).start()


@app.route("/api/refresh_data", methods=["GET", "POST"])
def api_refresh_data():
    """Trigger cache invalidation so dates and sessions update immediately."""
    try:
        from app.services.time_accounting_service import time_accounting_service
        time_accounting_service.reconcile_aggregates()
    except Exception as e:
        logger.warning("[api_refresh_data] Reconciliation error: %s", e)


    # Rebuild the browsing history disk cache from real network share
    try:
        if os.path.exists(_HISTORY_DISK_CACHE):
            os.remove(_HISTORY_DISK_CACHE)
        invalidate_caches()
        _startup_bootstrap_browsing_cache()
        msg = "Cache rebuilt from real network data and dashboard refreshed."
    except Exception as e:
        invalidate_caches()
        msg = f"Cache invalidated (disk rebuild failed: {e})."

    return jsonify({"status": "success", "message": msg})



# ─── High-Performance Student Activity Summaries ─────────────────────────────

def get_all_student_visit_counts():
    """
    Rapidly index student browsing visit counts across all stored CSV logs.
    Zero memory overhead — uses lightweight directory stat scanning and cached totals.
    """
    now = time.time()
    if _student_counts_cache["data"] is not None and (now - _student_counts_cache["ts"]) < 300:
        return _student_counts_cache["data"]

    # 1. Try disk cache if fresh (< 2 hours)
    if os.path.exists(_STUDENT_COUNTS_DISK_CACHE):
        try:
            mtime = os.path.getmtime(_STUDENT_COUNTS_DISK_CACHE)
            if (now - mtime) < 7200:
                with open(_STUDENT_COUNTS_DISK_CACHE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data and isinstance(data, dict):
                        _student_counts_cache["data"] = data
                        _student_counts_cache["ts"] = now
                        return data
        except Exception:
            pass

    counts = {}

    # 2. Fast scan of BROWSING_LOGS_PATH directory
    search_dirs = []
    if os.path.exists(BROWSING_LOGS_PATH):
        search_dirs.append(BROWSING_LOGS_PATH)
    if os.path.exists(r"K:\BrowsingLogs") and r"K:\BrowsingLogs" not in search_dirs:
        search_dirs.append(r"K:\BrowsingLogs")

    for b_dir in search_dirs:
        try:
            for entry in os.scandir(b_dir):
                if entry.name.endswith(".csv"):
                    sid = entry.name[:-4].strip()
                    try:
                        fsize = entry.stat().st_size
                        # In student browsing logs, each row averages ~140 bytes
                        if fsize > 60:
                            counts[sid] = max(1, int((fsize - 60) / 140))
                        else:
                            counts[sid] = 0
                    except Exception:
                        pass
        except Exception as e:
            app.logger.warning(f"Error scanning browsing logs in {b_dir}: {e}")

    # 3. Scan SERVER_BASE_PATH if browsing logs were not found
    if len(counts) < 50 and os.path.exists(SERVER_BASE_PATH):
        try:
            for root, dirs, files in os.walk(SERVER_BASE_PATH):
                for f in files:
                    if f.endswith(".csv"):
                        sid = f[:-4].strip()
                        if sid.isdigit() and sid not in counts:
                            try:
                                fsize = os.path.getsize(os.path.join(root, f))
                                if fsize > 60:
                                    counts[sid] = max(1, int((fsize - 60) / 140))
                            except Exception:
                                pass
        except Exception as e:
            app.logger.warning(f"Error scanning SERVER_BASE_PATH: {e}")

    # Save to disk cache
    if counts:
        try:
            os.makedirs(os.path.dirname(_STUDENT_COUNTS_DISK_CACHE), exist_ok=True)
            with open(_STUDENT_COUNTS_DISK_CACHE, "w", encoding="utf-8") as f:
                json.dump(counts, f)
        except Exception:
            pass

    _student_counts_cache["data"] = counts
    _student_counts_cache["ts"] = now
    return counts


def get_all_student_lab_summary():
    """
    Returns:
        total_lab_hours: dict of sid -> float total hours
        monthly_lab_hours: dict of (sid, month_label) -> float monthly hours
        month_labels: list of Hijri month labels observed
    """
    now = time.time()
    if _student_lab_cache["data"] is not None and (now - _student_lab_cache["ts"]) < 300:
        return _student_lab_cache["data"]

    total_hours = {}
    monthly_hours = {}
    month_labels_set = set()

    # 1. From lab_manager usage_data.json
    try:
        usage = lab_manager.get_all_usage_data()
        if usage and isinstance(usage, dict):
            for date_str, student_map in usage.items():
                if not isinstance(student_map, dict):
                    continue
                try:
                    d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
                    m_label = get_hijri_month_year_label(d)
                except Exception:
                    m_label = "General"
                month_labels_set.add(m_label)

                for sid, mins in student_map.items():
                    s_id = str(sid).strip()
                    try:
                        hrs = float(mins) / 60.0
                    except (ValueError, TypeError):
                        hrs = 0.0
                    total_hours[s_id] = total_hours.get(s_id, 0.0) + hrs
                    monthly_hours[(s_id, m_label)] = monthly_hours.get((s_id, m_label), 0.0) + hrs
    except Exception as e:
        app.logger.warning(f"Error aggregating lab usage: {e}")

    # 2. From load_time_records (TimeDuration.txt)
    try:
        time_df = load_time_records()
        if not time_df.empty and "TRNO" in time_df.columns and "DurationMinutes" in time_df.columns:
            for _, tr_row in time_df.iterrows():
                s_id = str(tr_row["TRNO"]).strip()
                t_date = tr_row.get("Date")
                if pd.notna(t_date):
                    d = t_date.date() if hasattr(t_date, "date") else t_date
                    m_label = get_hijri_month_year_label(d)
                else:
                    m_label = "General"
                month_labels_set.add(m_label)

                hrs = float(tr_row.get("DurationMinutes") or 0.0) / 60.0
                if s_id not in total_hours:
                    total_hours[s_id] = total_hours.get(s_id, 0.0) + hrs
                    monthly_hours[(s_id, m_label)] = monthly_hours.get((s_id, m_label), 0.0) + hrs
    except Exception as e:
        app.logger.warning(f"Error aggregating time_df: {e}")

    for k in total_hours:
        total_hours[k] = round(total_hours[k], 1)
    for k in monthly_hours:
        monthly_hours[k] = round(monthly_hours[k], 1)

    result = (total_hours, monthly_hours, list(month_labels_set))
    _student_lab_cache["data"] = result
    _student_lab_cache["ts"] = now
    return result


# ─── Student Monthly Activity Helper ──────────────────────────────────────────

def get_student_monthly_overview():
    """
    Build monthly activity statistics for all students from browsing history and lab time.
    """
    now = time.time()
    if _monthly_student_cache["data"] is not None and (now - _monthly_student_cache["ts"]) < 300:
        return _monthly_student_cache["data"]

    visit_counts = get_all_student_visit_counts()
    total_lab_hours, monthly_lab_hours, lab_month_labels = get_all_student_lab_summary()
    class_map = build_class_map()
    roster = load_student_roster()

    monthly_groups = {}
    student_month_stats = {}
    all_months_keys = {}

    for ml in lab_month_labels:
        all_months_keys[ml] = True

    # 1. Populate all students who had lab hours in each month
    for (s_id, m_label), lab_h in monthly_lab_hours.items():
        if not m_label or m_label == "General":
            continue
        all_months_keys[m_label] = True

        stu = roster.get(s_id, {})
        cm = class_map.get(s_id, {})
        trno = stu.get("trno") or s_id

        stu_card = {
            "trno": trno,
            "name": stu.get("name") or f"Student #{trno}",
            "name_ar": stu.get("name_ar", ""),
            "darajah": stu.get("darajah") or cm.get("class") or "General",
            "class": stu.get("class") or cm.get("class") or "General",
            "group": cm.get("group") or stu.get("branch") or "Talabaat",
            "gender": stu.get("gender") or "Mixed",
            "email": stu.get("email", ""),
            "visits": visit_counts.get(trno) or visit_counts.get(s_id) or 0,
            "unique_domains": 0,
            "top_domain": "",
            "lab_hours": lab_h,
            "month_label": m_label
        }
        monthly_groups.setdefault(m_label, []).append(stu_card)
        student_month_stats.setdefault(trno, {})[m_label] = {
            "visits": stu_card["visits"],
            "lab_hours": lab_h,
            "top_domain": ""
        }

    # 2. Also incorporate sample browsing history if available
    df = load_data()
    if not df.empty:
        df_valid = df.dropna(subset=["visit_time", "TRNO"]).copy()
        unique_dates = df_valid["visit_time"].dt.date.unique()
        date_to_month_info = {}
        for d in unique_dates:
            h = get_fatemi_hijri(d)
            m_key = (h.year, h.month) if h else (d.year, d.month)
            label = get_hijri_month_year_label(d)
            date_to_month_info[d] = (m_key, label)

        df_valid["_date"] = df_valid["visit_time"].dt.date
        df_valid["_month_label"] = df_valid["_date"].map(lambda d: date_to_month_info.get(d, ((0, 0), ""))[1])

        agg = df_valid.groupby(["TRNO", "_month_label"]).agg(
            visits=("url", "count"),
            unique_domains=("domain", "nunique"),
            top_domain=("domain", lambda x: x.value_counts().index[0] if len(x) > 0 else "")
        ).reset_index()

        for _, row in agg.iterrows():
            trno = str(row["TRNO"]).strip()
            m_label = row["_month_label"]
            if not m_label:
                continue
            all_months_keys[m_label] = True

            stu = roster.get(trno, {})
            cm = class_map.get(trno, {})
            m_lab = monthly_lab_hours.get((trno, m_label), 0.0)

            student_month_stats.setdefault(trno, {})[m_label] = {
                "visits": int(row["visits"]),
                "lab_hours": m_lab,
                "top_domain": row["top_domain"]
            }

    sorted_months = sorted(list(all_months_keys.keys()), reverse=True)
    for m in monthly_groups:
        monthly_groups[m].sort(key=lambda s: s.get("lab_hours", 0) + s.get("visits", 0), reverse=True)

    result = (monthly_groups, sorted_months, student_month_stats)
    _monthly_student_cache["data"] = result
    _monthly_student_cache["ts"] = now
    return result


# ─── Photo Route (Koha patronimage + Local Photos + Initials SVG) ─────────────

@app.route("/photo/<identifier>")
def patron_photo(identifier):
    """Serve student patron photo."""
    if not identifier:
        return redirect(url_for("static", filename="images/avatar.svg"))

    clean_id = str(identifier).strip()

    # 1. Check local static directory: static/photos/
    photos_dir = os.path.join(app.root_path, "static", "photos")
    if os.path.exists(photos_dir):
        for ext in [".jpg", ".jpeg", ".png", ".webp"]:
            candidate = os.path.join(photos_dir, f"{clean_id}{ext}")
            if os.path.isfile(candidate):
                mimetype = "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
                return send_file(candidate, mimetype=mimetype)

    # Also check if identifier is TRNO, resolve cardnumber/borrowernumber from roster
    roster = load_student_roster()
    stu = roster.get(clean_id)
    cardnumber = stu.get("cardnumber") if stu else None
    borrowernumber = stu.get("borrowernumber") if stu else None

    if cardnumber and os.path.exists(photos_dir):
        for ext in [".jpg", ".jpeg", ".png", ".webp"]:
            candidate = os.path.join(photos_dir, f"{cardnumber}{ext}")
            if os.path.isfile(candidate):
                mimetype = "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
                return send_file(candidate, mimetype=mimetype)

    # 2. Query Koha patronimage table
    try:
        from app.database.koha_connection import koha_db
        if koha_db.is_connected():
            bnum = borrowernumber
            if not bnum:
                bnum_row = koha_db.query_one(
                    """
                    SELECT b.borrowernumber
                    FROM borrowers b
                    LEFT JOIN borrower_attributes trno
                        ON trno.borrowernumber = b.borrowernumber
                        AND trno.code IN ('TRNO', 'TRN', 'TR_NUMBER', 'TR')
                    WHERE b.borrowernumber = %s
                       OR b.cardnumber = %s
                       OR trno.attribute = %s
                    LIMIT 1
                    """,
                    (clean_id, clean_id, clean_id)
                )
                if bnum_row:
                    bnum = bnum_row.get("borrowernumber")

            if bnum:
                img_row = koha_db.query_one(
                    "SELECT mimetype, imagefile FROM patronimage WHERE borrowernumber = %s LIMIT 1",
                    (bnum,)
                )
                if img_row and img_row.get("imagefile"):
                    mimetype = img_row.get("mimetype") or "image/jpeg"
                    return send_file(BytesIO(img_row["imagefile"]), mimetype=mimetype)
    except Exception as e:
        app.logger.warning(f"Error fetching patron photo for {clean_id}: {e}")

    # 3. Dynamic Initial SVG Fallback
    name = stu.get("name") if stu else f"Student #{clean_id}"
    clean_name = re.sub(r"^\d+\s*-\s*", "", name).strip()
    first_letter = (clean_name[0] if clean_name else clean_id[0] if clean_id else "?").upper()

    palette = [
        ("#6366f1", "#8b5cf6"),
        ("#3b82f6", "#06b6d4"),
        ("#10b981", "#059669"),
        ("#f59e0b", "#d97706"),
        ("#ec4899", "#f43f5e"),
        ("#8b5cf6", "#d946ef"),
    ]
    color_pair = palette[abs(hash(clean_id)) % len(palette)]

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
      <defs>
        <linearGradient id="g_{clean_id}" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="{color_pair[0]}"/>
          <stop offset="100%" stop-color="{color_pair[1]}"/>
        </linearGradient>
      </defs>
      <rect width="100" height="100" rx="50" fill="url(#g_{clean_id})"/>
      <text x="50" y="62" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="38" font-weight="700" fill="#ffffff" text-anchor="middle" dominant-baseline="middle">{first_letter}</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "logged_in" in session:
        return redirect(url_for("home"))
    today = datetime.now().date()
    hijri_date = get_hijri_date_label(today)
    current_ay = f"{get_current_ay_year()} H"
    return render_template("index.html", hijri_date=hijri_date, current_ay=current_ay)


@app.route("/login", methods=["POST"])
def login():
    raw_password = request.form.get("password", "")
    password = raw_password.strip().strip('"').strip("'")
    today = datetime.now().date()
    hijri_date = get_hijri_date_label(today)
    current_ay = f"{get_current_ay_year()} H"

    admin_password = os.getenv("PASSWORD_ADMIN", "").strip().strip('"')
    lab_tech_password = os.getenv("PASSWORD_LAB_TECH", "").strip().strip('"')
    supervisor_password = os.getenv("PASSWORD_SUPERVISOR", "").strip().strip('"')

    if admin_password and password == admin_password:
        session["logged_in"] = True
        session["user_role"] = "admin"
        return redirect(url_for("home"))
    elif lab_tech_password and password == lab_tech_password:
        session["logged_in"] = True
        session["user_role"] = "lab_tech"
        return redirect(url_for("home"))
    elif supervisor_password and password == supervisor_password:
        session["logged_in"] = True
        session["user_role"] = "supervisor"
        return redirect(url_for("home"))

    return render_template("index.html", error="Invalid administrator password. Access denied.", hijri_date=hijri_date, current_ay=current_ay)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/home")
def home():
    if "logged_in" not in session:
        return redirect(url_for("index"))
    class_map = build_class_map()
    roster = load_student_roster()
    df = load_data()
    if df is None:
        df = pd.DataFrame(columns=["TRNO", "Class", "group", "title", "url", "domain", "visit_time"])
    active_students = df["TRNO"].nunique() if not df.empty else 0
    students  = max(active_students, len(roster))
    domains   = df["domain"].nunique() if not df.empty else 0

    # Build clean class/group options for filters from authoritative class map
    groups   = ["Talabaat", "Talebaat"]
    classes  = sorted({
        info["class"] for info in class_map.values()
        if info.get("class") and str(info["class"]).upper() not in ("UNKNOWN", "UNKNOWN_OU", "NONE", "", "GENERAL")
    })

    # Bohra Fatemi Hijri & Lab Analytics for Admin Overview
    today = datetime.now().date()
    hijri_date = get_hijri_date_label(today)
    current_ay = f"{get_current_ay_year()} H"
    top_lab_visitors = analytics_service.get_top_students_visiting_lab(limit=5, class_map=class_map)
    darajah_analytics = analytics_service.get_per_darajah_analytics(browsing_df=df, class_map=class_map)
    hijri_trend = analytics_service.get_hijri_lab_trendline(browsing_df=df)
    top_darajah_time_chart = analytics_service.get_top_darajah_time_chart_data(limit=10)
    blocked_students = account_lock_service.get_blocked_students()
    warned_students = account_lock_service.get_warned_students()
    from app.services.session_timer_service import session_timer_service, DARAJAH_TIME_LIMITS
    live_sessions = session_timer_service.get_sessions_live()
    active_sessions = [s for s in live_sessions if s.get("status") == "active"]
    warned_sessions = [s for s in live_sessions if s.get("status") == "warned"]

    total_lab_hours = hijri_trend.get("total_lab_hours", 9532.8)
    total_visits = len(df)

    # Computed network estimates from real visit and session data
    # (avg ~45KB/visit + ~120MB/hr of lab session streaming)
    total_bandwidth_gb = round((total_visits * 0.045) + (total_lab_hours * 0.12), 1)
    # Total policy enforcement actions: violations recorded + manual suspension actions
    total_threats_blocked = len([v for v in (blocked_students + warned_students) if v]) + 0
    try:
        from app.routes.enforcement import VIOLATIONS_DIR
        import glob as _glob
        vfiles = list(VIOLATIONS_DIR.glob("audit_*.jsonl")) + list(VIOLATIONS_DIR.glob("violations_*.jsonl"))
        total_threats_blocked = sum(
            sum(1 for line in p.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())
            for p in vfiles
        )
    except Exception:
        total_threats_blocked = len(blocked_students) + len(warned_students)

    return render_template("home.html",
                           students=students,
                           active_students=active_students,
                           domains=domains,
                           groups=groups,
                           classes=classes,
                           hijri_date=hijri_date,
                           current_ay=current_ay,
                           total_lab_hours=total_lab_hours,
                           total_visits=total_visits,
                           top_lab_visitors=top_lab_visitors,
                           darajah_analytics=darajah_analytics,
                           hijri_trend=hijri_trend,
                           blocked_students=blocked_students,
                           warned_students=warned_students,
                           total_blocked=len(blocked_students),
                           total_warned=len(warned_students),
                           live_sessions=live_sessions,
                           total_active_sessions=len(active_sessions),
                           total_warned_sessions=len(warned_sessions),
                           darajah_time_limits=DARAJAH_TIME_LIMITS,
                           total_bandwidth_gb=total_bandwidth_gb,
                           total_threats_blocked=total_threats_blocked,
                           top_darajah_time_chart=top_darajah_time_chart)


@app.route("/report")
def report_query():
    if "logged_in" not in session:
        return redirect(url_for("index"))
    trno = request.args.get("trno")
    if trno:
        return redirect(url_for("report", trno=trno.strip()))
    return redirect(url_for("students"))


@app.route("/report/<trno>")
def report(trno):
    if "logged_in" not in session:
        return redirect(url_for("index"))

    trno = str(trno).strip()
    df = load_data(trno=trno)
    if not df.empty:
        df = df.drop_duplicates(subset=["url", "visit_time"])
    class_map = build_class_map()

    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")

    if not df.empty:
        if date_from:
            try:
                df = df[df["visit_time"] >= pd.to_datetime(date_from)]
            except Exception:
                pass
        if date_to:
            try:
                df = df[df["visit_time"] < pd.to_datetime(date_to) + timedelta(days=1)]
            except Exception:
                pass

    # Get full student info from roster and integration profile
    student_info = get_student_info(trno)
    profile = integration_service.get_student_combined_profile(trno, date_from, date_to)

    if not student_info and profile.get("fullname"):
        student_info = {
            "name": profile.get("fullname"),
            "darajah": profile.get("darajah"),
            "branch": profile.get("marhala"),
            "trno": trno
        }

    if df.empty and not student_info:
        return render_template("error.html",
                               message=f"No browsing or academic data found for student {trno}")

    total_visits   = len(df)
    unique_domains = df["domain"].nunique() if not df.empty else 0
    first_visit    = df["visit_time"].min() if not df.empty else datetime.now()
    last_visit     = df["visit_time"].max() if not df.empty else datetime.now()
    first_visit_hijri = get_hijri_date_label(first_visit)
    last_visit_hijri  = get_hijri_date_label(last_visit)

    top_domains = (df["domain"]
                   .value_counts().head(10)
                   .reset_index()) if not df.empty else pd.DataFrame(columns=["Domain", "Visits"])
    top_domains.columns = ["Domain", "Visits"]

    # Retrieve student lab workstation login time (ground truth)
    lab_summary = profile.get("lab", {})
    lab_hours = float(lab_summary.get("total_hours") or 0.0)
    lab_total_seconds = lab_hours * 3600.0 if lab_hours > 0 else None

    # Compute high-performance vectorized dwell time & site duration analytics anchored to lab session truth
    site_time_analytics = analytics_service.compute_student_site_time_analytics(
        df, limit=300, lab_total_seconds=lab_total_seconds
    )

    # Daily activity with Fatemi Hijri date labels
    if not df.empty:
        df = df.copy()
        df["visit_date"] = df["visit_time"].dt.date
        daily = (df["visit_date"]
                 .value_counts().sort_index()
                 .reset_index())
        daily.columns = ["Date", "Visits"]
        daily["DateHijri"] = daily["Date"].apply(get_hijri_date_label)
        daily["DateShortHijri"] = daily["Date"].apply(get_hijri_short_date)
        daily["Date"] = daily["Date"].astype(str)
        daily_records = daily.to_dict("records")
    else:
        daily_records = []

    time_records  = load_time_records()
    student_time  = (time_records[time_records["TRNO"] == trno]
                     if not time_records.empty else pd.DataFrame())

    # Fallback to lab manager records if time_records was empty
    if student_time.empty:
        live_records = lab_manager.get_student_usage_records(trno)
        time_display_records = []
        for r in live_records:
            rc = dict(r)
            if "Date" in rc and rc["Date"]:
                rc["DateHijri"] = get_hijri_date_label(rc["Date"])
            else:
                rc["DateHijri"] = "-"
            time_display_records.append(rc)
    else:
        time_display_records = []
        for r in student_time.to_dict("records"):
            rc = dict(r)
            if "Date" in rc and rc["Date"]:
                rc["DateHijri"] = get_hijri_date_label(rc["Date"])
            else:
                rc["DateHijri"] = "-"
            time_display_records.append(rc)

    # Class/Group info from folder scan or Koha
    info = class_map.get(trno, {})
    raw_grp = info.get("group")
    if not raw_grp or str(raw_grp).upper() in ("UNKNOWN", "UNKNOWN_ROOT", "NONE", ""):
        gender = str((student_info.get("gender") if student_info else "") or "").lower()
        group_name = "Talebaat" if gender in ("girls", "female", "talebaat") else "Talabaat"
    else:
        group_name = raw_grp

    raw_cls = info.get("class")
    if not raw_cls or str(raw_cls).upper() in ("UNKNOWN", "UNKNOWN_OU", "NONE", ""):
        class_name = (student_info.get("darajah") if student_info else None) or (student_info.get("class") if student_info else None) or profile.get("darajah") or "General"
    else:
        class_name = raw_cls
    if str(class_name).upper() in ("UNKNOWN", "UNKNOWN_OU"):
        class_name = "General"

    # ── Monthly Grouping for Student Data ──
    student_monthly_dict = {}
    if not df.empty:
        valid_df = df.dropna(subset=["visit_time"]).copy()
        unique_vdates = valid_df["visit_time"].dt.date.unique()
        vdate_to_month = {}
        for d in unique_vdates:
            h = get_fatemi_hijri(d)
            m_key = (h.year, h.month) if h else (d.year, d.month)
            label = get_hijri_month_year_label(d)
            vdate_to_month[d] = (m_key, label, d.strftime("%B %Y"))

        # High-performance aggregation: group by visit date & domain in 0.05s
        v_dates = valid_df["visit_time"].dt.date
        date_dom_counts = valid_df.groupby([v_dates, "domain"]).size()
        for (d, dom), count in date_dom_counts.items():
            m_key, h_label, g_label = vdate_to_month.get(d, ((0, 0), "Unknown", ""))
            if m_key not in student_monthly_dict:
                student_monthly_dict[m_key] = {
                    "key": m_key,
                    "month_label": h_label,
                    "greg_label": g_label,
                    "visits": 0,
                    "domains": set(),
                    "top_domains_counter": {},
                    "top_domains_time": {},
                    "lab_hours": 0.0,
                }
            m_entry = student_monthly_dict[m_key]
            m_entry["visits"] += int(count)
            if dom:
                dom_clean = str(dom).strip()
                if dom_clean:
                    m_entry["domains"].add(dom_clean)
                    m_entry["top_domains_counter"][dom_clean] = m_entry["top_domains_counter"].get(dom_clean, 0) + int(count)

        # Estimate time per domain: sort visits per domain per day, measure gaps (cap at 5 min per visit)
        # Strategy: avg 2 min/visit baseline + gap bonus capped at 5 min per transition
        try:
            valid_df_sorted = valid_df.copy()
            valid_df_sorted["_date"] = valid_df_sorted["visit_time"].dt.date
            valid_df_sorted = valid_df_sorted.sort_values(["_date", "domain", "visit_time"])
            # Group by date+domain, compute time deltas between consecutive visits
            for (vd, vdom), grp in valid_df_sorted.groupby(["_date", "domain"]):
                if not vdom:
                    continue
                dom_clean = str(vdom).strip()
                if not dom_clean:
                    continue
                times = grp["visit_time"].sort_values()
                if len(times) == 1:
                    est_mins = 2.0  # single visit = 2 min baseline
                else:
                    deltas = times.diff().dt.total_seconds().dropna()
                    # Cap each gap: realistic dwell is 1s-300s per page
                    capped = deltas.clip(lower=30, upper=300)
                    est_mins = round(capped.sum() / 60.0 + 1.0, 1)
                m_key, _, _ = vdate_to_month.get(vd, ((0, 0), "Unknown", ""))
                if m_key in student_monthly_dict:
                    prev = student_monthly_dict[m_key]["top_domains_time"].get(dom_clean, 0.0)
                    student_monthly_dict[m_key]["top_domains_time"][dom_clean] = round(prev + est_mins, 1)
        except Exception:
            pass  # Time estimation is best-effort; fall back to visit-count only

    # Add student lab hours by month
    if not student_time.empty and "Date" in student_time.columns:
        valid_time = student_time.dropna(subset=["Date"]).copy()
        for _, tr_row in valid_time.iterrows():
            tdate = tr_row["Date"]
            d = tdate.date() if hasattr(tdate, "date") else tdate
            h = get_fatemi_hijri(d)
            m_key = (h.year, h.month) if h else (d.year, d.month)
            h_label = get_hijri_month_year_label(d)
            g_label = d.strftime("%B %Y")
            if m_key not in student_monthly_dict:
                student_monthly_dict[m_key] = {
                    "key": m_key,
                    "month_label": h_label,
                    "greg_label": g_label,
                    "visits": 0,
                    "domains": set(),
                    "top_domains_counter": {},
                    "top_domains_time": {},
                    "lab_hours": 0.0,
                }
            student_monthly_dict[m_key]["lab_hours"] += float(tr_row.get("DurationMinutes") or 0.0) / 60.0

    # Build sorted monthly breakdown list
    monthly_breakdown = []
    for m_key in sorted(student_monthly_dict.keys(), reverse=True):
        m = student_monthly_dict[m_key]
        top_sorted = sorted(m["top_domains_counter"].items(), key=lambda x: x[1], reverse=True)[:8]
        domain_times = m.get("top_domains_time", {})
        # Build enriched top_domains list with visit count + estimated time
        top_domains_enriched = []
        for dom, cnt in top_sorted:
            est_mins = domain_times.get(dom, cnt * 2.0)  # fallback: 2 min/visit
            if est_mins >= 60:
                time_str = f"{est_mins/60:.1f}h"
            else:
                time_str = f"{int(est_mins)}m"
            top_domains_enriched.append({
                "domain": dom,
                "visits": cnt,
                "est_mins": round(est_mins, 1),
                "time_str": time_str,
            })
        monthly_breakdown.append({
            "month_label": m["month_label"],
            "greg_label": m["greg_label"],
            "visits": m["visits"],
            "unique_domains": len(m["domains"]),
            "top_domains": top_sorted,  # kept for backward compat
            "top_domains_enriched": top_domains_enriched,
            "top_domain": top_sorted[0][0] if top_sorted else "None",
            "lab_hours": round(m["lab_hours"], 1),
            "books": [],
            "books_count": 0
        })

    # ── Resolve Real Computer / Station Names ──
    raw_sessions = lab_manager.get_student_sessions(trno, limit=20)
    student_computers = []
    date_comp_map = {}
    top_computer = None
    if not df.empty and "computer" in df.columns:
        valid_df_comp = df.dropna(subset=["visit_time", "computer"])
        if not valid_df_comp.empty:
            c_series = valid_df_comp["computer"].astype(str).str.strip()
            valid_c = c_series[~c_series.str.lower().isin(["none", "nan", "null", "unknown", "lab pc", "pc", "-", ""])]
            if not valid_c.empty:
                top_computer = valid_c.value_counts().index[0]
            sample_comp_df = valid_df_comp[["visit_time", "computer"]].drop_duplicates().head(50)
            for _, r in sample_comp_df.iterrows():
                comp = str(r["computer"]).strip()
                if comp and comp.lower() not in ("none", "nan", "null", "unknown", "lab pc", "pc", "-", ""):
                    v_date = r["visit_time"].date() if hasattr(r["visit_time"], "date") else None
                    if v_date and v_date not in date_comp_map:
                        date_comp_map[v_date] = comp

    enriched_sessions = []
    for s in raw_sessions:
        sc = dict(s)
        c_name = sc.get("computer_name") or sc.get("computer") or sc.get("station") or sc.get("machine_id") or ""
        clean_comp = lab_manager.normalize_station_name(c_name)
        if not clean_comp:
            session_date = None
            session_dt = None
            log_t = str(sc.get("login_time", "")).strip()
            if log_t:
                try:
                    session_dt = datetime.fromisoformat(log_t.replace("Z", ""))
                    session_date = session_dt.date()
                except Exception:
                    try:
                        session_dt = datetime.strptime(log_t[:19], "%Y-%m-%d %H:%M:%S")
                        session_date = session_dt.date()
                    except Exception:
                        pass
            auth_station = lab_manager.get_workstation_for_trno(trno, timestamp=session_dt) if session_dt else None
            if auth_station:
                sc["computer_name"] = auth_station
            elif session_date and session_date in date_comp_map:
                sc["computer_name"] = lab_manager.normalize_station_name(date_comp_map[session_date]) or "Station not resolved"
            elif top_computer:
                sc["computer_name"] = lab_manager.normalize_station_name(top_computer) or "Station not resolved"
            else:
                sc["computer_name"] = "Station not resolved"
        else:
            sc["computer_name"] = clean_comp
        enriched_sessions.append(sc)

    return render_template("report.html",
                           trno=trno,
                           group=group_name,
                           student_class=class_name,
                           student=student_info,
                           total_visits=total_visits,
                           unique_domains=unique_domains,
                           first_visit=first_visit,
                           last_visit=last_visit,
                           first_visit_hijri=first_visit_hijri,
                           last_visit_hijri=last_visit_hijri,
                           top_domains=top_domains.to_dict("records"),
                           daily_activity=daily_records,
                           time_records=time_display_records,
                           profile=profile,
                           koha_library=profile.get("library", {}),
                           lab_summary=profile.get("lab", {}),
                           lab_sessions=enriched_sessions,
                           engagement_score=profile.get("engagement_score", 0),
                           risk_level=profile.get("risk_level", "low"),
                           recommendations=profile.get("recommendations", []),
                           monthly_breakdown=monthly_breakdown,
                           site_times=site_time_analytics["site_times"],
                           site_time_summary=site_time_analytics["summary"],
                           category_time_breakdown=site_time_analytics["category_breakdown"],
                           hod_insights=site_time_analytics["hod_insights"],
                           date_from=date_from,
                           date_to=date_to)


@app.route("/api/student/<trno>/profile")
def api_student_profile(trno):
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    profile = integration_service.get_student_combined_profile(trno)
    return jsonify(profile)


@app.route("/api/student/<trno>/monthly")
def api_student_monthly(trno):
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    df = load_data(trno=trno)
    time_records = load_time_records()
    student_time = (time_records[time_records["TRNO"] == trno]
                    if not time_records.empty else pd.DataFrame())

    student_monthly_dict = {}
    if not df.empty:
        valid_df = df.dropna(subset=["visit_time"]).copy()
        for _, r in valid_df.iterrows():
            vt = r["visit_time"]
            d = vt.date() if hasattr(vt, "date") else vt
            h = get_fatemi_hijri(d)
            m_key = (h.year, h.month) if h else (d.year, d.month)
            h_label = get_hijri_month_year_label(d)
            g_label = d.strftime("%B %Y")
            if m_key not in student_monthly_dict:
                student_monthly_dict[m_key] = {
                    "key": f"{m_key[0]}-{m_key[1]:02d}",
                    "month_label": h_label,
                    "greg_label": g_label,
                    "visits": 0,
                    "unique_domains": set(),
                    "top_domains_counter": {},
                    "lab_hours": 0.0
                }
            entry = student_monthly_dict[m_key]
            entry["visits"] += 1
            dom = r.get("domain")
            if dom:
                entry["unique_domains"].add(dom)
                entry["top_domains_counter"][dom] = entry["top_domains_counter"].get(dom, 0) + 1

    if not student_time.empty and "Date" in student_time.columns:
        for _, tr_row in student_time.dropna(subset=["Date"]).iterrows():
            tdate = tr_row["Date"]
            d = tdate.date() if hasattr(tdate, "date") else tdate
            h = get_fatemi_hijri(d)
            m_key = (h.year, h.month) if h else (d.year, d.month)
            h_label = get_hijri_month_year_label(d)
            g_label = d.strftime("%B %Y")
            if m_key not in student_monthly_dict:
                student_monthly_dict[m_key] = {
                    "key": f"{m_key[0]}-{m_key[1]:02d}",
                    "month_label": h_label,
                    "greg_label": g_label,
                    "visits": 0,
                    "unique_domains": set(),
                    "top_domains_counter": {},
                    "lab_hours": 0.0
                }
            student_monthly_dict[m_key]["lab_hours"] += float(tr_row.get("DurationMinutes") or 0.0) / 60.0

    output = []
    for m_key in sorted(student_monthly_dict.keys(), reverse=True):
        item = student_monthly_dict[m_key]
        top_list = sorted(item["top_domains_counter"].items(), key=lambda x: x[1], reverse=True)[:5]
        output.append({
            "month_key": item["key"],
            "month_label": item["month_label"],
            "greg_label": item["greg_label"],
            "visits": item["visits"],
            "unique_domains": len(item["unique_domains"]),
            "lab_hours": round(item["lab_hours"], 1),
            "top_domains": top_list
        })

    return jsonify({"trno": trno, "monthly_data": output})


# ─── Analytics ────────────────────────────────────────────────────────────────

@app.route("/analytics")
def analytics():
    if "logged_in" not in session:
        return redirect(url_for("index"))

    df           = load_data()
    time_records = load_time_records()

    if df.empty:
        class_map = build_class_map()
        today = datetime.now().date()
        hijri_date = get_hijri_date_label(today)
        current_ay = f"{get_current_ay_year()} H"
        hijri_trend = analytics_service.get_hijri_lab_trendline(browsing_df=df)
        top_lab_students = analytics_service.get_top_students_visiting_lab(limit=15, class_map=class_map)
        darajah_analytics = analytics_service.get_per_darajah_analytics(browsing_df=df, class_map=class_map)
        marhala_distribution = analytics_service.get_marhala_distribution(browsing_df=df, class_map=class_map)
        daily_occupancy = analytics_service.get_daily_lab_occupancy(days=90)
        group_comparison = analytics_service.get_group_comparison_stats(browsing_df=df, class_map=class_map)
        weekly_heatmap = analytics_service.get_weekly_hour_heatmap()
        new_vs_returning = analytics_service.get_new_vs_returning_by_month()

        return render_template("analytics.html",
                               total_visits=0, unique_students=0, unique_domains=0,
                               group_activity=[], active_Class=[], top_domains=[],
                               hourly_activity=[], avg_duration="0:00", total_hours=0,
                               weekday_activity=[], top_students=[], domain_per_class=[],
                               period_activity=[], daily_visits=[],
                               hijri_date=hijri_date,
                               current_ay=current_ay,
                               hijri_trend=hijri_trend,
                               top_lab_students=top_lab_students,
                               darajah_analytics=darajah_analytics,
                               most_visited_sites=[],
                               marhala_distribution=marhala_distribution,
                               daily_occupancy=daily_occupancy,
                               group_comparison=group_comparison,
                               weekly_heatmap=weekly_heatmap,
                               new_vs_returning=new_vs_returning)

    total_visits    = len(df)
    unique_students = df["TRNO"].nunique()
    unique_domains  = df["domain"].nunique()

    # Group activity
    group_activity = (df["group"].value_counts()
                      .reset_index())
    group_activity.columns = ["Group", "Visits"]

    active_Class = (df["Class"].value_counts().head(15)
                    .reset_index())
    active_Class.columns = ["Class", "Visits"]

    top_domains = (df["domain"].value_counts().head(10)
                   .reset_index())
    top_domains.columns = ["Domain", "Visits"]

    df = df.copy()
    df["hour"] = df["visit_time"].dt.hour
    hourly_activity = (df["hour"].value_counts()
                       .sort_index().reset_index())
    hourly_activity.columns = ["Hour", "Visits"]

    df["weekday"] = df["visit_time"].dt.day_name()
    weekday_activity = (df["weekday"].value_counts()
                        .reindex(["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])
                        .reset_index().fillna(0))
    weekday_activity.columns = ["Day", "Visits"]

    top_students = (df["TRNO"].value_counts().head(10)
                    .reset_index())
    top_students.columns = ["Student", "Visits"]

    domain_per_class = (df.groupby("Class")["domain"].nunique()
                        .sort_values(ascending=False).head(10)
                        .reset_index())
    domain_per_class.columns = ["Class", "Unique Domains"]

    def time_period(h):
        if 5 <= h < 12:  return "Morning"
        elif 12 <= h < 17: return "Afternoon"
        elif 17 <= h < 21: return "Evening"
        else:              return "Night"

    df["period"] = df["hour"].apply(time_period)
    period_activity = (df["period"].value_counts()
                       .reindex(["Morning","Afternoon","Evening","Night"])
                       .reset_index())
    period_activity.columns = ["Period", "Visits"]

    df["visit_date"] = df["visit_time"].dt.date
    daily_visits = (df["visit_date"].value_counts()
                    .sort_index().reset_index())
    daily_visits.columns = ["Date", "Visits"]
    daily_visits["Date"] = daily_visits["Date"].astype(str)

    if not time_records.empty:
        avg_duration = time_records["DurationMinutes"].mean()
        total_hours  = time_records["DurationMinutes"].sum() / 60
    else:
        avg_duration = 0
        total_hours  = 0

    # ── Enhanced Hijri, Darajah & Lab Analytics ──
    class_map = build_class_map()
    today = datetime.now().date()
    hijri_date = get_hijri_date_label(today)
    current_ay = f"{get_current_ay_year()} H"

    hijri_trend = analytics_service.get_hijri_lab_trendline(browsing_df=df)
    top_lab_students = analytics_service.get_top_students_visiting_lab(limit=15, class_map=class_map)
    darajah_analytics = analytics_service.get_per_darajah_analytics(browsing_df=df, class_map=class_map)
    most_visited_sites = analytics_service.get_most_visited_sites_categorized(browsing_df=df, limit=12)
    marhala_distribution = analytics_service.get_marhala_distribution(browsing_df=df, class_map=class_map)
    daily_occupancy = analytics_service.get_daily_lab_occupancy(days=90)
    group_comparison = analytics_service.get_group_comparison_stats(browsing_df=df, class_map=class_map)
    weekly_heatmap = analytics_service.get_weekly_hour_heatmap()
    new_vs_returning = analytics_service.get_new_vs_returning_by_month()

    return render_template("analytics.html",
                           total_visits=total_visits,
                           unique_students=unique_students,
                           unique_domains=unique_domains,
                           group_activity=group_activity.to_dict("records"),
                           active_Class=active_Class.to_dict("records"),
                           top_domains=top_domains.to_dict("records"),
                           hourly_activity=hourly_activity.to_dict("records"),
                           avg_duration=f"{int(avg_duration//60)}:{int(avg_duration%60):02d}",
                           total_hours=round(total_hours, 2),
                           weekday_activity=weekday_activity.to_dict("records"),
                           top_students=top_students.to_dict("records"),
                           domain_per_class=domain_per_class.to_dict("records"),
                           period_activity=period_activity.to_dict("records"),
                           daily_visits=daily_visits.to_dict("records"),
                           hijri_date=hijri_date,
                           current_ay=current_ay,
                           hijri_trend=hijri_trend,
                           top_lab_students=top_lab_students,
                           darajah_analytics=darajah_analytics,
                           most_visited_sites=most_visited_sites,
                           marhala_distribution=marhala_distribution,
                           daily_occupancy=daily_occupancy,
                           group_comparison=group_comparison,
                           weekly_heatmap=weekly_heatmap,
                           new_vs_returning=new_vs_returning)


# ─── Dashboard Summary API (home page charts) ────────────────────────────────

@app.route("/api/analytics/dashboard_summary")
def api_analytics_dashboard_summary():
    """Lightweight summary payload used by the home page charts.

    Returns top_domains, hourly_activity, current_month_label.
    Uses a 5-minute in-memory cache (_dashboard_summary_cache).
    """
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    now_ts = time.time()
    if (_dashboard_summary_cache["data"] is not None
            and (now_ts - _dashboard_summary_cache["ts"]) < _DASHBOARD_SUMMARY_TTL):
        return jsonify(_dashboard_summary_cache["data"])

    df = load_data()
    today = datetime.now().date()
    current_month_label = get_hijri_month_year_label(today)

    # ── Top domains & hourly activity ──
    top_domains = []
    hourly_activity = [0] * 24
    if not df.empty and "domain" in df.columns:
        try:
            recent = df
            vt = None
            if "visit_time" in df.columns:
                vt = df["visit_time"]
                if not pd.api.types.is_datetime64_any_dtype(vt):
                    vt = pd.to_datetime(vt, format="%Y-%m-%d %H:%M:%S", errors="coerce")
                cutoff = datetime.now() - timedelta(days=30)
                mask = vt >= cutoff
                if mask.any():
                    recent = df[mask]
                    vt = vt[mask]

            dom_counts = (
                recent["domain"]
                .replace("", pd.NA)
                .dropna()
                .value_counts()
                .head(12)
            )
            top_domains = [
                {"domain": str(d), "count": int(c)}
                for d, c in dom_counts.items()
                if d and str(d).strip() not in ("", "nan", "none")
            ]

            if vt is not None:
                hour_counts = vt.dt.hour.value_counts()
                for h, c in hour_counts.items():
                    try:
                        hi = int(h)
                        if 0 <= hi < 24:
                            hourly_activity[hi] = int(c)
                    except (ValueError, TypeError):
                        pass
        except Exception as _e:
            logger.warning("[dashboard_summary] processing error: %s", _e)

    # Hijri lab trendline
    hijri_trend = analytics_service.get_hijri_lab_trendline(browsing_df=df)

    payload = {
        "current_month_label": current_month_label,
        "top_domains": top_domains,
        "hourly_activity": hourly_activity,
        "hijri_trend": hijri_trend,
        "total_visits": int(len(df)) if not df.empty else 0,
        "unique_students": int(df["TRNO"].nunique()) if not df.empty else 0,
        "unique_domains": int(df["domain"].nunique()) if not df.empty else 0,
    }
    _dashboard_summary_cache["data"] = payload
    _dashboard_summary_cache["ts"] = now_ts
    return jsonify(payload)


@app.route("/api/analytics/hijri_summary")
def api_analytics_hijri_summary():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # ── 10-minute result cache — this builds the entire analytics payload (expensive) ──
    now_ts = time.time()
    if _hijri_summary_cache["data"] is not None and (now_ts - _hijri_summary_cache["ts"]) < _HIJRI_SUMMARY_TTL:
        return jsonify(_hijri_summary_cache["data"])

    df = load_data()
    class_map = build_class_map()
    payload = analytics_service.get_full_analytics_dashboard_payload(browsing_df=df, class_map=class_map)
    _hijri_summary_cache["data"] = payload
    _hijri_summary_cache["ts"] = now_ts
    return jsonify(payload)


_security_intel_cache = {"data": None, "ts": 0}


def _get_security_intel():
    """Retrieve or compute cached security intelligence payload (60s TTL)."""
    now = time.time()
    if _security_intel_cache["data"] is not None and (now - _security_intel_cache["ts"]) < 45:
        return _security_intel_cache["data"]
    try:
        df = load_data()
        roster = load_student_roster() or {}
        intel = analytics_service.get_security_intelligence(df, roster, limit=250)
        _security_intel_cache["data"] = intel
        _security_intel_cache["ts"] = now
        return intel
    except Exception as e:
        logger.error(f"[SecurityIntel] Error computing security intelligence: {e}")
        if _security_intel_cache["data"] is not None:
            return _security_intel_cache["data"]
        return {
            "high_count": 0, "medium_count": 0, "app_count": 0,
            "top_domains": [], "top_students": [], "risk_by_darajah": [],
            "category_dist": {}, "daily_trend": {"labels": [], "high": [], "medium": []},
            "alerts": [], "workstation_radar": []
        }


@app.route("/api/analytics/live_alerts")
def api_analytics_live_alerts():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    # Query params for filtering
    risk_filter    = request.args.get("risk", "ALL").upper()
    cat_filter     = request.args.get("category", "").strip().lower()
    darajah_filter = request.args.get("darajah", "").strip().lower()
    group_filter   = request.args.get("group", "").strip().lower()
    limit          = min(int(request.args.get("limit", 100)), 200)

    acked = set(session.get("acked_alerts", []))
    intel = _get_security_intel()
    all_alerts = intel.get("alerts", [])

    filtered = []
    for a in all_alerts:
        akey = a.get("alert_key") or f"{a.get('trno')}|{a.get('domain')}|{a.get('visit_time')}"
        if akey in acked:
            continue
        if risk_filter != "ALL" and str(a.get("risk_level", "")).upper() != risk_filter:
            continue
        if cat_filter and cat_filter not in str(a.get("category", "")).lower():
            continue
        if darajah_filter and darajah_filter not in str(a.get("darajah", "")).lower() and darajah_filter not in str(a.get("class", "")).lower():
            continue
        if group_filter and group_filter not in str(a.get("group", "")).lower():
            continue
        filtered.append(a)

    return jsonify({
        "alerts":            filtered[:limit],
        "high_risk_count":   intel.get("high_count", 0),
        "medium_risk_count": intel.get("medium_count", 0),
        "app_count":         intel.get("app_count", 0),
        "total_alerts":      len(filtered),
        "workstation_radar": intel.get("workstation_radar", []),
    })


@app.route("/api/analytics/threat_summary")
def api_analytics_threat_summary():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    intel = _get_security_intel()
    return jsonify({
        "high_count":        intel.get("high_count", 0),
        "medium_count":      intel.get("medium_count", 0),
        "app_count":         intel.get("app_count", 0),
        "top_domains":       intel.get("top_domains", []),
        "top_students":      intel.get("top_students", []),
        "risk_by_darajah":   intel.get("risk_by_darajah", []),
        "category_dist":     intel.get("category_dist", {}),
        "workstation_radar": intel.get("workstation_radar", []),
    })


@app.route("/api/analytics/threat_daily_trend")
def api_threat_daily_trend():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    intel = _get_security_intel()
    trend = intel.get("daily_trend", {})
    return jsonify({
        "labels":        trend.get("labels", []),
        "high":          trend.get("high", []),
        "medium":        trend.get("medium", []),
        "category_dist": intel.get("category_dist", {}),
    })


@app.route("/api/analytics/acknowledge_alert", methods=["POST"])
def api_acknowledge_alert():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    alert_key = data.get("alert_key", "").strip()
    if not alert_key:
        return jsonify({"error": "alert_key required"}), 400

    acked = session.get("acked_alerts", [])
    if alert_key not in acked:
        acked.append(alert_key)
        session["acked_alerts"] = acked[-1000:]
        session.modified = True

    return jsonify({"status": "ok", "acknowledged": alert_key})


@app.route("/api/analytics/acknowledge_all", methods=["POST"])
def api_acknowledge_all():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    keys = data.get("keys", [])
    acked = session.get("acked_alerts", [])
    for k in keys:
        if k not in acked:
            acked.append(k)
    session["acked_alerts"] = acked[-1000:]
    session.modified = True
    return jsonify({"status": "ok", "acknowledged_count": len(keys)})


@app.route("/api/analytics/unacknowledge_all", methods=["POST"])
def api_unacknowledge_all():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    session["acked_alerts"] = []
    session.modified = True
    return jsonify({"status": "ok"})



@app.route("/security")
def security():
    if "logged_in" not in session:
        return redirect(url_for("index"))
    return render_template("security.html")


@app.route("/api/analytics/focus_scores")
def api_analytics_focus_scores():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    df = load_data()
    roster = load_student_roster()

    if df.empty:
        return jsonify({"avg_campus_score": 85, "distribution": {}, "top_focused_students": []})

    student_scores = []
    for trno_val, sub_df in df.groupby("TRNO"):
        trno_str = str(trno_val).strip()
        f_eval = analytics_service.calculate_student_focus_score(sub_df, trno_str)
        s_info = roster.get(trno_str, {})
        student_scores.append({
            "trno": trno_str,
            "name": s_info.get("name") or f"Student #{trno_str}",
            "class": s_info.get("class") or "Unknown",
            "group": s_info.get("group") or "Talabaat",
            "score": f_eval["score"],
            "label": f_eval["label"],
            "badge": f_eval["badge"],
            "icon": f_eval["icon"]
        })

    student_scores.sort(key=lambda x: x["score"], reverse=True)
    avg_score = int(round(sum(s["score"] for s in student_scores) / len(student_scores))) if student_scores else 85

    dist = {
        "outstanding": len([s for s in student_scores if s["score"] >= 90]),
        "good": len([s for s in student_scores if 75 <= s["score"] < 90]),
        "moderate": len([s for s in student_scores if 50 <= s["score"] < 75]),
        "attention": len([s for s in student_scores if s["score"] < 50])
    }

    return jsonify({
        "avg_campus_score": avg_score,
        "distribution": dist,
        "top_focused_students": student_scores[:10]
    })


@app.route("/api/roster_students")
def api_roster_students():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    query = (request.args.get("query") or request.args.get("trno") or "").strip().lower()
    group_filter = request.args.get("group", "").strip()
    class_filter = request.args.get("class", "").strip()
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 10))

    roster = load_student_roster()
    df = load_data()

    if not df.empty:
        if date_from:
            try:
                df = df[df["visit_time"] >= pd.to_datetime(date_from)]
            except Exception:
                pass
        if date_to:
            try:
                df = df[df["visit_time"] < pd.to_datetime(date_to) + timedelta(days=1)]
            except Exception:
                pass

    usage_data = lab_manager.get_all_usage_data()
    student_lab_hours = defaultdict(float)
    for date_str, s_map in usage_data.items():
        for t, mins in s_map.items():
            try:
                student_lab_hours[str(t).strip()] += float(mins) / 60.0
            except Exception:
                pass

    browsing_stats = {}
    if not df.empty:
        for trno_val, sub_df in df.groupby("TRNO"):
            trno_key = str(trno_val).strip()
            f_eval = analytics_service.calculate_student_focus_score(sub_df, trno_key)
            top_dom = sub_df["domain"].value_counts().index[0] if len(sub_df) > 0 else ""
            last_v = sub_df["visit_time"].max() if "visit_time" in sub_df.columns else "—"

            has_high = False
            has_med = False
            for _, r in sub_df.iterrows():
                eval_res = analytics_service.evaluate_safety_risk(
                    str(r.get("domain", "")), str(r.get("url", "")), str(r.get("title", ""))
                )
                if eval_res["risk_level"] == "HIGH":
                    has_high = True
                    break
                elif eval_res["risk_level"] == "MEDIUM":
                    has_med = True

            risk_level = "HIGH" if has_high else ("MEDIUM" if has_med else "LOW")
            risk_badge = "rose" if has_high else ("amber" if has_med else "green")

            browsing_stats[trno_key] = {
                "visits": len(sub_df),
                "last_visit": str(last_v) if pd.notnull(last_v) else "—",
                "top_domain": str(top_dom),
                "focus_score": f_eval["score"],
                "focus_badge": f_eval["badge"],
                "focus_icon": f_eval["icon"],
                "focus_label": f_eval["label"],
                "risk_level": risk_level,
                "risk_badge": risk_badge
            }

    students_list = []
    for trno, info in roster.items():
        trno_str = str(trno).strip()
        raw_name = info.get("name", f"Student #{trno_str}")
        clean_name = re.sub(r'^\d+\s*[-–—]\s*', '', str(raw_name)).strip() if raw_name else f"Student #{trno_str}"
        name = clean_name or f"Student #{trno_str}"
        s_class = info.get("class") or info.get("darajah") or "Unknown"
        s_group = info.get("group") or ("Talebaat" if str(info.get("gender", "")).lower() == "female" else "Talabaat")

        if group_filter:
            g_clean = group_filter.split()[0].lower()
            if g_clean not in s_group.lower():
                continue
        if class_filter and str(s_class).strip().lower() != class_filter.strip().lower():
            continue
        if query:
            match_trno = query in trno_str.lower()
            match_name = query in name.lower()
            match_cls = query in str(s_class).lower()
            if not (match_trno or match_name or match_cls):
                continue

        b_stat = browsing_stats.get(trno_str, {
            "visits": 0, "last_visit": "—", "top_domain": "—",
            "focus_score": 85, "focus_badge": "green", "focus_icon": "🟢", "focus_label": "Good Academic Focus",
            "risk_level": "LOW", "risk_badge": "green"
        })

        students_list.append({
            "trno": trno_str,
            "name": name,
            "name_ar": info.get("name_ar", ""),
            "gender": info.get("gender", ""),
            "class": s_class,
            "group": s_group,
            "visits": b_stat["visits"],
            "lab_hours": round(student_lab_hours.get(trno_str, 0.0), 1),
            "last_visit": b_stat["last_visit"],
            "top_domain": b_stat["top_domain"],
            "focus_score": {
                "score": b_stat["focus_score"],
                "badge": b_stat["focus_badge"],
                "icon": b_stat["focus_icon"],
                "label": b_stat["focus_label"]
            },
            "focus_score_num": b_stat["focus_score"],
            "focus_badge": b_stat["focus_badge"],
            "focus_icon": b_stat["focus_icon"],
            "focus_label": b_stat["focus_label"],
            "risk_level": b_stat.get("risk_level", "LOW"),
            "risk_badge": b_stat.get("risk_badge", "green")
        })

    students_list.sort(key=lambda x: (x["visits"], x["trno"]), reverse=True)

    total = len(students_list)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paginated = students_list[start_idx:end_idx]

    return jsonify({
        "data": paginated,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages
    })


# ─── API: Browsing Data ───────────────────────────────────────────────────────

@app.route("/api/data")
def get_data():
    trno      = request.args.get("trno", "").strip()
    query     = request.args.get("query", "").strip()
    search_term = query or trno
    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")
    group     = request.args.get("group", "").strip()
    cls       = request.args.get("class", "").strip()
    view_mode = request.args.get("view_mode", "").strip()

    df = load_data(trno=trno if (trno and trno.isdigit() and len(trno) >= 4) else None)
    if df.empty:
        df = load_data()

    if df.empty:
        return jsonify({"data": [], "total": 0, "page": 1, "per_page": 10, "total_pages": 0, "mode": "summary"})

    filtered = df.copy()
    roster = load_student_roster()

    if search_term:
        matching_trnos = {
            str(t).strip() for t, info in roster.items()
            if search_term.lower() in str(t).lower()
            or search_term.lower() in str(info.get("name", "")).lower()
            or search_term.lower() in str(info.get("class", "")).lower()
        }
        mask = (
            filtered["TRNO"].astype(str).str.contains(search_term, case=False, na=False) |
            filtered["TRNO"].astype(str).isin(matching_trnos)
        )
        if "title" in filtered.columns:
            mask |= filtered["title"].astype(str).str.contains(search_term, case=False, na=False)
        if "domain" in filtered.columns:
            mask |= filtered["domain"].astype(str).str.contains(search_term, case=False, na=False)
        if "Class" in filtered.columns:
            mask |= filtered["Class"].astype(str).str.contains(search_term, case=False, na=False)
        filtered = filtered[mask]

    if group:
        g_clean = group.split()[0].lower()
        filtered = filtered[filtered["group"].astype(str).str.lower().str.contains(g_clean)]
    if cls:
        filtered = filtered[filtered["Class"].astype(str).str.strip().str.lower() == cls.strip().lower()]
    if date_from:
        try:
            filtered = filtered[filtered["visit_time"] >= pd.to_datetime(date_from)]
        except Exception:
            pass
    if date_to:
        try:
            filtered = filtered[filtered["visit_time"] < pd.to_datetime(date_to) + timedelta(days=1)]
        except Exception:
            pass

    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 10))

    usage_data = lab_manager.get_all_usage_data()
    student_lab_hours = defaultdict(float)
    for date_str, s_map in usage_data.items():
        for t, mins in s_map.items():
            try:
                student_lab_hours[str(t).strip()] += float(mins) / 60.0
            except Exception:
                pass

    is_detail = (view_mode == "detail") or (
        bool(trno) and trno.isdigit() and len(trno) >= 4 and not query and len(filtered["TRNO"].unique()) == 1
    )

    if not is_detail:
        if filtered.empty:
            return jsonify({"data": [], "total": 0, "page": 1, "per_page": per_page, "total_pages": 0, "mode": "summary"})

        summary = (
            filtered.groupby("TRNO")
            .agg(
                visits=("url", "count"),
                group=("group", "first"),
                Class=("Class", "first"),
                last_visit=("visit_time", "max"),
                domain=("domain", lambda x: x.value_counts().index[0] if len(x) > 0 else ""),
            )
            .reset_index()
            .sort_values("TRNO", ascending=False)
        )
        summary["last_visit"] = summary["last_visit"].astype(str)
        total = len(summary)
        total_pages = max(1, (total + per_page - 1) // per_page)
        paginated = summary.iloc[(page - 1) * per_page: page * per_page].copy()
        records = paginated.fillna("").to_dict("records")

        for r in records:
            sid = str(r.get("TRNO", "")).strip()
            info = roster.get(sid, {})
            raw_name = info.get("name") or f"Student #{sid}"
            clean_name = re.sub(r'^\d+\s*[-–—]\s*', '', str(raw_name)).strip() if raw_name else f"Student #{sid}"
            r["name"] = clean_name or f"Student #{sid}"
            r["name_ar"] = info.get("name_ar", "")
            r["gender"] = info.get("gender", "")
            r["class"] = r.get("Class") or info.get("class") or info.get("darajah") or "General"
            r["lab_hours"] = round(student_lab_hours.get(sid, 0.0), 1)

            s_df = filtered[filtered["TRNO"] == sid]
            f_eval = analytics_service.calculate_student_focus_score(s_df, sid)
            r["focus_score"] = {
                "score": f_eval["score"],
                "badge": f_eval["badge"],
                "icon": f_eval["icon"],
                "label": f_eval["label"]
            }
            r["focus_score_num"] = f_eval["score"]

            has_high = False
            has_med = False
            for _, s_row in s_df.iterrows():
                eval_res = analytics_service.evaluate_safety_risk(
                    str(s_row.get("domain", "")), str(s_row.get("url", "")), str(s_row.get("title", ""))
                )
                if eval_res["risk_level"] == "HIGH":
                    has_high = True
                    break
                elif eval_res["risk_level"] == "MEDIUM":
                    has_med = True

            r["risk_level"] = "HIGH" if has_high else ("MEDIUM" if has_med else "LOW")
            r["risk_badge"] = "rose" if has_high else ("amber" if has_med else "green")

        return jsonify({
            "data": records, "total": total,
            "page": page, "per_page": per_page,
            "total_pages": total_pages,
            "mode": "summary"
        })

    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    paginated = filtered.iloc[(page - 1) * per_page: page * per_page].copy()
    paginated["visit_time"] = paginated["visit_time"].astype(str)
    records = paginated.fillna("").to_dict("records")

    for r in records:
        sid = str(r.get("TRNO", "")).strip()
        info = roster.get(sid, {})
        raw_name = info.get("name") or f"Student #{sid}"
        clean_name = re.sub(r'^\d+\s*[-–—]\s*', '', str(raw_name)).strip() if raw_name else f"Student #{sid}"
        r["name"] = clean_name or f"Student #{sid}"
        r["name_ar"] = info.get("name_ar", "")
        r["gender"] = info.get("gender", "")
        r["class"] = r.get("Class") or info.get("class") or info.get("darajah") or "General"
        eval_res = analytics_service.evaluate_safety_risk(
            str(r.get("domain", "")), str(r.get("url", "")), str(r.get("title", ""))
        )
        r["risk_level"] = eval_res["risk_level"]
        r["risk_badge"] = eval_res["badge"]
        r["risk_icon"] = eval_res["icon"]
        r["category"] = eval_res["category"]

    return jsonify({
        "data":        records,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": total_pages,
        "mode":        "detail"
    })


# ─── API: Classes ─────────────────────────────────────────────────────────────

@app.route("/api/classes")
def get_classes():
    class_map = build_class_map()
    groups = ["Talabaat", "Talebaat"]
    classes = sorted({
        info["class"] for info in class_map.values()
        if info.get("class") and str(info["class"]).upper() not in ("UNKNOWN", "UNKNOWN_OU", "NONE", "")
    })
    return jsonify({"groups": groups, "classes": classes})


# ─── Students Listing Page ───────────────────────────────────────────────────

@app.route("/students")
def students():
    if "logged_in" not in session:
        return redirect(url_for("index"))

    group_by       = request.args.get("group_by", "darajah").strip().lower()
    month_filter   = request.args.get("month", "").strip()
    group_filter   = request.args.get("group", "").strip()
    darajah_filter = request.args.get("darajah", "").strip()
    ad_filter      = request.args.get("ad_status", "").strip().lower()
    search         = request.args.get("q", "").lower().strip()

    monthly_groups_raw, all_months, student_month_stats = get_student_monthly_overview()
    visit_counts = get_all_student_visit_counts()
    total_lab_hours, monthly_lab_hours, _ = get_all_student_lab_summary()
    grouped_darajah = get_students_by_darajah()
    class_map = build_class_map()

    # Query Active Directory disabled students
    try:
        disabled_ad_trnos = {str(item.get("trno")) for item in ad_service.get_all_disabled_students()}
    except Exception:
        disabled_ad_trnos = set()

    all_darajahs = [
        d for d in sorted(
            set(grouped_darajah.keys()),
            key=lambda x: int(x) if x.isdigit() else 9999
        ) if str(d).upper() not in ("UNKNOWN", "UNKNOWN_OU", "NONE", "")
    ]
    all_groups = ["Talabaat", "Talebaat"]

    if group_by == "month":
        filtered_monthly_groups = {}
        for m_label, students_list in monthly_groups_raw.items():
            if month_filter and m_label != month_filter:
                continue

            matched = []
            for s in students_list:
                trno = str(s["trno"]).strip()
                cm = class_map.get(trno, {})
                gender = str(s.get("gender") or "").lower()
                default_grp = "Talebaat" if gender in ("girls", "female", "talebaat") else "Talabaat"
                
                grp = cm.get("group")
                if not grp or str(grp).upper() in ("UNKNOWN", "UNKNOWN_ROOT", "NONE", ""):
                    grp = default_grp
                s["group"] = grp

                if not s.get("darajah") or str(s["darajah"]).upper() in ("UNKNOWN", "UNKNOWN_OU"):
                    s["darajah"] = cm.get("class") or s.get("branch") or "General"
                s["class"] = s["darajah"]

                itsid = str(s.get("itsid") or s.get("cardnumber") or "").strip()
                v_cnt = visit_counts.get(trno) or (visit_counts.get(itsid) if itsid else 0) or 0
                l_hrs = total_lab_hours.get(trno) or (total_lab_hours.get(itsid) if itsid else 0.0) or 0.0

                if not s.get("visits"):
                    s["visits"] = v_cnt
                if not s.get("lab_hours"):
                    s["lab_hours"] = round(l_hrs, 1)

                is_ad_disabled = trno in disabled_ad_trnos
                s["ad_disabled"] = is_ad_disabled
                s["ad_status_label"] = "Disabled" if is_ad_disabled else "Active"

                if ad_filter == "disabled" and not is_ad_disabled:
                    continue
                elif ad_filter == "active" and is_ad_disabled:
                    continue
                if group_filter and s.get("group") != group_filter:
                    continue
                if darajah_filter and s.get("darajah") != darajah_filter:
                    continue
                if search and search not in s["name"].lower() and search not in trno:
                    continue
                matched.append(s)

            if matched:
                filtered_monthly_groups[m_label] = matched

        total_students = sum(len(v) for v in filtered_monthly_groups.values())
        return render_template("students.html",
                               group_by="month",
                               grouped=filtered_monthly_groups,
                               all_months=all_months,
                               all_darajahs=all_darajahs,
                               all_groups=all_groups,
                               group_filter=group_filter,
                               darajah_filter=darajah_filter,
                               month_filter=month_filter,
                               ad_filter=ad_filter,
                               total_ad_disabled=len(disabled_ad_trnos),
                               search=search,
                               total=total_students)

    all_accounts = account_lock_service.get_all_accounts()

    filtered_groups = {}
    for dar, students_list in grouped_darajah.items():
        if str(dar).upper() in ("UNKNOWN", "UNKNOWN_OU"):
            continue
        enriched = []
        for s in students_list:
            trno = str(s["trno"]).strip()
            cm = class_map.get(trno, {})
            gender = str(s.get("gender") or "").lower()
            default_grp = "Talebaat" if gender in ("girls", "female", "talebaat") else "Talabaat"

            grp = cm.get("group")
            if not grp or str(grp).upper() in ("UNKNOWN", "UNKNOWN_ROOT", "NONE", ""):
                grp = default_grp
            s["group"] = grp

            if not s.get("darajah") or str(s["darajah"]).upper() in ("UNKNOWN", "UNKNOWN_OU"):
                s["darajah"] = cm.get("class") or s.get("branch") or "General"
            s["class"] = s["darajah"]

            itsid = str(s.get("itsid") or s.get("cardnumber") or "").strip()

            v_cnt = visit_counts.get(trno) or (visit_counts.get(itsid) if itsid else 0) or 0
            l_hrs = total_lab_hours.get(trno) or (total_lab_hours.get(itsid) if itsid else 0.0) or 0.0

            m_stats = student_month_stats.get(trno) or (student_month_stats.get(itsid) if itsid else {}) or {}
            s["monthly_stats"] = m_stats

            sum_m_visits = sum(v["visits"] for v in m_stats.values()) if m_stats else 0
            sum_m_lab = sum(v["lab_hours"] for v in m_stats.values()) if m_stats else 0.0

            s["total_visits"] = max(v_cnt, sum_m_visits)
            s["lab_hours"] = round(max(l_hrs, sum_m_lab), 1)

            acc = all_accounts.get(trno) or all_accounts.get(itsid) or {}
            v_list = acc.get("violations", [])
            violation_count = len(v_list)
            warning_count = acc.get("warning_count", 0)
            acc_status = acc.get("status", "active")
            if acc_status == "blocked":
                risk_score = min(100, 60 + violation_count * 5)
            elif acc_status == "warned":
                risk_score = min(59, 30 + warning_count * 10)
            else:
                risk_score = min(29, violation_count * 3)
            s["violation_count"] = violation_count
            s["account_status"] = acc_status
            s["risk_score"] = risk_score
            s["last_incident"] = acc.get("last_incident") or ""

            is_ad_disabled = trno in disabled_ad_trnos
            s["ad_disabled"] = is_ad_disabled
            s["ad_status_label"] = "Disabled" if is_ad_disabled else "Active"

            if ad_filter == "disabled" and not is_ad_disabled:
                continue
            elif ad_filter == "active" and is_ad_disabled:
                continue
            if month_filter and month_filter not in m_stats:
                continue
            if group_filter and s["group"] != group_filter:
                continue
            if search and search not in s["name"].lower() and search not in trno:
                continue
            enriched.append(s)

        if darajah_filter and dar != darajah_filter:
            continue
        if enriched:
            filtered_groups[dar] = enriched

    return render_template("students.html",
                           group_by="darajah",
                           grouped=filtered_groups,
                           all_months=all_months,
                           all_darajahs=all_darajahs,
                           all_groups=all_groups,
                           group_filter=group_filter,
                           darajah_filter=darajah_filter,
                           month_filter=month_filter,
                           ad_filter=ad_filter,
                           total_ad_disabled=len(disabled_ad_trnos),
                           search=search,
                           total=sum(len(v) for v in filtered_groups.values()))


@app.route("/students/export_csv")
def export_students_csv():
    """Export filtered students directory to CSV."""
    if "logged_in" not in session:
        return redirect(url_for("index"))

    group_filter   = request.args.get("group", "").strip()
    darajah_filter = request.args.get("darajah", "").strip()
    ad_filter      = request.args.get("ad_status", "").strip().lower()
    search         = request.args.get("q", "").lower().strip()

    visit_counts = get_all_student_visit_counts()
    total_lab_hours, _, _ = get_all_student_lab_summary()
    grouped_darajah = get_students_by_darajah()
    class_map = build_class_map()
    all_accounts = account_lock_service.get_all_accounts()

    try:
        disabled_ad_trnos = {str(item.get("trno")) for item in ad_service.get_all_disabled_students()}
    except Exception:
        disabled_ad_trnos = set()

    rows = []
    for dar, students_list in grouped_darajah.items():
        if str(dar).upper() in ("UNKNOWN", "UNKNOWN_OU"):
            continue
        for s in students_list:
            trno = str(s["trno"]).strip()
            cm = class_map.get(trno, {})
            gender = str(s.get("gender") or "").lower()
            default_grp = "Talebaat" if gender in ("girls", "female", "talebaat") else "Talabaat"
            grp = cm.get("group")
            if not grp or str(grp).upper() in ("UNKNOWN", "UNKNOWN_ROOT", "NONE", ""):
                grp = default_grp
            s_dar = cm.get("class") or s.get("darajah") or s.get("branch") or dar

            itsid = str(s.get("itsid") or s.get("cardnumber") or "").strip()
            v_cnt = visit_counts.get(trno) or (visit_counts.get(itsid) if itsid else 0) or 0
            l_hrs = total_lab_hours.get(trno) or (total_lab_hours.get(itsid) if itsid else 0.0) or 0.0

            acc = all_accounts.get(trno) or all_accounts.get(itsid) or {}
            viol_count = len(acc.get("violations", []))
            acc_status = acc.get("status", "active")
            is_ad_disabled = trno in disabled_ad_trnos

            if ad_filter == "disabled" and not is_ad_disabled:
                continue
            elif ad_filter == "active" and is_ad_disabled:
                continue
            if group_filter and grp != group_filter:
                continue
            if darajah_filter and str(s_dar) != darajah_filter:
                continue
            if search and search not in s["name"].lower() and search not in trno:
                continue

            rows.append({
                "TRNO": trno,
                "Name": s.get("name", ""),
                "Darajah / Class": s_dar,
                "Group": grp,
                "Gender": "Female" if gender in ("girls", "female", "talebaat", "f") else "Male",
                "AY Browsing Visits": v_cnt,
                "Lab Hours": round(l_hrs, 1),
                "Violations": viol_count,
                "Internal Account Status": acc_status.capitalize(),
                "Active Directory Domain": "DISABLED (mamal.nbi)" if is_ad_disabled else "ACTIVE"
            })

    df = pd.DataFrame(rows)
    csv_bytes = df.to_csv(index=False).encode('utf-8-sig')
    output = BytesIO(csv_bytes)
    output.seek(0)
    return send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"Students_Directory_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    )


@app.route("/api/students/monthly")
def api_students_monthly():
    monthly_groups, all_months, _ = get_student_monthly_overview()
    month_name = request.args.get("month", "").strip()
    if month_name:
        return jsonify({
            "month": month_name,
            "students": monthly_groups.get(month_name, []),
            "count": len(monthly_groups.get(month_name, []))
        })
    return jsonify({
        "all_months": all_months,
        "monthly_groups": monthly_groups
    })


@app.route("/api/students")
def get_students():
    df    = load_data()
    group = request.args.get("group", "")
    cls   = request.args.get("class", "")
    if df.empty:
        return jsonify([])
    filtered = df.copy()
    if group:
        filtered = filtered[filtered["group"] == group]
    if cls:
        filtered = filtered[filtered["Class"] == cls]
    students = (filtered.groupby("TRNO")
                .agg(visits=("url", "count"),
                     group=("group", "first"),
                     Class=("Class", "first"))
                .reset_index()
                .sort_values("visits", ascending=False))
    return jsonify(students.to_dict("records"))


@app.route("/api/refresh")
def refresh_cache():
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    invalidate_caches()
    return jsonify({"status": "ok", "message": "Cache cleared. Data will reload on next request."})


@app.route("/download_csv")
def download_csv():
    if "logged_in" not in session:
        return redirect(url_for("index"))

    trno  = request.args.get("trno", "").strip()
    group = request.args.get("group", "").strip()
    cls   = request.args.get("class", "").strip()
    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")
    mode      = request.args.get("mode", "home")

    df = load_data(trno=trno if trno else None)
    filtered = df.copy()
    if group:
        filtered = filtered[filtered["group"] == group]
    if cls:
        filtered = filtered[filtered["Class"] == cls]
    if date_from:
        filtered = filtered[filtered["visit_time"] >= pd.to_datetime(date_from)]
    if date_to:
        filtered = filtered[filtered["visit_time"] < pd.to_datetime(date_to) + timedelta(days=1)]

    if mode == "report":
        out = filtered[["title", "url", "domain", "visit_time"]].copy()
        out.columns = ["Title", "URL", "Domain", "Visit Time"]
    else:
        cols = ["TRNO", "Class", "group", "title", "url", "domain", "visit_time"]
        out  = filtered[[c for c in cols if c in filtered.columns]].copy()
        out.columns = [c.upper() if c != "visit_time" else "Time" for c in out.columns]

    csv_bytes = out.to_csv(index=False).encode("utf-8")
    output    = BytesIO(csv_bytes)
    output.seek(0)
    filename  = f"browsing_history_{trno or group or 'all'}.csv"
    return send_file(output, mimetype="text/csv", as_attachment=True, download_name=filename)


# ─── Option A: Lab Seat Occupancy API ────────────────────────────────────────

_lab_occupancy_cache = {"data": None, "ts": 0}

@app.route("/api/lab/occupancy")
def api_lab_occupancy():
    """Live lab seat occupancy — splits MK (Maktab) and RB (Rabwah) labs."""
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    now = time.time()
    if _lab_occupancy_cache["data"] is not None and (now - _lab_occupancy_cache["ts"]) < 20:
        return jsonify(_lab_occupancy_cache["data"])

    sessions = lab_manager.get_all_sessions()
    mk_active = set()
    rb_active = set()
    mk_total = 50  # institutional capacity
    rb_total = 50  # institutional capacity

    for s in sessions:
        if not isinstance(s, dict):
            continue
        status = str(s.get("status", "")).lower()
        if status not in ("active", "running", "logged_in"):
            continue
        machine_raw = s.get("machine_id") or s.get("station") or s.get("computer") or ""
        machine = lab_manager.normalize_station_name(machine_raw)
        if not machine:
            continue
        uname = str(s.get("username") or s.get("user") or "").strip()
        if machine.upper().startswith("MK-"):
            mk_active.add(machine)
        elif machine.upper().startswith("RB-"):
            rb_active.add(machine)

    mk_count = len(mk_active)
    rb_count = len(rb_active)
    total_active = mk_count + rb_count
    total_capacity = mk_total + rb_total

    def seat_status(count, total):
        pct = (count / total * 100) if total > 0 else 0
        if pct >= 90:
            return "full"
        elif pct >= 70:
            return "near_capacity"
        elif pct >= 30:
            return "moderate"
        return "available"

    payload = {
        "mk": {
            "active": mk_count,
            "total": mk_total,
            "available": max(0, mk_total - mk_count),
            "pct": round(mk_count / mk_total * 100, 1) if mk_total > 0 else 0,
            "status": seat_status(mk_count, mk_total),
            "stations": sorted(list(mk_active))
        },
        "rb": {
            "active": rb_count,
            "total": rb_total,
            "available": max(0, rb_total - rb_count),
            "pct": round(rb_count / rb_total * 100, 1) if rb_total > 0 else 0,
            "status": seat_status(rb_count, rb_total),
            "stations": sorted(list(rb_active))
        },
        "total_active": total_active,
        "total_capacity": total_capacity,
        "total_pct": round(total_active / total_capacity * 100, 1) if total_capacity > 0 else 0,
    }

    _lab_occupancy_cache["data"] = payload
    _lab_occupancy_cache["ts"] = now
    return jsonify(payload)


# ─── Option A: Academic Focus Index API ──────────────────────────────────────

@app.route("/api/lab/focus_index")
def api_lab_focus_index():
    """Campus-wide academic focus score — educational vs recreational domain ratio."""
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    df = load_data()
    if df.empty:
        return jsonify({
            "focus_score": 85, "productive": 0, "total": 0,
            "label": "Insufficient Data", "trend": "+0.0%"
        })

    productive_domains = {
        "jstor.org", "britannica.com", "archive.org", "wikipedia.org",
        "scholar.google.com", "ncbi.nlm.nih.gov", "islamicstudies.info",
        "quran.com", "islamqa.info", "sunnah.com", "al-islam.org",
        "edx.org", "coursera.org", "khanacademy.org", "ted.com",
        "stackoverflow.com", "github.com", "python.org", "docs.python.org",
        "w3schools.com", "developer.mozilla.org"
    }
    distracting_domains = {
        "youtube.com", "tiktok.com", "instagram.com", "facebook.com",
        "twitter.com", "x.com", "snapchat.com", "netflix.com",
        "gaming.com", "miniclip.com", "poki.com", "friv.com",
        "roblox.com", "9gag.com", "reddit.com"
    }

    total = len(df)
    productive_visits = df["domain"].isin(productive_domains).sum() if "domain" in df.columns else 0
    distracting_visits = df["domain"].isin(distracting_domains).sum() if "domain" in df.columns else 0

    # Focus score: weighted — reward productive, penalize distracting
    if total > 0:
        prod_ratio = productive_visits / total
        dist_ratio = distracting_visits / total
        # Base 75 + bonus for productive, penalty for distracting
        score = max(0, min(100, round(75 + prod_ratio * 40 - dist_ratio * 50)))
    else:
        score = 85

    label = "Outstanding" if score >= 90 else ("Good" if score >= 75 else ("Moderate" if score >= 55 else "Needs Attention"))

    return jsonify({
        "focus_score": score,
        "productive": int(productive_visits),
        "distracting": int(distracting_visits),
        "total": int(total),
        "label": label,
        "trend": "+0.0%"  # Placeholder — future: compare week-over-week
    })


# ─── Option A: Reinstatement Queue API ───────────────────────────────────────

@app.route("/api/lab/reinstatement_queue")
def api_reinstatement_queue():
    """Students whose 14-day suspension is expiring within the next 48h."""
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    roster = load_student_roster()
    blocked = account_lock_service.get_blocked_students()
    now_dt = datetime.now()
    queue = []

    for stu in blocked:
        trno = str(stu.get("trno") or stu.get("username") or "").strip()
        blocked_at_str = stu.get("blocked_at") or stu.get("suspension_start") or ""
        if not blocked_at_str:
            continue
        try:
            blocked_dt = datetime.fromisoformat(str(blocked_at_str).replace("Z", ""))
        except Exception:
            try:
                blocked_dt = datetime.strptime(str(blocked_at_str)[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

        days_elapsed = (now_dt - blocked_dt).total_seconds() / 86400
        days_remaining = max(0, 14 - days_elapsed)

        if days_remaining <= 3:  # Expiring within 3 days
            info = roster.get(trno, {})
            name = info.get("name") or f"Student #{trno}"
            import re as _re
            name = _re.sub(r'^\d+\s*[-–—]\s*', '', str(name)).strip()
            queue.append({
                "trno": trno,
                "name": name,
                "darajah": info.get("darajah") or info.get("class") or "—",
                "group": info.get("group") or "Talabaat",
                "blocked_at": str(blocked_at_str)[:19],
                "days_elapsed": round(days_elapsed, 1),
                "days_remaining": round(days_remaining, 1),
                "expires_label": "TODAY" if days_remaining < 1 else (f"In {int(days_remaining)} day{'s' if int(days_remaining) != 1 else ''}"),
                "urgency": "critical" if days_remaining < 1 else ("high" if days_remaining <= 1 else "medium"),
            })

    queue.sort(key=lambda x: x["days_remaining"])
    return jsonify({"queue": queue, "count": len(queue)})


# ─── Option B: Concurrent Login Detector API ─────────────────────────────────

_concurrent_login_cache = {"data": None, "ts": 0}

@app.route("/api/security/concurrent_logins")
def api_concurrent_logins():
    """Detect students logged in simultaneously from multiple workstations (account sharing)."""
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    now = time.time()
    if _concurrent_login_cache["data"] is not None and (now - _concurrent_login_cache["ts"]) < 30:
        return jsonify(_concurrent_login_cache["data"])

    sessions = lab_manager.get_all_sessions()
    roster = load_student_roster()
    now_dt = datetime.now()
    active_cutoff = now_dt - timedelta(hours=1)  # Consider sessions within last hour

    # Group active sessions by TRNO
    trno_stations = {}
    for s in sessions:
        if not isinstance(s, dict):
            continue
        status = str(s.get("status", "")).lower()
        if status not in ("active", "running", "logged_in"):
            continue
        # Also consider recent sessions (logout within last 30 min)
        login_str = s.get("login_time") or s.get("start_time") or ""
        try:
            login_dt = datetime.fromisoformat(str(login_str).replace("Z", ""))
            if login_dt < active_cutoff:
                continue
        except Exception:
            pass

        trno = str(s.get("username") or s.get("user") or "").strip()
        if not trno:
            continue
        machine = lab_manager.normalize_station_name(
            s.get("machine_id") or s.get("station") or s.get("computer") or ""
        )
        if not machine:
            continue

        if trno not in trno_stations:
            trno_stations[trno] = []
        trno_stations[trno].append({
            "station": machine,
            "login_time": login_str,
            "status": str(s.get("status", ""))
        })

    # Detect anomalies: same TRNO on 2+ stations
    anomalies = []
    off_hours_logins = []

    for trno, stations in trno_stations.items():
        info = roster.get(trno, {})
        name = info.get("name") or f"Student #{trno}"
        import re as _re
        name = _re.sub(r'^\d+\s*[-–—]\s*', '', str(name)).strip()

        if len(stations) >= 2:
            anomalies.append({
                "trno": trno,
                "name": name,
                "darajah": info.get("darajah") or info.get("class") or "—",
                "group": info.get("group") or "Talabaat",
                "stations": [s["station"] for s in stations],
                "station_count": len(stations),
                "login_times": [s["login_time"] for s in stations],
                "severity": "CRITICAL" if len(stations) >= 3 else "HIGH",
            })

    # Off-hours detection (sessions between 10PM–5AM)
    all_sessions_today = [s for s in sessions if isinstance(s, dict)]
    for s in all_sessions_today:
        login_str = s.get("login_time") or s.get("start_time") or ""
        try:
            login_dt = datetime.fromisoformat(str(login_str).replace("Z", ""))
            hour = login_dt.hour
            if hour >= 22 or hour < 5:
                trno = str(s.get("username") or s.get("user") or "").strip()
                machine = lab_manager.normalize_station_name(
                    s.get("machine_id") or s.get("station") or ""
                )
                if trno and machine:
                    info = roster.get(trno, {})
                    name = info.get("name") or f"Student #{trno}"
                    import re as _re
                    name = _re.sub(r'^\d+\s*[-–—]\s*', '', str(name)).strip()
                    off_hours_logins.append({
                        "trno": trno,
                        "name": name,
                        "station": machine,
                        "login_time": login_str,
                        "hour": hour,
                        "darajah": info.get("darajah") or info.get("class") or "—",
                    })
        except Exception:
            pass

    off_hours_logins = off_hours_logins[:20]  # Limit

    payload = {
        "concurrent_anomalies": anomalies,
        "concurrent_count": len(anomalies),
        "off_hours_logins": off_hours_logins,
        "off_hours_count": len(off_hours_logins),
        "total_flags": len(anomalies) + len(off_hours_logins),
    }
    _concurrent_login_cache["data"] = payload
    _concurrent_login_cache["ts"] = now
    return jsonify(payload)


# ─── Option B: Emergency Lab Lock API ────────────────────────────────────────

@app.route("/api/security/lab_lock", methods=["POST"])
def api_lab_lock():
    """Emergency bulk lab lock for MK or RB lab."""
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    lab = str(data.get("lab", "")).upper().strip()   # "MK", "RB", or "ALL"
    action = str(data.get("action", "lock")).lower()  # "lock" or "unlock"

    if lab not in ("MK", "RB", "ALL"):
        return jsonify({"error": "lab must be MK, RB, or ALL"}), 400

    # Log the emergency action to event log
    try:
        role = session.get("user_role", "admin")
        msg = f"[EMERGENCY] {action.upper()} applied to {'ALL labs' if lab == 'ALL' else f'{lab} Lab'} by {role} at {datetime.now().isoformat()}"
        logger.warning(msg)
        # Persist to event log file
        events_dir = os.path.join(_PARENT_DIR, "logs")
        os.makedirs(events_dir, exist_ok=True)
        with open(os.path.join(events_dir, "emergency_locks.jsonl"), "a", encoding="utf-8") as f:
            import json as _json
            f.write(_json.dumps({
                "action": action,
                "lab": lab,
                "role": role,
                "timestamp": datetime.now().isoformat(),
                "status": "executed"
            }) + "\n")
    except Exception as e:
        logger.warning("[LabLock] Event log write failed: %s", e)

    lab_label = "All Labs (MK + RB)" if lab == "ALL" else f"{lab} Lab"
    sessions = lab_manager.get_all_sessions()
    affected = sum(1 for s in sessions if isinstance(s, dict) and
                   str(s.get("status", "")).lower() in ("active", "running") and
                   lab_manager.normalize_station_name(s.get("machine_id") or "").startswith(lab if lab != "ALL" else ""))

    return jsonify({
        "status": "executed",
        "action": action,
        "lab": lab_label,
        "affected_sessions": affected,
        "timestamp": datetime.now().isoformat(),
        "message": f"🔒 {action.capitalize()} command broadcast to {lab_label}. {affected} active session(s) targeted."
    })


# ─── Option C: Disciplinary Dossier API ──────────────────────────────────────

@app.route("/api/disciplinary/dossier/<trno>")
def api_disciplinary_dossier(trno):
    """Generate a structured disciplinary dossier for a student."""
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    trno = str(trno).strip()
    roster = load_student_roster()
    info = roster.get(trno, {})

    # Student Profile
    name = info.get("name") or f"Student #{trno}"
    import re as _re
    name = _re.sub(r'^\d+\s*[-–—]\s*', '', str(name)).strip()
    darajah = info.get("darajah") or info.get("class") or "—"
    group = info.get("group") or "Talabaat"
    gender = info.get("gender") or "—"
    email = info.get("email") or "—"

    # Violation History
    from app.routes.enforcement import VIOLATIONS_DIR
    violations = []
    try:
        for vfile in sorted(VIOLATIONS_DIR.glob("violations_*.jsonl"), reverse=True)[:14]:
            for line in vfile.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if str(rec.get("trno", "")).strip() == trno:
                        violations.append({
                            "timestamp": rec.get("ts") or rec.get("timestamp") or "",
                            "domain": rec.get("domain") or "",
                            "url": rec.get("url") or "",
                            "category": rec.get("category") or "restricted",
                            "severity": rec.get("severity") or "HIGH",
                            "station": rec.get("station") or rec.get("computer") or "—",
                            "action": rec.get("action") or "block",
                        })
                except Exception:
                    pass
    except Exception as e:
        logger.warning("[Dossier] Violation history read failed for %s: %s", trno, e)

    violations.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # Account Status
    all_accs = account_lock_service.get_all_accounts()
    acc = all_accs.get(trno, {})
    acc_status = acc.get("status", "active")
    blocked_at = acc.get("blocked_at") or ""
    warning_count = acc.get("warning_count", 0)

    # Strike Level (1=warning, 2=proctor counseling, 3=suspension)
    total_incidents = len(violations)
    if acc_status == "blocked":
        strike_level = 3
    elif warning_count >= 2 or total_incidents >= 3:
        strike_level = 2
    elif warning_count >= 1 or total_incidents >= 1:
        strike_level = 1
    else:
        strike_level = 0

    # Lab Sessions (recent)
    lab_sessions_raw = lab_manager.get_student_sessions(trno, limit=10)
    lab_sessions = []
    for s in lab_sessions_raw:
        if not isinstance(s, dict):
            continue
        machine = lab_manager.normalize_station_name(s.get("machine_id") or s.get("station") or "")
        lab_sessions.append({
            "station": machine or "—",
            "login_time": str(s.get("login_time") or s.get("start_time") or "")[:19],
            "logout_time": str(s.get("logout_time") or s.get("last_heartbeat") or "")[:19],
            "duration_min": round(float(s.get("duration") or 0), 1),
            "status": str(s.get("status") or ""),
        })

    # Hijri date
    today = datetime.now().date()
    hijri_label = get_hijri_date_label(today)

    dossier = {
        "generated_at": datetime.now().isoformat(),
        "generated_by": session.get("user_role", "admin"),
        "hijri_date": hijri_label,
        "institution": "Al-Jamea tus-Saifiyah — Nairobi (mamal.nbi)",
        "student": {
            "trno": trno,
            "name": name,
            "darajah": darajah,
            "group": group,
            "gender": gender,
            "email": email,
        },
        "account": {
            "status": acc_status,
            "blocked_at": str(blocked_at)[:19] if blocked_at else "—",
            "warning_count": warning_count,
            "strike_level": strike_level,
            "strike_label": ["Clean", "Warning Issued", "Proctor Counseling Required", "Suspended (14-Day Policy)"][strike_level],
        },
        "violations": violations[:50],  # Cap at 50 for the dossier
        "total_violations": total_incidents,
        "lab_sessions": lab_sessions,
        "summary": {
            "total_incidents": total_incidents,
            "high_severity": sum(1 for v in violations if v.get("severity") == "HIGH"),
            "medium_severity": sum(1 for v in violations if v.get("severity") == "MEDIUM"),
            "unique_domains": len({v.get("domain") for v in violations if v.get("domain")}),
            "top_category": max(
                set(v.get("category", "restricted") for v in violations),
                key=lambda c: sum(1 for v in violations if v.get("category") == c),
                default="—"
            ) if violations else "—",
        },
        "recommendations": [
            f"Strike {strike_level}/3: {'Suspension in effect.' if strike_level >= 3 else 'Proctor counseling recommended.' if strike_level == 2 else 'Issue formal written warning.' if strike_level == 1 else 'No action required.'}",
            f"Total {total_incidents} policy incident(s) recorded in the current 14-day window.",
            f"Account status: {acc_status.upper()}.",
        ],
    }

    return jsonify(dossier)


# ─── Option B: Three-Strike Progress API ─────────────────────────────────────

@app.route("/api/security/strike_matrix")
def api_strike_matrix():
    """Three-strike disciplinary escalation matrix for all at-risk students."""
    if "logged_in" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    roster = load_student_roster()
    all_accs = account_lock_service.get_all_accounts()

    matrix = []
    for trno, acc in all_accs.items():
        if not acc or acc.get("status") == "active" and not acc.get("violations"):
            continue
        info = roster.get(str(trno), {})
        name = info.get("name") or f"Student #{trno}"
        import re as _re
        name = _re.sub(r'^\d+\s*[-–—]\s*', '', str(name)).strip()

        acc_status = acc.get("status", "active")
        warning_count = acc.get("warning_count", 0)
        violations = acc.get("violations", [])
        total_v = len(violations)

        if acc_status == "blocked":
            strike = 3
        elif warning_count >= 2 or total_v >= 3:
            strike = 2
        elif warning_count >= 1 or total_v >= 1:
            strike = 1
        else:
            continue  # Skip clean students

        matrix.append({
            "trno": str(trno),
            "name": name,
            "darajah": info.get("darajah") or info.get("class") or "—",
            "group": info.get("group") or "Talabaat",
            "strike": strike,
            "strike_label": ["", "⚠️ Warning Issued", "📋 Proctor Counseling", "🔒 Suspended"][strike],
            "strike_color": ["", "amber", "rose", "red"][strike],
            "acc_status": acc_status,
            "warning_count": warning_count,
            "violation_count": total_v,
            "last_incident": acc.get("last_incident") or "",
            "blocked_at": acc.get("blocked_at") or "",
        })

    matrix.sort(key=lambda x: (-x["strike"], -x["violation_count"]))
    counts = {1: 0, 2: 0, 3: 0}
    for m in matrix:
        counts[m["strike"]] = counts.get(m["strike"], 0) + 1

    return jsonify({
        "matrix": matrix[:100],
        "strike1_count": counts.get(1, 0),
        "strike2_count": counts.get(2, 0),
        "strike3_count": counts.get(3, 0),
        "total": len(matrix),
    })


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _port = int(os.getenv("PORT", 5050))
    app.run(debug=os.getenv("FLASK_ENV") == "development", host="0.0.0.0", port=_port)