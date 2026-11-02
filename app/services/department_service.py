# app/services/department_service.py
"""
Department Taxonomy & Academic Structure Service for MamalBrowse Sentinel.

Standardizes all Darajah cohorts, classes, Koha LMS groups, and Lab Manager OUs
into the official 5 institutional academic departments:
  1. "Culture Générale (Std 1-2)"              (Primary Foundational · Std 1–2)
  2. "Culture Générale (Std 3-4)"              (Primary Intermediate · Std 3–4)
  3. "Collegiate I (5-7)"                      (Junior Collegiate · Std 5–7)
  4. "Collegiate II & Higher Studies (Std 8-11)"(Senior Collegiate & Higher Studies · Std 8–11)
     [Aliased by: "Collegiate II and Higher Studies"]
  5. "Dars Burhani"                            (Dars Burhani Division · DB1, DB2, DB3, etc.)
  + "Asateza & Staff"                          (Faculty, Staff, & Lab Administration)

Guarantees 100% consistency across Analytics, Policy Enforcement, Roster, and Reports.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


CANONICAL_DEPARTMENTS: Dict[str, Dict[str, Any]] = {
    "Culture Générale (Std 1-2)": {
        "id": "cg_1_2",
        "name": "Culture Générale (Std 1-2)",
        "short_name": "CG 1-2",
        "standards": [1, 2],
        "quota_minutes": 30,
        "color": "#10b981",          # Emerald
        "badge": "emerald",
        "sub": "Primary Foundational · Std 1–2",
        "description": "Foundational primary standard focusing on early language, foundational reading, and guided learning.",
        "order": 1,
    },
    "Culture Générale (Std 3-4)": {
        "id": "cg_3_4",
        "name": "Culture Générale (Std 3-4)",
        "short_name": "CG 3-4",
        "standards": [3, 4],
        "quota_minutes": 30,
        "color": "#06b6d4",          # Cyan / Sky
        "badge": "cyan",
        "sub": "Primary Intermediate · Std 3–4",
        "description": "Intermediate primary standard with structured digital literacy and supervised curriculum research.",
        "order": 2,
    },
    "Collegiate I (5-7)": {
        "id": "collegiate_1",
        "name": "Collegiate I (5-7)",
        "short_name": "Collegiate I",
        "standards": [5, 6, 7],
        "quota_minutes": 60,
        "color": "#6366f1",          # Indigo / Purple
        "badge": "purple",
        "sub": "Junior Collegiate · Std 5–7",
        "description": "Middle secondary / lower collegiate curriculum with educational reference, language research, and academic assignments.",
        "order": 3,
    },
    "Collegiate II & Higher Studies (Std 8-11)": {
        "id": "collegiate_2",
        "name": "Collegiate II & Higher Studies (Std 8-11)",
        "short_name": "Collegiate II",
        "standards": [8, 9, 10, 11, 12],
        "quota_minutes": 120,
        "color": "#f59e0b",          # Amber / Gold
        "badge": "amber",
        "sub": "Senior Collegiate & Higher Studies · Std 8–11",
        "description": "Senior collegiate division and higher research studies, academic journals, programming, and university preparatory work.",
        "order": 4,
    },
    "Dars Burhani": {
        "id": "dars_burhani",
        "name": "Dars Burhani",
        "short_name": "Dars Burhani",
        "standards": [],
        "quota_minutes": 60,
        "color": "#ec4899",          # Rose / Magenta
        "badge": "rose",
        "sub": "Dars Burhani Division",
        "description": "Specialized theological division with specialized curriculum, library research, and textual studies.",
        "order": 5,
    },
    "Asateza & Staff": {
        "id": "asateza",
        "name": "Asateza & Staff",
        "short_name": "Faculty / Staff",
        "standards": [99],
        "quota_minutes": 240,
        "color": "#8b5cf6",          # Violet
        "badge": "slate",
        "sub": "Faculty & Administration",
        "description": "Instructional faculty, lab coordinators, and administrative staff.",
        "order": 6,
    },
}

DEPARTMENT_ORDER: List[str] = [
    "Culture Générale (Std 1-2)",
    "Culture Générale (Std 3-4)",
    "Collegiate I (5-7)",
    "Collegiate II & Higher Studies (Std 8-11)",
    "Dars Burhani",
    "Asateza & Staff",
]

# Explicit alias normalizations
DEPARTMENT_ALIASES: Dict[str, str] = {
    "Collegiate II and Higher Studies": "Collegiate II & Higher Studies (Std 8-11)",
    "Collegiate II & Higher Studies (Std 8-11)": "Collegiate II & Higher Studies (Std 8-11)",
    "Collegiate II": "Collegiate II & Higher Studies (Std 8-11)",
    "Collegiate 2": "Collegiate II & Higher Studies (Std 8-11)",
    "Collegiate I (5-7)": "Collegiate I (5-7)",
    "Collegiate I": "Collegiate I (5-7)",
    "Collegiate 1": "Collegiate I (5-7)",
    "Culture Générale (Std 3-4)": "Culture Générale (Std 3-4)",
    "Culture Generale (Std 3-4)": "Culture Générale (Std 3-4)",
    "Culture Générale (Std 1-2)": "Culture Générale (Std 1-2)",
    "Culture Generale (Std 1-2)": "Culture Générale (Std 1-2)",
    "Culture Générale": "Culture Générale (Std 1-2)",
    "Culture Generale": "Culture Générale (Std 1-2)",
    "Dars Burhani": "Dars Burhani",
    "Dars-Burhani": "Dars Burhani",
    "DB": "Dars Burhani",
    # Legacy Marhalah translations
    "Al-Marhalah al-Ula": "Culture Générale (Std 1-2)",
    "Al-Marhalah al-Thaniyah": "Collegiate I (5-7)",
    "Al-Marhalah al-Thalithah": "Collegiate II & Higher Studies (Std 8-11)",
    "Asateza & Other": "Asateza & Staff",
    "Faculty": "Asateza & Staff",
}


def classify_department(key_or_name: str | None) -> Dict[str, Any]:
    """
    Classifies any input string (Darajah name, class code, OU distinguished name,
    cohort label, or TRNO group) into its canonical department structure.

    Examples:
      - '1 A M'               -> Culture Générale (Std 1-2)
      - '2 D F'               -> Culture Générale (Std 1-2)
      - '3 B M'               -> Culture Générale (Std 3-4)
      - '4 C F'               -> Culture Générale (Std 3-4)
      - '5 A M'               -> Collegiate I (5-7)
      - '7 B F'               -> Collegiate I (5-7)
      - '8 A M'               -> Collegiate II & Higher Studies (Std 8-11)
      - '11 A M'              -> Collegiate II & Higher Studies (Std 8-11)
      - '1 DARS M'            -> Dars Burhani
      - '3 DARS F'            -> Dars Burhani
      - 'OU=DB2,...'          -> Dars Burhani
      - 'OU=Darajah 6,...'    -> Collegiate I (5-7)
      - 'Asateza'             -> Asateza & Staff
    """
    raw = str(key_or_name or "").strip()
    if not raw:
        return CANONICAL_DEPARTMENTS["Culture Générale (Std 1-2)"]

    # 1. Exact alias check
    if raw in DEPARTMENT_ALIASES:
        canonical_name = DEPARTMENT_ALIASES[raw]
        return CANONICAL_DEPARTMENTS[canonical_name]

    upper = raw.upper()

    # 2. Dars Burhani detection (DB1, DB2, DB3, 1 DARS M, 2 DARS F, OU=1 DARSM)
    if "DARS" in upper or re.search(r"\bDB\d*\b", upper) or "OU=DB" in upper or "BURHANI" in upper:
        return CANONICAL_DEPARTMENTS["Dars Burhani"]

    # 3. Asateza / Staff / Test detection
    if any(k in upper for k in ("ASATEZA", "STAFF", "FACULTY", "ADMIN", "TEST", "TEACHER")):
        return CANONICAL_DEPARTMENTS["Asateza & Staff"]

    # 4. Extract numeric Darajah / Standard from string
    # Look for Darajah or Std pattern first, e.g. "Darajah 4", "Std 5", "OU=Darajah 6"
    ou_match = re.search(r"OU=(?:DARAJA|DARAJAH|STD)\s*(\d+)", upper)
    if ou_match:
        d_num = int(ou_match.group(1))
    else:
        num_match = re.search(r"\b(\d+)\b", raw) or re.match(r"^(\d+)", raw)
        d_num = int(num_match.group(1)) if num_match else 0

    if 1 <= d_num <= 2:
        return CANONICAL_DEPARTMENTS["Culture Générale (Std 1-2)"]
    elif 3 <= d_num <= 4:
        return CANONICAL_DEPARTMENTS["Culture Générale (Std 3-4)"]
    elif 5 <= d_num <= 7:
        return CANONICAL_DEPARTMENTS["Collegiate I (5-7)"]
    elif 8 <= d_num <= 12:
        return CANONICAL_DEPARTMENTS["Collegiate II & Higher Studies (Std 8-11)"]

    # 5. Check if alias contained inside string
    for alias_key, canon in DEPARTMENT_ALIASES.items():
        if alias_key.lower() in raw.lower():
            return CANONICAL_DEPARTMENTS[canon]

    # Default fallback
    return CANONICAL_DEPARTMENTS["Culture Générale (Std 1-2)"]


def get_all_canonical_departments() -> List[Dict[str, Any]]:
    """Return ordered list of canonical departments with complete metadata."""
    return [CANONICAL_DEPARTMENTS[name] for name in DEPARTMENT_ORDER]


def get_department_quota(dept_name_or_key: str) -> int:
    """Return default daily lab quota in minutes for given department."""
    dept = classify_department(dept_name_or_key)
    return dept.get("quota_minutes", 60)
