# app/services/calendar_utils.py
"""
Dawoodi Bohra (Fatemi) Hijri Calendar Utilities
Calculates Fatemi Hijri dates, Academic Year boundaries, and monthly sequences.
Fatemi calendar is aligned with an offset (FATEMI_OFFSET = 1) from standard astronomical Hijri.
Academic Year begins on 1st Shawwal (Month 10) and ends on the last day of Sha'ban (Month 8 of next year).
"""

from datetime import date, datetime, timedelta
from typing import Tuple, Optional, List, Dict, Any
import logging
from collections import namedtuple

logger = logging.getLogger("calendar_utils")

# Bohra Dawoodi Hijri (Fatemi) Calendar offset from standard astronomical Hijri.
# Fatemi calendar is usually 1 day ahead.
FATEMI_OFFSET = 1

HIJRI_MONTHS = [
    "Muḥarram al-Harām",       # 1
    "Safar al-Muzaffar",       # 2
    "Rabi al-Awwal",           # 3
    "Rabī al-Akhar",           # 4
    "Jamādil Awwal",           # 5
    "Jamādā al-ʾŪkhrā",        # 6
    "Rajab al-Asab",           # 7
    "Shabān al-Karim",         # 8
    "Shehrullah al-Moazzam",   # 9
    "Shawwāl al-Mukarram",     # 10
    "Zilqādah al-Harām",       # 11
    "Zilhijjatil Harām",       # 12
]

HijriDate = namedtuple("HijriDate", ["year", "month", "day"])

try:
    from hijri_converter import convert as _hijri_convert
except ImportError:
    _hijri_convert = None


# ---------- Pure Python Tabular/Fatemi Fallback Converter ----------
def _gregorian_to_jd(year: int, month: int, day: int) -> int:
    """Compute Julian Day Number for Gregorian date."""
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + (a // 4)
    jd = int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + b - 1524
    return jd


def _jd_to_gregorian(jd: int) -> Tuple[int, int, int]:
    """Convert Julian Day Number to Gregorian date."""
    z = jd
    w = int((z - 1867216.25) / 36524.25)
    x = w // 4
    a = z + 1 + w - x
    b = a + 1524
    c = int((b - 122.1) / 365.25)
    d = int(365.25 * c)
    e = int((b - d) / 30.6001)
    day = b - d - int(30.6001 * e)
    month = e - 1 if e < 14 else e - 13
    year = c - 4716 if month > 2 else c - 4715
    return int(year), int(month), int(day)


def _jd_to_fatemi_hijri(jd: int) -> Tuple[int, int, int]:
    """
    Convert Julian Day to Tabular Islamic (Fatemi) Date.
    Standard epoch: 1948439.5 (1 Muharram 1 AH)
    """
    jd_islamic = jd - 1948440 + 10632
    cycle = jd_islamic // 10631
    cycle_days = jd_islamic % 10631
    
    # 30-year cycle: leap years in 2, 5, 8, 10, 13, 16, 19, 21, 24, 27, 29
    year = cycle * 30 + int((cycle_days + 14) / 354.366)
    
    # Determine days at start of this year
    y_in_cycle = (year - 1) % 30 + 1
    # Standard formula for days before current year in cycle
    days_before = int((11 * y_in_cycle + 3) / 30) + 354 * (y_in_cycle - 1)
    day_of_year = cycle_days - days_before
    
    # Month calculation (odd months 30, even months 29)
    month = min(12, int((day_of_year + 28.5) / 29.5))
    if month < 1:
        month = 1
        
    days_in_prior_months = int(29.5 * (month - 1) + 0.5)
    day = day_of_year - days_in_prior_months
    if day < 1:
        day = 1
    return int(year), int(month), int(day)


def _fatemi_hijri_to_jd(year: int, month: int, day: int) -> int:
    """Convert Tabular Islamic (Fatemi) Date to Julian Day Number."""
    cycle = (year - 1) // 30
    y_in_cycle = (year - 1) % 30
    days_in_cycle = int((11 * (y_in_cycle + 1) + 3) / 30) + 354 * y_in_cycle if y_in_cycle > 0 else 0
    days_in_prior_months = int(29.5 * (month - 1) + 0.5)
    jd = cycle * 10631 + days_in_cycle + days_in_prior_months + day + 1948439 - 10632
    return jd


# ---------- Core Public Functions ----------

def get_fatemi_hijri(d: Any) -> Optional[HijriDate]:
    """
    Convert Gregorian date to Fatemi Hijri object.
    Nudges Gregorian date forward by FATEMI_OFFSET to align with Bohra Fatemi calendar.
    Accepts date, datetime, or ISO string.
    """
    if not d:
        return None

    if isinstance(d, datetime):
        d = d.date()
    elif isinstance(d, str):
        s = str(d).strip().split(" ")[0].split("T")[0]
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            try:
                d = datetime.strptime(s, "%d/%m/%Y").date()
            except Exception:
                return None

    nudged_g = d + timedelta(days=FATEMI_OFFSET)

    if _hijri_convert:
        try:
            h = _hijri_convert.Gregorian(nudged_g.year, nudged_g.month, nudged_g.day).to_hijri()
            return HijriDate(year=h.year, month=h.month, day=h.day)
        except Exception as e:
            logger.debug(f"hijri_convert failed, falling back to arithmetic: {e}")
            
    # Pure Python Arithmetic Fallback
    try:
        jd = _gregorian_to_jd(nudged_g.year, nudged_g.month, nudged_g.day)
        hy, hm, hd = _jd_to_fatemi_hijri(jd)
        return HijriDate(year=hy, month=hm, day=hd)
    except Exception as e:
        logger.error(f"Error in Fatemi conversion: {e}")
        return HijriDate(year=1447, month=1, day=1)


def get_fatemi_gregorian(h_year: int, h_month: int, h_day: int) -> date:
    """
    Convert Fatemi Hijri components to Gregorian date.
    Nudges the resulting Gregorian date back by FATEMI_OFFSET.
    """
    if _hijri_convert:
        try:
            g = _hijri_convert.Hijri(h_year, h_month, h_day).to_gregorian()
            return date(g.year, g.month, g.day) - timedelta(days=FATEMI_OFFSET)
        except Exception:
            pass
            
    # Pure Python Arithmetic Fallback
    jd = _fatemi_hijri_to_jd(h_year, h_month, h_day)
    gy, gm, gd = _jd_to_gregorian(jd)
    return date(gy, gm, gd) - timedelta(days=FATEMI_OFFSET)


def get_hijri_date_label(d: Any) -> str:
    """Returns full date label: e.g. '19 Safar al-Muzaffar 1448 H'"""
    if not d:
        return ""
    try:
        h = get_fatemi_hijri(d)
        if h and 1 <= h.month <= 12:
            return f"{h.day} {HIJRI_MONTHS[h.month - 1]} {h.year} H"
    except Exception:
        pass
    return str(d)


def get_hijri_month_year_label(d: Any) -> str:
    """Returns Month Year label: e.g. 'Safar al-Muzaffar 1448 H'"""
    if not d:
        return ""
    try:
        h = get_fatemi_hijri(d)
        if h and 1 <= h.month <= 12:
            return f"{HIJRI_MONTHS[h.month - 1]} {h.year} H"
    except Exception:
        pass
    return str(d)


def get_hijri_short_date(d: Any) -> str:
    """Returns short Hijri date: e.g. '19-02-48 H'"""
    if not d:
        return ""
    try:
        h = get_fatemi_hijri(d)
        if h:
            return f"{h.day:02d}-{h.month:02d}-{str(h.year)[-2:]} H"
    except Exception:
        pass
    return str(d)


# ---------- Academic Year Helpers ----------

def get_current_ay_year() -> int:
    """
    Return the current academic year base integer.
    Academic Year starts in Shawwal (month 10).
    If current month is before Shawwal (< 10), we are in the cycle that started last year.
    """
    today = date.today()
    h = get_fatemi_hijri(today)
    if not h:
        return 1447
    return h.year - 1 if h.month < 10 else h.year


def get_ay_bounds_for_hijri_year(hijri_year: int) -> Tuple[date, date]:
    """
    Get academic year bounds for a specific Hijri year using Fatemi calendar.
    AY starts on 1st Shawwal of hijri_year and ends on last day of Sha'ban of hijri_year + 1.
    """
    try:
        # Start: 1st Shawwal (Month 10)
        start = get_fatemi_gregorian(hijri_year, 10, 1)
        
        # End: day before 1st Ramazan (Month 9) of next year
        end_ramazan_start = get_fatemi_gregorian(hijri_year + 1, 9, 1)
        end = end_ramazan_start - timedelta(days=1)
        return start, end
    except Exception as e:
        logger.error(f"Error getting AY bounds for Hijri year {hijri_year}: {e}")
        # Fallback approximate
        start = date(2025, 3, 30) if hijri_year == 1446 else date(2026, 3, 20)
        end = date(2026, 3, 19) if hijri_year == 1446 else date(2027, 3, 9)
        return start, end


def get_ay_bounds(hijri_year: Optional[int] = None) -> Tuple[date, date]:
    """
    Get academic year bounds for given or current academic year.
    """
    if not hijri_year:
        hijri_year = get_current_ay_year()
    return get_ay_bounds_for_hijri_year(hijri_year)


def get_academic_hijri_months_sequence(base_hijri_year: int) -> List[Dict[str, Any]]:
    """
    Returns the ordered list of 12 Hijri months in an Academic Year cycle,
    beginning with Shawwal (Month 10) and ending with Shehrullah (Month 9 of next year).
    Each entry provides:
      - 'month_index': 1..12 in astronomical calendar
      - 'academic_index': 1..12 in academic year sequence
      - 'hijri_year': Hijri year for this month
      - 'name': Full Fatemi Hijri month name
      - 'short_name': Abbreviated display name
      - 'label': "Month Name YYYY H"
    """
    sequence = []
    
    # Months in order: 10, 11, 12 of base_year, then 1, 2, 3, 4, 5, 6, 7, 8, 9 of base_year + 1
    m_order = [
        (10, base_hijri_year),
        (11, base_hijri_year),
        (12, base_hijri_year),
        (1, base_hijri_year + 1),
        (2, base_hijri_year + 1),
        (3, base_hijri_year + 1),
        (4, base_hijri_year + 1),
        (5, base_hijri_year + 1),
        (6, base_hijri_year + 1),
        (7, base_hijri_year + 1),
        (8, base_hijri_year + 1),
        (9, base_hijri_year + 1),
    ]
    
    for acad_idx, (m_num, h_yr) in enumerate(m_order, 1):
        m_name = HIJRI_MONTHS[m_num - 1]
        # Clean short name
        short = m_name.split()[0]
        label = f"{m_name} {h_yr} H"
        sequence.append({
            "academic_index": acad_idx,
            "month_num": m_num,
            "hijri_year": h_yr,
            "name": m_name,
            "short_name": short,
            "label": label,
        })
        
    return sequence
