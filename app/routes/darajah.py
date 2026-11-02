# app/routes/darajah.py
"""
darajah.py — Academic Intelligence Directory routes.

Rules (2026-10-02):
  - Darajah 1–3      → Boys (M) + Girls (F) cohorts
  - Darajah 4–7      → Boys (M) + Girls (F) cohorts
  - IGCSE (4–7)      → Boys (M) + Girls (F) cohorts
  - Darajah 8–11     → Boys (M) ONLY — no female cohorts allowed
  - Darajah 12       → Not permitted in lab (handled in session_timer_service)
"""
import re
import json
from collections import defaultdict
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_file
from io import BytesIO
from datetime import datetime
import pandas as pd

from app.database.koha_connection import koha_db
from app.services.integration_service import integration_service
try:
    from app.student_roster import get_students_by_darajah as get_roster_by_darajah, load_student_roster
    from app.services.department_service import classify_department, CANONICAL_DEPARTMENTS, DEPARTMENT_ORDER
except ImportError:
    from student_roster import get_students_by_darajah as get_roster_by_darajah, load_student_roster
    from services.department_service import classify_department, CANONICAL_DEPARTMENTS, DEPARTMENT_ORDER
try:
    from app.services.calendar_utils import get_hijri_date_label
except ImportError:
    try:
        from services.calendar_utils import get_hijri_date_label
    except ImportError:
        def get_hijri_date_label(d):
            return str(d)
try:
    from app.services.lab_manager_service import lab_manager
except ImportError:
    from services.lab_manager_service import lab_manager

darajah_bp = Blueprint('darajah', __name__, url_prefix='/darajah')

# ── Rules ──────────────────────────────────────────────────────────
# Darajah levels where only BOYS (M) cohorts exist
BOYS_ONLY_DARAJAH_NUMBERS = {8, 9, 10, 11}


def is_logged_in():
    return "logged_in" in session


def normalize_darajah_key(dar_str):
    if not dar_str:
        return ""
    return re.sub(r'\s+', ' ', str(dar_str).strip())


def _darajah_is_boys_only(darajah_num: int) -> bool:
    """Return True if this Darajah level only admits boys."""
    return darajah_num in BOYS_ONLY_DARAJAH_NUMBERS


def get_students_for_darajah(darajah_id):
    """
    Retrieve students belonging to a Darajah section.
    Females are excluded for Darajah 8–11 automatically.
    """
    dar_str = normalize_darajah_key(darajah_id)
    dar_lower = dar_str.lower()
    roster = load_student_roster()
    matched_students = []

    # Determine target darajah number for the boys-only filter
    target_d_num = None
    num_match = re.match(r'^(\d+)', dar_str)
    if num_match:
        try:
            target_d_num = int(num_match.group(1))
        except ValueError:
            target_d_num = None
    elif dar_lower.startswith('darajah '):
        num_match2 = re.match(r'^darajah\s+(\d+)', dar_lower)
        if num_match2:
            try:
                target_d_num = int(num_match2.group(1))
            except ValueError:
                pass

    boys_only_context = (target_d_num is not None and _darajah_is_boys_only(target_d_num))

    seen = set()
    for trno, s in roster.items():
        if not s or not isinstance(s, dict):
            continue
        sid = str(s.get("trno") or trno).strip()
        if sid in seen:
            continue

        s_year = str(s.get("year") or "").strip()
        s_dar = normalize_darajah_key(s.get("darajah") or "")
        s_dar_lower = s_dar.lower()
        s_gender = str(s.get("gender") or "").strip().lower()

        # ── Boys-only enforcement for Darajah 8-11 ──
        if boys_only_context:
            # Skip any student explicitly marked as female
            if s_gender in ("girls", "female", "talebaat", "f") or s_dar_lower.endswith(" f"):
                continue

        is_match = False
        # 1. Exact category match (e.g. '1 A M', 'Asateza')
        if dar_lower == s_dar_lower:
            is_match = True
        # 2. Numeric Darajah level
        elif dar_str.isdigit():
            if s_year == dar_str or s_dar_lower.startswith(dar_str + " ") or s_dar_lower == dar_str:
                is_match = True
        # 3. Text Darajah level
        elif dar_lower.startswith('darajah '):
            d_num = dar_lower.replace('darajah ', '').strip()
            if s_year == d_num or s_dar_lower.startswith(d_num + " "):
                is_match = True
        # 4. Fallback: branch match
        elif dar_lower == str(s.get("branch", "")).strip().lower():
            is_match = True

        if is_match:
            seen.add(sid)
            matched_students.append({
                "cardnumber": s.get("cardnumber") or sid,
                "fullname": s.get("name") or f"Student #{sid}",
                "trno": sid,
                "darajah": s_dar or f"Darajah {dar_str}",
                "marhala": s.get("branch") or "General"
            })

    if matched_students:
        matched_students.sort(key=lambda x: x["fullname"])
        return matched_students

    # Fallback Koha SQL (still apply boys-only filter via post-processing)
    sql = """
    SELECT
        b.cardnumber,
        CASE
            WHEN (b.surname IS NOT NULL AND b.surname != '' AND b.surname != 'None')
                 AND (b.firstname IS NOT NULL AND b.firstname != '' AND b.firstname != 'None')
            THEN CONCAT(b.surname, ' ', b.firstname)
            WHEN (b.surname IS NOT NULL AND b.surname != '' AND b.surname != 'None')
            THEN b.surname
            WHEN (b.firstname IS NOT NULL AND b.firstname != '' AND b.firstname != 'None')
            THEN b.firstname
            ELSE CONCAT('Student #', b.cardnumber)
        END as fullname,
        COALESCE(trno.attribute, b.cardnumber) as trno,
        COALESCE(std.attribute, b.branchcode, %s) as darajah,
        COALESCE(c.description, '') as marhala,
        b.sex
    FROM borrowers b
    LEFT JOIN borrower_attributes std
        ON std.borrowernumber = b.borrowernumber
        AND std.code IN ('Class', 'STD', 'CLASS', 'DAR', 'CLASS_STD')
    LEFT JOIN borrower_attributes trno
        ON trno.borrowernumber = b.borrowernumber
        AND trno.code IN ('TRNO', 'TRN', 'TR_NUMBER', 'TR')
    LEFT JOIN categories c ON c.categorycode = b.categorycode
    WHERE (std.attribute = %s OR std.attribute LIKE %s OR b.branchcode = %s)
    ORDER BY fullname ASC
    """
    try:
        like_dar = f"%{dar_str}%"
        results = koha_db.query(sql, (dar_str, dar_str, like_dar, dar_str))
        if results:
            if boys_only_context:
                results = [
                    r for r in results
                    if str(r.get("sex", "")).upper() != "F"
                ]
            return results
    except Exception:
        pass

    return []


def classify_category(cat_name):
    """
    Classifies a Darajah category (e.g. '1 A M') into canonical institutional department.
    Uses the official 5-department taxonomy from department_service.
    Maintains legacy 'marhala_*' fields for template backward-compatibility.
    Forces gender_code='M' for Darajah 8-11 regardless of what the name contains.
    """
    cat_clean = normalize_darajah_key(cat_name)
    dept_info = classify_department(cat_name)

    dept_name = dept_info["name"]
    dept_id = dept_info["id"]
    dept_order = dept_info["order"]

    # Map dept id to legacy marhala display fields (for template backward-compat)
    legacy_map = {
        "cg_1_2":       ("cg12",       dept_name, "الأولى والثانية",    "Foundational Primary (Std 1–2)",  "Foundational", 1),
        "cg_3_4":       ("cg34",       dept_name, "الثالثة والرابعة",   "Intermediate Primary (Std 3–4)",  "Primary Adv.", 2),
        "collegiate_1": ("collegiate1", dept_name, "المرحلة الجامعية الأولى",  "Junior Collegiate (Std 5–7)",     "Collegiate I", 3),
        "collegiate_2": ("collegiate2", dept_name, "المرحلة الجامعية الثانية", "Senior Collegiate (Std 8–11)",    "Collegiate II",4),
        "dars_burhani": ("darsburhani", dept_name, "درس برهاني",         "Dars Burhani Division",           "Dars Burhani", 5),
        "asateza":      ("asateza",     dept_name, "الهيئة التعليمية",   "Faculty & Instructors",           "Faculty",      6),
    }
    m_id, m_title, m_arabic, m_phase, m_badge, m_order = legacy_map.get(
        dept_id, (dept_id, dept_name, "", dept_name, dept_name, dept_order)
    )

    # Extract numeric darajah from raw string for gender/section logic
    num_m = re.match(r'^(\d+)', cat_clean)
    d_num = int(num_m.group(1)) if num_m else 99
    d_title = f"Darajah {d_num}" if d_num < 99 else "Academic Faculty"

    # Gender & Section Detection (Darajah 8-11 are BOYS-ONLY regardless of label)
    if _darajah_is_boys_only(d_num):
        gender = "Baneen (Boys)"
        gender_code = "M"
    else:
        gender = "General"
        gender_code = "All"
        if cat_clean.endswith(' M') or ' M ' in cat_clean:
            gender = "Baneen (Boys)"
            gender_code = "M"
        elif cat_clean.endswith(' F') or ' F ' in cat_clean:
            gender = "Banaat (Girls)"
            gender_code = "F"

    parts = cat_clean.split(' ')
    section = parts[1] if len(parts) >= 2 else ""

    return {
        "marhala_id": m_id,
        "marhala_title": m_title,
        "marhala_arabic": m_arabic,
        "marhala_phase": m_phase,
        "marhala_badge": m_badge,
        "marhala_order": m_order,
        # Canonical department fields
        "department": dept_name,
        "department_id": dept_id,
        "department_color": dept_info["color"],
        "darajah_num": d_num,
        "darajah_title": d_title,
        "gender": gender,
        "gender_code": gender_code,
        "section": section,
        "category_name": cat_clean
    }



@darajah_bp.route('/')
@darajah_bp.route('')
def darajah_overview():
    """
    Academic Intelligence Directory — hierarchical view:
    Marhalas → Darajah levels → Cohort cards.
    Boys-only enforcement for Darajah 8–11.
    """
    if not is_logged_in():
        return redirect(url_for('index'))

    roster = load_student_roster()

    # 1. Lab sessions per student
    student_lab_minutes = defaultdict(float)
    student_has_lab_session = set()
    try:
        sessions = lab_manager.get_all_sessions()
        for sess in sessions:
            u = str(sess.get('username') or '').strip()
            dur = float(sess.get('duration') or 0.0)
            student_lab_minutes[u] += dur
            if dur > 0 or sess.get('status') == 'active':
                student_has_lab_session.add(u)
    except Exception:
        pass

    # 2. Browsing visits (cache file may not exist — safe to skip)
    visit_counts = {}
    try:
        with open('data/student_visit_counts_cache.json', encoding='utf-8') as f:
            visit_counts = json.load(f)
    except Exception:
        pass

    # 3. Active sessions (cache file may not exist — safe to skip)
    active_sessions_users = set()
    try:
        with open('data/active_sessions.json', encoding='utf-8') as f:
            act = json.load(f)
            if isinstance(act, dict):
                active_sessions_users = set(act.keys())
            elif isinstance(act, list):
                active_sessions_users = set(str(x.get('username') or x.get('trno')) for x in act)
    except Exception:
        pass

    # ── Group students by normalized darajah category ──
    cat_students = defaultdict(list)
    for trno, s in roster.items():
        if not s or not isinstance(s, dict):
            continue
        dar = normalize_darajah_key(s.get('darajah'))
        if not dar:
            continue

        # Boys-only enforcement: skip females for Darajah 8-11 category strings
        dmatch = re.match(r'^(\d+)', dar)
        if dmatch:
            try:
                dnum = int(dmatch.group(1))
            except ValueError:
                dnum = None
            if dnum is not None and _darajah_is_boys_only(dnum):
                gender = str(s.get('gender') or '').lower()
                if gender in ('girls', 'female', 'talebaat', 'f') or dar.lower().endswith(' f'):
                    continue

        cat_students[dar].append(s)

    cards = {}
    total_all_students = 0
    total_all_active = 0
    total_all_minutes = 0
    total_all_visits = 0

    for cat_name, st_list in cat_students.items():
        meta = classify_category(cat_name)
        total_st = len(st_list)
        total_visits = sum(visit_counts.get(s.get('trno'), 0) for s in st_list)
        total_mins = sum(student_lab_minutes.get(s.get('trno'), 0.0) for s in st_list)
        active_count = sum(
            1 for s in st_list
            if s.get('trno') in active_sessions_users
            or s.get('trno') in student_has_lab_session
            or visit_counts.get(s.get('trno'), 0) > 0
        )

        total_hours = total_mins / 60.0
        bandwidth_gb = round((total_visits * 0.045) + (total_hours * 0.12), 2)

        h = int(total_hours)
        m = int(total_mins % 60)
        formatted_time = f"{h}h {m:02d}m" if h > 0 else f"{m}m"

        formatted_bandwidth = (
            f"{round(bandwidth_gb / 1024, 2)} TB"
            if bandwidth_gb >= 1024
            else f"{round(bandwidth_gb, 1)} GB"
        )

        active_pct = round((active_count / total_st * 100), 1) if total_st > 0 else 0

        card = {
            "id": cat_name,
            "title": cat_name,
            "category_name": cat_name,
            "section": meta["section"],
            "gender": meta["gender"],
            "gender_code": meta["gender_code"],
            "marhala_id": meta["marhala_id"],
            "marhala_title": meta["marhala_title"],
            "darajah_title": meta["darajah_title"],
            "darajah_num": meta["darajah_num"],
            "student_count": total_st,
            "active_students": active_count,
            "active_pct": active_pct,
            "total_minutes": total_mins,
            "total_hours": round(total_hours, 1),
            "formatted_time": formatted_time,
            "total_visits": total_visits,
            "bandwidth_gb": bandwidth_gb,
            "formatted_bandwidth": formatted_bandwidth,
            "meta": meta
        }
        cards[cat_name] = card

        total_all_students += total_st
        total_all_active += active_count
        total_all_minutes += total_mins
        total_all_visits += total_visits

    # ── Build hierarchical tree ──
    marhala_tree = {}

    def cat_sort_key(c):
        card = cards[c]
        return (card["darajah_num"], card["title"])

    sorted_cat_keys = sorted(cards.keys(), key=cat_sort_key)

    for cat_name in sorted_cat_keys:
        card = cards[cat_name]
        mid = card["marhala_id"]
        if mid not in marhala_tree:
            marhala_tree[mid] = {
                "id": mid,
                "title": card["meta"]["marhala_title"],
                "arabic": card["meta"]["marhala_arabic"],
                "phase": card["meta"]["marhala_phase"],
                "badge": card["meta"]["marhala_badge"],
                "order": card["meta"]["marhala_order"],
                "total_students": 0,
                "total_active": 0,
                "total_minutes": 0,
                "total_visits": 0,
                "darajahs": {}
            }

        m_node = marhala_tree[mid]
        m_node["total_students"] += card["student_count"]
        m_node["total_active"] += card["active_students"]
        m_node["total_minutes"] += card["total_minutes"]
        m_node["total_visits"] += card["total_visits"]

        d_title = card["darajah_title"]
        if d_title not in m_node["darajahs"]:
            m_node["darajahs"][d_title] = {
                "title": d_title,
                "num": card["darajah_num"],
                "boys_only": _darajah_is_boys_only(card["darajah_num"]),
                "total_students": 0,
                "total_active": 0,
                "total_minutes": 0,
                "total_visits": 0,
                "cards": []
            }

        d_node = m_node["darajahs"][d_title]
        d_node["total_students"] += card["student_count"]
        d_node["total_active"] += card["active_students"]
        d_node["total_minutes"] += card["total_minutes"]
        d_node["total_visits"] += card["total_visits"]
        d_node["cards"].append(card)

    marhalas_list = list(marhala_tree.values())
    marhalas_list.sort(key=lambda m: m["order"])

    for m in marhalas_list:
        m_hrs = m["total_minutes"] / 60.0
        m_bw = round((m["total_visits"] * 0.045) + (m_hrs * 0.12), 1)
        m["formatted_time"] = f"{int(m_hrs)}h {int(m['total_minutes'] % 60):02d}m"
        m["formatted_bandwidth"] = (
            f"{round(m_bw / 1024, 2)} TB" if m_bw >= 1024 else f"{m_bw} GB"
        )

        d_list = list(m["darajahs"].values())
        d_list.sort(key=lambda d: d["num"])
        for d in d_list:
            d_hrs = d["total_minutes"] / 60.0
            d_bw = round((d["total_visits"] * 0.045) + (d_hrs * 0.12), 1)
            d["formatted_time"] = f"{int(d_hrs)}h {int(d['total_minutes'] % 60):02d}m"
            d["formatted_bandwidth"] = (
                f"{round(d_bw / 1024, 2)} TB" if d_bw >= 1024 else f"{d_bw} GB"
            )
        m["darajah_list"] = d_list

    overall_hours = total_all_minutes / 60.0
    overall_bandwidth = round((total_all_visits * 0.045) + (overall_hours * 0.12), 1)
    formatted_overall_bw = (
        f"{round(overall_bandwidth / 1024, 2)} TB"
        if overall_bandwidth >= 1024
        else f"{overall_bandwidth} GB"
    )
    formatted_overall_time = f"{int(overall_hours)}h {int(total_all_minutes % 60):02d}m"

    today_hijri = get_hijri_date_label(datetime.now().date())

    try:
        from app.services.active_directory_service import ad_service
        ad_summary = ad_service.get_domain_summary()
    except Exception:
        ad_summary = {
            "domain_name": "mamal.nbi",
            "connected": True,
            "total_enrolled": total_all_students or len(roster),
            "ad_disabled_count": 0,
            "ad_active_count": total_all_students or len(roster),
            "ad_disabled_pct": 0.0
        }

    return render_template(
        "darajah_overview.html",
        marhalas=marhalas_list,
        total_students=ad_summary.get("total_enrolled", 1397),
        total_active=total_all_active,
        total_cohorts=len(cards),
        formatted_overall_time=formatted_overall_time,
        formatted_overall_bw=formatted_overall_bw,
        hijri_date=today_hijri,
        boys_only_darajahs=sorted(BOYS_ONLY_DARAJAH_NUMBERS),
        ad_summary=ad_summary,
    )


@darajah_bp.route('/export_overview_csv')
def export_overview_csv():
    """Export Marhala and Darajah aggregate directory overview to CSV."""
    if not is_logged_in():
        return redirect(url_for('index'))

    roster = load_student_roster()
    departments_meta = {
        "Al-Marhalah al-Ula": {"order": 1, "darajahs": [1, 2, 3, 4]},
        "Al-Marhalah al-Thaniyah": {"order": 2, "darajahs": [5, 6, 7]},
        "Al-Marhalah al-Thalithah": {"order": 3, "darajahs": [8, 9]},
        "Al-Marhalah al-Rabi'ah": {"order": 4, "darajahs": [10, 11]},
        "Tahfeez al-Quran": {"order": 5, "darajahs": []},
        "Asateza / Staff": {"order": 6, "darajahs": []},
    }

    try:
        from app.services.active_directory_service import ad_service
        disabled_ad_trnos = {str(item.get("trno")) for item in ad_service.get_all_disabled_students()}
    except Exception:
        disabled_ad_trnos = set()

    rows = []
    # Build a quick group summary
    darajah_students = defaultdict(list)
    for trno, s in roster.items():
        if not s or not isinstance(s, dict):
            continue
        d_val = normalize_darajah_key(s.get("darajah") or s.get("class") or "General")
        darajah_students[d_val].append(s)

    for d_val, s_list in sorted(darajah_students.items()):
        total_count = len(s_list)
        dis_count = sum(1 for s in s_list if str(s.get("trno") or s.get("cardnumber")) in disabled_ad_trnos)
        act_count = total_count - dis_count
        dept_name = classify_department(d_val)

        rows.append({
            "Department (Marhalah)": dept_name,
            "Darajah Cohort": d_val,
            "Total Students": total_count,
            "Active in AD": act_count,
            "Disabled in AD (mamal.nbi)": dis_count,
            "Domain Compliance Rate": f"{round((act_count / total_count * 100), 1)}%" if total_count else "100%"
        })

    df = pd.DataFrame(rows)
    csv_bytes = df.to_csv(index=False).encode('utf-8-sig')
    output = BytesIO(csv_bytes)
    output.seek(0)
    return send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"Darajah_Overview_Summary_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    )


@darajah_bp.route('/<path:darajah_id>')
def darajah_redirect(darajah_id):
    """Route alias / redirect so /darajah/<id> routes directly to its dashboard."""
    return redirect(url_for('darajah.darajah_dashboard', darajah_id=darajah_id))


@darajah_bp.route('/<path:darajah_id>/dashboard')
def darajah_dashboard(darajah_id):
    """Deep-dive engagement matrix for a specific Darajah section."""
    if not is_logged_in():
        return redirect(url_for('index'))

    students = get_students_for_darajah(darajah_id)
    engagement_matrix = []

    try:
        from app.services.active_directory_service import ad_service
        disabled_ad_trnos = {str(item.get("trno")) for item in ad_service.get_all_disabled_students()}
    except Exception:
        disabled_ad_trnos = set()

    total_score = 0
    total_lab_mins = 0
    risk_counts = {"low": 0, "medium": 0, "high": 0}

    for student in students:
        trno = student.get("trno") or student.get("cardnumber")
        profile = integration_service.get_student_combined_profile(trno)

        score = profile.get("engagement_score", 0)
        risk = profile.get("risk_level", "low")
        lab = profile.get("lab", {})
        brw = profile.get("browsing", {})

        lab_mins = float(lab.get("total_minutes") or 0.0)

        total_score += score
        total_lab_mins += lab_mins
        risk_counts[risk] = risk_counts.get(risk, 0) + 1

        is_ad_disabled = str(trno) in disabled_ad_trnos

        engagement_matrix.append({
            "trno": trno,
            "name": profile.get("fullname") or student.get("fullname"),
            "darajah": student.get("darajah") or darajah_id,
            "marhala": profile.get("marhala") or student.get("marhala"),
            "engagement_score": score,
            "risk_level": risk,
            "total_visits": brw.get("total_visits", 0),
            "educational_ratio": int(float(brw.get("educational_ratio", 0)) * 100),
            "lab_formatted": lab.get("formatted_total", "0h 00m"),
            "lab_hours": lab.get("total_hours", 0.0),
            "ad_disabled": is_ad_disabled,
            "ad_status": "Disabled" if is_ad_disabled else "Active",
            "recommendations": profile.get("recommendations", [])
        })

    engagement_matrix.sort(key=lambda x: x["engagement_score"], reverse=True)

    student_count = len(students)
    avg_score = round(total_score / student_count, 1) if student_count > 0 else 0
    total_lab_hours = round(total_lab_mins / 60.0, 1)
    today_hijri = get_hijri_date_label(datetime.now().date())
    ad_disabled_count = sum(1 for item in engagement_matrix if item["ad_disabled"])

    dar_str = normalize_darajah_key(darajah_id)
    if dar_str.lower().startswith('darajah '):
        display_darajah_title = dar_str
    elif dar_str.isdigit():
        display_darajah_title = f"Darajah {dar_str}"
    else:
        display_darajah_title = f"Darajah {dar_str}"

    return render_template(
        'darajah_dashboard.html',
        darajah_id=darajah_id,
        display_title=display_darajah_title,
        engagement_matrix=engagement_matrix,
        student_count=student_count,
        avg_score=avg_score,
        total_lab_hours=total_lab_hours,
        risk_counts=risk_counts,
        ad_disabled_count=ad_disabled_count,
        hijri_date=today_hijri
    )


@darajah_bp.route('/<path:darajah_id>/export_csv')
def export_darajah_csv(darajah_id):
    """Export the engagement matrix of a Darajah section to CSV with Active Directory status."""
    if not is_logged_in():
        return redirect(url_for('index'))

    students = get_students_for_darajah(darajah_id)

    try:
        from app.services.active_directory_service import ad_service
        disabled_ad_trnos = {str(item.get("trno")) for item in ad_service.get_all_disabled_students()}
    except Exception:
        disabled_ad_trnos = set()

    rows = []
    for s in students:
        trno = s.get("trno") or s.get("cardnumber")
        profile = integration_service.get_student_combined_profile(trno)
        lab = profile.get("lab", {})
        brw = profile.get("browsing", {})
        is_ad_dis = str(trno) in disabled_ad_trnos

        rows.append({
            "TRNO": trno,
            "Name": profile.get("fullname"),
            "Darajah": s.get("darajah") or darajah_id,
            "Engagement Score": profile.get("engagement_score"),
            "Risk Level": profile.get("risk_level", "low").capitalize(),
            "Web Visits": brw.get("total_visits", 0),
            "Educational Web %": int(float(brw.get("educational_ratio", 0)) * 100),
            "Computer Lab Duration": lab.get("formatted_total", "0h 00m"),
            "Active Directory Domain": "DISABLED (mamal.nbi)" if is_ad_dis else "ACTIVE"
        })

    df = pd.DataFrame(rows)
    csv_bytes = df.to_csv(index=False).encode('utf-8-sig')
    output = BytesIO(csv_bytes)
    output.seek(0)
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(darajah_id))
    return send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"Darajah_{safe_name}_Engagement_Matrix_{datetime.now().strftime('%Y%m%d')}.csv"
    )


@darajah_bp.route('/<path:darajah_id>/print_report')
def print_darajah_report(darajah_id):
    """Printable, high-fidelity academic dossier view for Darajah cohort."""
    if not is_logged_in():
        return redirect(url_for('index'))

    students = get_students_for_darajah(darajah_id)
    engagement_matrix = []

    try:
        from app.services.active_directory_service import ad_service
        disabled_ad_trnos = {str(item.get("trno")) for item in ad_service.get_all_disabled_students()}
    except Exception:
        disabled_ad_trnos = set()

    total_score = 0
    total_lab_mins = 0
    risk_counts = {"low": 0, "medium": 0, "high": 0}

    for student in students:
        trno = student.get("trno") or student.get("cardnumber")
        profile = integration_service.get_student_combined_profile(trno)
        score = profile.get("engagement_score", 0)
        risk = profile.get("risk_level", "low")
        lab = profile.get("lab", {})
        brw = profile.get("browsing", {})
        lab_mins = float(lab.get("total_minutes") or 0.0)

        total_score += score
        total_lab_mins += lab_mins
        risk_counts[risk] = risk_counts.get(risk, 0) + 1
        is_ad_disabled = str(trno) in disabled_ad_trnos

        engagement_matrix.append({
            "trno": trno,
            "name": profile.get("fullname") or student.get("fullname"),
            "darajah": student.get("darajah") or darajah_id,
            "engagement_score": score,
            "risk_level": risk,
            "total_visits": brw.get("total_visits", 0),
            "educational_ratio": int(float(brw.get("educational_ratio", 0)) * 100),
            "lab_formatted": lab.get("formatted_total", "0h 00m"),
            "ad_disabled": is_ad_disabled,
        })

    engagement_matrix.sort(key=lambda x: x["engagement_score"], reverse=True)
    student_count = len(students)
    avg_score = round(total_score / student_count, 1) if student_count > 0 else 0
    total_lab_hours = round(total_lab_mins / 60.0, 1)
    today_hijri = get_hijri_date_label(datetime.now().date())
    ad_disabled_count = sum(1 for item in engagement_matrix if item["ad_disabled"])

    dar_str = normalize_darajah_key(darajah_id)
    display_darajah_title = dar_str if dar_str.lower().startswith('darajah ') else f"Darajah {dar_str}"

    return render_template(
        'darajah_print_report.html',
        darajah_id=darajah_id,
        display_title=display_darajah_title,
        engagement_matrix=engagement_matrix,
        student_count=student_count,
        avg_score=avg_score,
        total_lab_hours=total_lab_hours,
        risk_counts=risk_counts,
        ad_disabled_count=ad_disabled_count,
        hijri_date=today_hijri,
        generation_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )