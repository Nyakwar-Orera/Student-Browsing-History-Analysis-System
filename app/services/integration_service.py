# app/services/integration_service.py
import os
import re
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import threading
import pandas as pd

from app.services.lab_manager_service import lab_manager
try:
    from app.student_roster import get_student_info
except ImportError:
    from student_roster import get_student_info
try:
    from app.services.calendar_utils import get_hijri_date_label
except ImportError:
    from calendar_utils import get_hijri_date_label

logger = logging.getLogger("integration_service")

# Recognised educational / research domains
EDUCATIONAL_DOMAINS = {
    "wikipedia.org", "wikimedia.org", "khanacademy.org", "coursera.org", "edx.org",
    "mit.edu", "harvard.edu", "cambridge.org", "oxfordjournals.org", "jstor.org",
    "sciencedirect.com", "springer.com", "nature.com", "researchgate.net", "arxiv.org",
    "scholar.google.com", "duolingo.com", "quizlet.com", "britannica.com",
    "jameasaifiyah.org", "its52.com", "darseburhani.org", "aljamea.edu",
    "github.com", "stackoverflow.com", "python.org", "w3schools.com", "geeksforgeeks.org",
    "archive.org", "gutenberg.org"
}

class IntegrationService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.executor = ThreadPoolExecutor(max_workers=8)
        self._profile_cache = {}
        self._cache_lock = threading.Lock()

    def get_student_combined_profile(self, student_id, from_date=None, to_date=None, browsing_df=None):
        """
        Gathers Student 360 profile combining Koha Library, Live Browsing Logs,
        Lab Time Management computer sessions, and Roster metadata.
        Uses in-memory cache to guarantee fast response times without machine hangs.
        """
        import time as _t
        student_id_str = str(student_id).strip()
        if not from_date:
            from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")

        cache_key = (student_id_str, from_date, to_date)
        with self._cache_lock:
            if cache_key in self._profile_cache:
                prof, ts = self._profile_cache[cache_key]
                if _t.time() - ts < 300:  # 5 min TTL
                    return prof

        # Fetch in parallel for high performance
        future_library = self.executor.submit(
            self._fetch_library_data, student_id_str, from_date, to_date
        )
        if browsing_df is not None:
            future_browsing = self.executor.submit(
                self._compute_browsing_from_df, browsing_df, from_date, to_date
            )
        else:
            future_browsing = self.executor.submit(
                self._fetch_browsing_data, student_id_str, from_date, to_date
            )
        future_lab = self.executor.submit(
            lab_manager.get_student_summary, student_id_str
        )
        future_borrower = self.executor.submit(
            self._fetch_borrower_info, student_id_str
        )

        library_data = future_library.result()
        browsing_data = future_browsing.result()
        lab_data = future_lab.result()
        borrower_info = future_borrower.result()

        result = self._merge_profile(student_id_str, borrower_info, library_data, browsing_data, lab_data)
        with self._cache_lock:
            self._profile_cache[cache_key] = (result, _t.time())
        return result

    def _fetch_borrower_info(self, student_id):
        """Fetch student details prioritizing hot in-memory roster (instant) before Koha SQL."""
        # 1. Hot in-memory student roster (instant <1ms, covers all 2900+ students)
        roster_info = get_student_info(student_id)
        if roster_info:
            dar = roster_info.get("darajah") or roster_info.get("class") or "General"
            if str(dar).upper() in ("UNKNOWN", "UNKNOWN_OU", "NONE", ""):
                dar = roster_info.get("branch") or "General"
            return {
                "borrowernumber": roster_info.get("borrowernumber"),
                "cardnumber": roster_info.get("cardnumber") or student_id,
                "fullname": roster_info.get("name") or f"Student #{student_id}",
                "darajah": dar,
                "marhala": roster_info.get("branch") or "General",
                "trno": roster_info.get("trno", student_id),
                "dateexpiry": None,
                "debarred": 0,
                "gonenoaddress": 0
            }


        return {
            "borrowernumber": None,
            "cardnumber": student_id,
            "fullname": f"Student #{student_id}",
            "darajah": "General",
            "marhala": "General",
            "trno": str(student_id),
            "dateexpiry": None,
            "debarred": 0,
            "gonenoaddress": 0
        }

    def _fetch_library_data(self, student_id, from_date, to_date):
        """Koha book borrowing query bypassed (books tracking excluded)."""
        return {
            "total_issues": 0,
            "unique_titles": 0,
            "titles": "None",
            "last_issue": "Never",
            "last_issue_hijri": "Never",
            "recent_issues": 0,
            "weekly_issues": 0
        }

    def _compute_browsing_from_df(self, df, from_date, to_date):
        """Calculate metrics directly from pre-loaded DataFrame without disk re-read."""
        try:
            if df is None or df.empty or "domain" not in df.columns:
                return {
                    "total_visits": 0, "unique_domains": 0, "educational_ratio": 0.0,
                    "productive_hours": 0.0, "top_domain": "None", "active_days": 0
                }
            f_df = df
            if from_date and "visit_time" in f_df.columns:
                try:
                    f_df = f_df[f_df["visit_time"] >= pd.to_datetime(from_date)]
                except Exception:
                    pass
            if to_date and "visit_time" in f_df.columns:
                try:
                    f_df = f_df[f_df["visit_time"] < pd.to_datetime(to_date) + timedelta(days=1)]
                except Exception:
                    pass

            if f_df.empty:
                return {
                    "total_visits": 0, "unique_domains": 0, "educational_ratio": 0.0,
                    "productive_hours": 0.0, "top_domain": "None", "active_days": 0
                }

            total_visits = len(f_df)
            dom_counts = f_df["domain"].value_counts()
            unique_domains = len(dom_counts)
            top_domain = str(dom_counts.index[0]) if not dom_counts.empty else "None"

            # Check educational domains across unique domain keys in milliseconds
            edu_visits = sum(cnt for dom, cnt in dom_counts.items() if any(edu in str(dom).lower() for edu in EDUCATIONAL_DOMAINS))
            educational_ratio = round(float(edu_visits) / float(total_visits), 2) if total_visits > 0 else 0.0
            active_days = f_df["visit_time"].dt.date.nunique() if "visit_time" in f_df.columns else 0
            productive_hours = round(min(total_visits * 0.05, 50.0), 1)

            return {
                "total_visits": total_visits,
                "unique_domains": unique_domains,
                "educational_ratio": educational_ratio,
                "productive_hours": productive_hours,
                "top_domain": top_domain,
                "active_days": active_days
            }
        except Exception as e:
            logger.warning(f"[Integration] Direct df metric extraction error: {e}")
            return {
                "total_visits": 0, "unique_domains": 0, "educational_ratio": 0.0,
                "productive_hours": 0.0, "top_domain": "None", "active_days": 0
            }

    def _fetch_browsing_data(self, student_id, from_date, to_date):
        """Load browsing metrics directly from the student's CSV file."""
        from app.main import load_data
        try:
            df = load_data(trno=student_id)
            return self._compute_browsing_from_df(df, from_date, to_date)
        except Exception as e:
            logger.warning(f"[Integration] Browsing fetch failed for {student_id}: {e}")
            return {
                "total_visits": 0,
                "unique_domains": 0,
                "educational_ratio": 0.0,
                "productive_hours": 0.0,
                "top_domain": "None",
                "active_days": 0
            }

    def _merge_profile(self, student_id, borrower, library_data, browsing_data, lab_data):
        """Combine all data sources into a unified profile and calculate engagement & risk."""
        status = self._determine_status(borrower)

        profile = {
            "student_id": student_id,
            "fullname": borrower.get("fullname", "Unknown"),
            "darajah": borrower.get("darajah", "Unknown"),
            "marhala": borrower.get("marhala", "Unknown"),
            "trno": borrower.get("trno", student_id),
            "status": status,
            "library": library_data or {},
            "browsing": browsing_data or {},
            "lab": lab_data or {},
            "engagement_score": 0,
            "risk_level": "low",
            "recommendations": []
        }

        # Calculate AI Academic Focus Score
        from app.main import load_data
        from app.services.analytics_service import analytics_service
        from app.services.account_lock_service import account_lock_service
        try:
            student_df = load_data(trno=student_id)
            lib_count = library_data.get("total_issues", 0) if library_data else 0
            focus_info = analytics_service.calculate_student_focus_score(student_df, student_id, lib_count)
        except Exception as e:
            focus_info = {
                "score": 85, "label": "Good Academic Focus", "badge": "green", "icon": "🟢",
                "edu_pct": 0, "inst_pct": 0, "distraction_pct": 0, "high_risk_pct": 0,
                "reason": "Default roster rating."
            }

        profile["focus_score"] = focus_info

        # Integrate Account Lock & Policy Enforcement Status
        acc_info = account_lock_service.get_account(student_id)
        profile["account_status"] = acc_info.get("status", "active")
        profile["warning_count"] = acc_info.get("warning_count", 0)
        profile["is_blocked"] = (acc_info.get("status") == "blocked")
        profile["blocked_reason"] = acc_info.get("blocked_reason", "")

        # Calculate score and risk
        score = self._calculate_engagement_score(profile)
        profile["engagement_score"] = score
        profile["risk_level"] = self._determine_risk_level(score, is_blocked=profile["is_blocked"], warnings=profile["warning_count"])
        profile["recommendations"] = self._generate_recommendations(profile)

        return profile

    def _determine_status(self, borrower):
        if not borrower:
            return "Active"
        if borrower.get("debarred") == 1:
            return "Debarred"
        if borrower.get("gonenoaddress") == 1:
            return "Address Lost"
        date_exp = borrower.get("dateexpiry")
        if date_exp and isinstance(date_exp, datetime) and date_exp.date() < datetime.now().date():
            return "Expired"
        return "Active"

    def _calculate_engagement_score(self, profile):
        """
        Engagement Score (0 to 100):
        - Browsing Quality & Academic Domains: 50%
        - Lab Computer Time & Consistency: 40%
        - Enrollment Status: 10%
        - Deductions for policy violations and account lock
        """
        brw = profile.get("browsing", {})
        lab = profile.get("lab", {})

        # 1. Browsing Score (50 pts max)
        brw_visits = int(brw.get("total_visits") or 0)
        brw_edu_ratio = float(brw.get("educational_ratio") or 0.0)
        brw_days = int(brw.get("active_days") or 0)
        brw_score = (
            min(brw_visits / 80.0, 1.0) * 15.0 +
            min(brw_edu_ratio / 0.5, 1.0) * 20.0 +
            min(brw_days / 10.0, 1.0) * 15.0
        )

        # 2. Lab Usage Score (40 pts max)
        lab_hours = float(lab.get("total_hours") or 0.0)
        lab_days = int(lab.get("active_days") or 0)
        lab_score = (
            min(lab_hours / 15.0, 1.0) * 24.0 +
            min(lab_days / 8.0, 1.0) * 16.0
        )

        # 3. Status Consistency (10 pts max)
        status_score = 10.0 if profile.get("status") == "Active" else 4.0

        total_score = int(round(brw_score + lab_score + status_score))

        # Deduct for policy violations & account blocks
        violations = profile.get("warning_count", 0)
        if violations > 0:
            total_score = max(0, total_score - violations * 5)
        if profile.get("is_blocked"):
            total_score = max(0, total_score - 25)

        return max(0, min(100, total_score))

    def _determine_risk_level(self, score, is_blocked=False, warnings=0):
        if is_blocked or warnings >= 3:
            return "high"
        if score >= 70:
            return "low"
        elif score >= 40:
            return "medium"
        else:
            return "high"

    def _generate_recommendations(self, profile):
        recs = []
        brw = profile.get("browsing", {})
        lab = profile.get("lab", {})
        score = profile.get("engagement_score", 0)

        if profile.get("is_blocked"):
            recs.append({
                "title": "Account Locked — Policy Violation",
                "description": f"Student account has been suspended ({profile.get('blocked_reason')}). Consult teacher or lab admin to unblock.",
                "priority": "critical",
                "badge": "Enforcement"
            })

        edu_ratio = float(brw.get("educational_ratio") or 0.0)
        if edu_ratio < 0.25 and int(brw.get("total_visits") or 0) > 30:
            recs.append({
                "title": "Elevate Academic Browsing",
                "description": f"Only {int(edu_ratio * 100)}% of web traffic is on educational/academic resources. Direct to academic databases.",
                "priority": "medium",
                "badge": "Browsing"
            })

        lab_hours = float(lab.get("total_hours") or 0.0)
        if lab_hours < 2.0:
            recs.append({
                "title": "Computer Lab Attendance",
                "description": "Low recorded computer lab participation. Verify lab schedule or attendance logs.",
                "priority": "medium",
                "badge": "Lab Time"
            })

        if score < 40:
            recs.append({
                "title": "Academic Advisor Review",
                "description": "Composite engagement score is in the high risk bracket. Recommend a check-in with the Darajah Masool.",
                "priority": "high",
                "badge": "Advisor"
            })

        return recs

# Global integration service
integration_service = IntegrationService()
