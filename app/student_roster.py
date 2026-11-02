# app/student_roster.py
"""
Student Roster Provider
Uses proven Koha LMS student extraction logic:
- Handles attribute codes: ('Class', 'STD', 'CLASS', 'DAR', 'CLASS_STD')
- Handles TRNO attribute codes: ('TRNO', 'TRN', 'TR_NUMBER', 'TR')
- Robust StudentName synthesis matching Koha queries
- Extracts Darajah (e.g. 3 B M, 3 B F), Section (A, B, C), and Gender (M / F)
- Student category identification ('S%')
- Persistent local disk cache + background refresh
"""
import os
import re
import sys
import json
import time
import logging
import threading
import pandas as pd

logger = logging.getLogger("student_roster")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data")
CACHE_FILE = os.path.join(CACHE_DIR, "koha_student_roster_cache.json")

XLSX_PATH = os.path.join(BASE_DIR,
                         "internal", "ALL FILES ARE HERE",
                         "student list 45-46 without photos full.xlsx")

_student_cache = {"data": None, "ts": 0}
CACHE_TTL = 1800   # 30 minutes memory TTL
_is_refreshing = False
_refresh_lock = threading.Lock()


class RosterDict(dict):
    """
    A dict subclass that supports alias-based lookups.
    Primary keys are TRNOs; aliases map alternative identifiers
    (e.g. cardnumber) back to the canonical TRNO key.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases = {}

    def add_alias(self, alias_key, target_trno):
        """Register *alias_key* as an alias pointing at *target_trno*."""
        if alias_key and target_trno and alias_key != target_trno:
            self._aliases[str(alias_key).strip()] = str(target_trno).strip()

    def __getitem__(self, key):
        k_str = str(key).strip()
        if super().__contains__(k_str):
            return super().__getitem__(k_str)
        if k_str in self._aliases:
            return super().__getitem__(self._aliases[k_str])
        raise KeyError(key)

    def get(self, key, default=None):
        k_str = str(key).strip()
        if super().__contains__(k_str):
            return super().get(k_str, default)
        if k_str in self._aliases:
            return super().get(self._aliases[k_str], default)
        return default

    def __contains__(self, key):
        k_str = str(key).strip()
        return super().__contains__(k_str) or (k_str in self._aliases)


def parse_darajah_details(darajah_name: str) -> dict:
    """Parse Darajah string e.g. '3 B M' into year, section, gender, and group."""
    name_str = str(darajah_name or "").strip()
    if name_str.upper() == "AJSN":
        return {"darajah": "Asateza", "gender": "Mixed", "section": "", "year": "", "group": "Asateza"}

    gender = "Mixed"
    if " M" in name_str or name_str.endswith("M") or name_str.endswith(" M"):
        gender = "Boys"
    elif " F" in name_str or name_str.endswith("F") or name_str.endswith(" F"):
        gender = "Girls"

    section = ""
    if " A" in name_str:
        section = "A"
    elif " B" in name_str:
        section = "B"
    elif " C" in name_str:
        section = "C"

    year_match = re.search(r'\d+', name_str)
    year = year_match.group() if year_match else ""

    return {
        "darajah": name_str or "Unknown",
        "gender": gender,
        "section": section,
        "year": year
    }


AD_OU_STUDENTS_FILE = os.path.join(CACHE_DIR, "ad_ou_students.json")
AD_OU_SCRIPT = os.path.join(BASE_DIR, "tools", "get_ad_student_roster.ps1")


def _get_ad_ou_students() -> dict:
    """
    Load authoritative student accounts from Active Directory OUs
    (OU=TALABAAT and OU=TALEBAAT).
    Returns dict mapping TRNO -> student AD metadata dict.
    """
    # 1. Try reading ad_ou_students.json if present
    if os.path.exists(AD_OU_STUDENTS_FILE):
        try:
            with open(AD_OU_STUDENTS_FILE, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
                if isinstance(raw, list) and len(raw) > 0:
                    return {str(x["trno"]).strip(): x for x in raw if x.get("trno")}
        except Exception as e:
            logger.warning(f"[AD OUs] Failed reading {AD_OU_STUDENTS_FILE}: {e}")

    # 2. Extract directly via PowerShell script if present
    if os.path.exists(AD_OU_SCRIPT):
        try:
            import subprocess
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", AD_OU_SCRIPT],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if os.path.exists(AD_OU_STUDENTS_FILE):
                with open(AD_OU_STUDENTS_FILE, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                    if isinstance(raw, list) and len(raw) > 0:
                        return {str(x["trno"]).strip(): x for x in raw if x.get("trno")}
        except Exception as e:
            logger.warning(f"[AD OUs] Failed extracting AD OU students: {e}")

    return {}


def _filter_roster_by_ad_ous(raw_roster: RosterDict) -> RosterDict:
    """
    Filter the roster to ONLY include students present in Active Directory student OUs.
    Excludes any student outside of the OUs (alumni, test accounts, etc.).
    Enriches with AD metadata for any OU student not returned by Koha.
    """
    ad_students = _get_ad_ou_students()
    if not ad_students:
        return raw_roster

    filtered = RosterDict()
    for trno, ad_info in ad_students.items():
        trno_str = str(trno).strip()
        if trno_str in raw_roster:
            stu_obj = dict(raw_roster[trno_str])
            if ad_info.get("gender") and stu_obj.get("gender") in ("Mixed", "", "Unknown"):
                stu_obj["gender"] = ad_info["gender"]
            filtered[trno_str] = stu_obj
            card_key = str(stu_obj.get("cardnumber") or "").strip()
            if card_key and card_key != trno_str:
                filtered.add_alias(card_key, trno_str)
            its_key = str(stu_obj.get("itsid") or "").strip()
            if its_key and its_key != trno_str:
                filtered.add_alias(its_key, trno_str)
        else:
            dn = ad_info.get("dn", "")
            dar_m = re.search(r'OU=([^,]+)', dn)
            dar_raw = dar_m.group(1).replace("Darajah ", "").strip() if dar_m else ""
            gender = ad_info.get("gender", "Mixed")
            details = parse_darajah_details(f"{dar_raw} {gender[0] if gender in ('Boys', 'Girls') else ''}")
            name = re.sub(r'^\d+\s*[-–—]\s*', '', ad_info.get("name") or f"Student #{trno_str}").strip()

            stu_obj = {
                "trno":           trno_str,
                "name":           name,
                "name_ar":        "",
                "darajah":        dar_raw or details["darajah"],
                "class":          dar_raw or details["darajah"],
                "section":        details["section"],
                "year":           details["year"],
                "gender":         gender,
                "branch":         "General",
                "itsid":          trno_str,
                "cardnumber":     trno_str,
                "borrowernumber": None,
                "age":            "",
                "email":          "",
                "mobile":         "",
                "nationality":    "",
                "jamaat":         "",
                "accommodation":  "",
                "nizaam":         "",
                "hizb":           "",
                "vatan":          "",
                "tabaqat":        "",
            }
            filtered[trno_str] = stu_obj

    return filtered


def _load_from_disk_cache():
    """Load cached roster from local JSON file if available."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            if isinstance(raw, dict) and len(raw) > 0:
                roster = RosterDict()
                for k, v in raw.items():
                    if not isinstance(v, dict):
                        continue
                    if "name" in v:
                        v["name"] = re.sub(r'^\d+\s*[-–—]\s*', '', str(v["name"])).strip()
                    trno_key = str(v.get("trno") or k).strip()
                    if trno_key not in roster:
                        roster[trno_key] = v
                for k, v in raw.items():
                    if not isinstance(v, dict):
                        continue
                    trno_key = str(v.get("trno") or k).strip()
                    card_key = str(v.get("cardnumber") or "").strip()
                    if card_key and card_key != trno_key:
                        roster.add_alias(card_key, trno_key)
                    if k != trno_key:
                        roster.add_alias(k, trno_key)

                filtered_roster = _filter_roster_by_ad_ous(roster)
                if len(filtered_roster) != len(roster):
                    _save_to_disk_cache(filtered_roster)
                logger.info(f"[AD OUs Roster] Loaded {len(filtered_roster)} active student profiles from local cache (AD OUs authority)")
                return filtered_roster
    except Exception as e:
        logger.warning(f"[Roster Cache] Could not read disk cache: {e}")
    return None


def _save_to_disk_cache(data: dict):
    """Save roster dictionary to local JSON cache."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        to_save = {}
        for k, v in data.items():
            if isinstance(v, dict) and "trno" in v:
                to_save[str(v["trno"]).strip()] = v
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False)
        logger.info(f"[AD OUs Roster] Saved {len(to_save)} active AD OU student profiles (Koha alumni/expired excluded) to {CACHE_FILE}")
    except Exception as e:
        logger.warning(f"[Roster Cache] Could not save to disk cache: {e}")


def _query_koha_students():
    """
    Query students from Koha using proven Koha library logic:
    - Standard attribute codes: Class, STD, CLASS, DAR, CLASS_STD
    - TRNO attribute codes: TRNO, TRN, TR_NUMBER, TR
    - Student category filter: categorycode LIKE 'S%' OR std.attribute IS NOT NULL
    """
    try:
        from app.database.koha_connection import koha_db
    except ImportError:
        try:
            from database.koha_connection import koha_db
        except ImportError:
            return None

    if not koha_db.is_available():
        return None

    sql = """
    SELECT 
        b.borrowernumber,
        b.cardnumber,
        b.surname,
        b.firstname,
        CASE 
            WHEN (b.surname IS NOT NULL AND b.surname != '' AND b.surname != 'None')
                 AND (b.firstname IS NOT NULL AND b.firstname != '' AND b.firstname != 'None')
            THEN CONCAT(b.surname, ' ', b.firstname)
            WHEN (b.surname IS NOT NULL AND b.surname != '' AND b.surname != 'None')
            THEN b.surname
            WHEN (b.firstname IS NOT NULL AND b.firstname != '' AND b.firstname != 'None')
            THEN b.firstname
            ELSE CONCAT('Student #', b.cardnumber)
        END AS StudentName,
        b.email,
        b.phone AS mobile,
        COALESCE(std.attribute, b.branchcode, '') AS darajah,
        COALESCE(trno.attribute, b.cardnumber) AS trno,
        COALESCE(c.description, '') AS branch,
        b.categorycode,
        b.sex
    FROM borrowers b
    LEFT JOIN categories c 
        ON b.categorycode = c.categorycode
    LEFT JOIN borrower_attributes std
        ON std.borrowernumber = b.borrowernumber
        AND std.code IN ('Class', 'STD', 'CLASS', 'DAR', 'CLASS_STD')
    LEFT JOIN borrower_attributes trno
        ON trno.borrowernumber = b.borrowernumber
        AND trno.code IN ('TRNO', 'TRN', 'TR_NUMBER', 'TR')
    WHERE (b.categorycode LIKE 'S%%' OR std.attribute IS NOT NULL)
      AND b.categorycode NOT IN ('T-KG', 'L', 'T', 'HO', 'M-KG')
    """
    try:
        t0 = time.time()
        rows = koha_db.query(sql)
        elapsed = round(time.time() - t0, 2)
        if not rows:
            logger.warning(f"[Koha Roster] Query completed in {elapsed}s but returned 0 student rows")
            return None

        roster = RosterDict()
        for r in rows:
            trno_key = str(r.get("trno") or r.get("cardnumber") or "").strip()
            if not trno_key:
                continue

            darajah_val = str(r.get("darajah") or "").strip()
            details = parse_darajah_details(darajah_val)

            name_val = str(r.get("StudentName") or "").strip()
            if not name_val or name_val == "None":
                name_val = f"Student #{trno_key}"
            else:
                clean_name = re.sub(r'^\d+\s*[-–—]\s*', '', name_val).strip()
                if clean_name:
                    name_val = clean_name

            student_obj = {
                "trno":           trno_key,
                "name":           name_val,
                "name_ar":        "",
                "darajah":        details["darajah"],
                "class":          details["darajah"],
                "section":        details["section"],
                "year":           details["year"],
                "gender":         details["gender"],
                "branch":         r.get("branch") or "General",
                "itsid":          str(r.get("cardnumber") or trno_key),
                "cardnumber":     str(r.get("cardnumber") or ""),
                "borrowernumber": r.get("borrowernumber"),
                "age":            "",
                "email":          r.get("email") or "",
                "mobile":         r.get("mobile") or "",
                "nationality":    "",
                "jamaat":         "",
                "accommodation":  "",
                "nizaam":         "",
                "hizb":           "",
                "vatan":          "",
                "tabaqat":        "",
            }
            roster[trno_key] = student_obj

            card_key = str(r.get("cardnumber") or "").strip()
            if card_key and card_key != trno_key:
                roster.add_alias(card_key, trno_key)

        filtered_roster = _filter_roster_by_ad_ous(roster)
        logger.info(f"[Active Directory OUs] Authoritative student roster locked to {len(filtered_roster)} active OU accounts (all Koha alumni/expired excluded) in {elapsed}s")
        return filtered_roster
    except Exception as e:
        logger.warning(f"[Active Directory OUs] Roster query error: {e}")
        return None


def _background_refresh():
    """Background worker to refresh roster from Koha without blocking web threads."""
    global _is_refreshing
    with _refresh_lock:
        if _is_refreshing:
            return
        _is_refreshing = True

    def _task():
        global _is_refreshing
        try:
            roster = _query_koha_students()
            if roster:
                _student_cache["data"] = roster
                _student_cache["ts"] = time.time()
                _save_to_disk_cache(roster)
        except Exception as e:
            logger.warning(f"[Koha Roster] Background refresh error: {e}")
        finally:
            _is_refreshing = False

    t = threading.Thread(target=_task, name="KohaRosterSync", daemon=True)
    t.start()


def load_student_roster():
    """
    Retrieve student roster.
    1. Returns hot in-memory cache if fresh (<30 min) and non-empty.
    2. Loads persistent disk cache immediately (<1ms) if memory is empty.
    3. Triggers asynchronous background refresh if stale, ensuring 0 user wait time.
    4. Falls back to local disk cache or Excel if Koha is offline / timed out.
    """
    now = time.time()
    if _student_cache["data"] and len(_student_cache["data"]) > 0:
        if (now - _student_cache["ts"]) < CACHE_TTL:
            return _student_cache["data"]
        else:
            _background_refresh()
            return _student_cache["data"]

    # 2. Try persistent disk cache
    disk_data = _load_from_disk_cache()
    if disk_data and len(disk_data) > 0:
        _student_cache["data"] = disk_data
        _student_cache["ts"] = now
        _background_refresh()
        return disk_data

    # 3. Synchronous load for first run
    roster = _query_koha_students()
    if roster and len(roster) > 0:
        _student_cache["data"] = roster
        _student_cache["ts"] = now
        _save_to_disk_cache(roster)
        return roster

    # 4. Fallback to Excel if present
    if os.path.exists(XLSX_PATH):
        try:
            df = pd.read_excel(XLSX_PATH, dtype=str)
            df.columns = [c.strip() for c in df.columns]
            if "TRNO" in df.columns:
                df["TRNO"] = df["TRNO"].astype(str).str.strip()
                df = df.dropna(subset=["TRNO"])
                df = df[df["TRNO"].str.match(r"^\d{4,8}$")]
                df = df.fillna("")
                roster = RosterDict()
                for _, row in df.iterrows():
                    trno = row["TRNO"]
                    d_val = row.get("Darajah", "")
                    details = parse_darajah_details(d_val)
                    itsid = str(row.get("ITSID", "") or "").strip()
                    stu_obj = {
                        "trno":          trno,
                        "name":          row.get("Name", f"Student #{trno}"),
                        "name_ar":       row.get("NameAR", ""),
                        "darajah":       details["darajah"],
                        "class":         details["darajah"],
                        "section":       details["section"],
                        "year":          details["year"],
                        "gender":        details["gender"],
                        "branch":        row.get("Branch", "General"),
                        "itsid":         itsid or trno,
                        "cardnumber":    itsid,
                        "age":           row.get("Age", ""),
                        "email":         row.get("Email", ""),
                        "mobile":        row.get("Mobile", ""),
                    }
                    roster[trno] = stu_obj
                    if itsid and itsid != trno:
                        roster.add_alias(itsid, trno)
                _student_cache["data"] = roster
                _student_cache["ts"] = now
                _save_to_disk_cache(roster)
                return roster
        except Exception:
            pass

    # 5. Last resort: reload disk cache if available
    disk_data = _load_from_disk_cache()
    if disk_data and len(disk_data) > 0:
        _student_cache["data"] = disk_data
        _student_cache["ts"] = now
        return disk_data

    _student_cache["data"] = RosterDict()
    _student_cache["ts"] = now
    return RosterDict()


def get_student_info(trno: str) -> dict:
    """Return full student info dict for a given TRNO, or synthetic default."""
    if not trno:
        return {}
    s_trno = str(trno).strip()
    roster = load_student_roster()
    if s_trno in roster:
        return roster[s_trno]

    return {
        "trno":           s_trno,
        "name":           f"Student #{s_trno}",
        "name_ar":        "",
        "darajah":        "Unknown",
        "class":          "Unknown",
        "section":        "",
        "year":           "",
        "gender":         "Mixed",
        "branch":         "General",
        "itsid":          s_trno,
        "cardnumber":     s_trno,
        "borrowernumber": None,
        "age":            "",
        "email":          "",
        "mobile":         "",
    }


def get_student_borrowernumber(identifier: str):
    """Resolve borrowernumber from identifier (TRNO, cardnumber, or borrowernumber) via roster or Koha."""
    if not identifier:
        return None
    s_id = str(identifier).strip()
    roster = load_student_roster()
    stu = roster.get(s_id)
    if stu and stu.get("borrowernumber"):
        return stu.get("borrowernumber")

    # If identifier is already numeric borrowernumber or cardnumber, query Koha
    try:
        from app.database.koha_connection import koha_db
        if koha_db.is_available():
            row = koha_db.query_one(
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
                (s_id, s_id, s_id)
            )
            if row and row.get("borrowernumber"):
                return row.get("borrowernumber")
    except Exception as e:
        logger.warning(f"Error resolving borrowernumber for {s_id}: {e}")
    return None


def get_all_students_list():
    """Return all unique students sorted by Darajah then TRNO descending."""
    roster = load_student_roster()
    students = list(roster.values())

    seen = set()
    unique_students = []
    for s in students:
        tid = s.get("trno")
        if tid not in seen:
            seen.add(tid)
            unique_students.append(s)

    def sort_key(s):
        try:
            darajah = int(str(s.get("year") or str(s.get("darajah", "")).split()[0]))
        except (ValueError, IndexError):
            darajah = 999
        try:
            trno = -int(s.get("trno") or 0)
        except ValueError:
            trno = 0
        return (darajah, trno)

    unique_students.sort(key=sort_key)
    return unique_students


def get_students_by_darajah():
    """Return students grouped by Darajah."""
    students = get_all_students_list()
    groups = {}
    for s in students:
        d = s.get("darajah") or "Unknown"
        groups.setdefault(d, []).append(s)
    return groups
