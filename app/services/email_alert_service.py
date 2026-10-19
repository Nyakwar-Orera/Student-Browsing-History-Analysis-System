# app/services/email_alert_service.py
"""
email_alert_service.py — Teacher Email Notification Service for MamalBrowse Sentinel.

Sends violation alerts and daily digests to teachers via SMTP.
Supports:
  - Immediate email for severe violations (adult, torrent, movie_download, unauthorized_browser)
  - Hourly digest email summarising all violations
  - Blocked student summary

Configure in .env:
  EMAIL_SMTP_HOST=smtp.gmail.com
  EMAIL_SMTP_PORT=587
  EMAIL_USERNAME=sentinel@ajsn.edu.qa
  EMAIL_PASSWORD=your-app-password
  EMAIL_FROM=MamalBrowse Sentinel <sentinel@ajsn.edu.qa>
  EMAIL_RECIPIENTS=teacher@ajsn.edu.qa,principal@ajsn.edu.qa
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import threading
import time
from collections import defaultdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("email_alert_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ─── SMTP Config from .env ───────────────────────────────────────
SMTP_HOST       = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.getenv("EMAIL_SMTP_PORT", "587"))
SMTP_USER       = os.getenv("EMAIL_USERNAME", "")
SMTP_PASS       = os.getenv("EMAIL_PASSWORD", "")
EMAIL_FROM      = os.getenv("EMAIL_FROM", "MamalBrowse Sentinel <sentinel@ajsn.edu.qa>")
EMAIL_ENABLED   = os.getenv("EMAIL_ENABLED", "true").lower() == "true"
EMAIL_RECIPIENTS_ENV = os.getenv("EMAIL_RECIPIENTS", "")

# Immediate alert severity categories
IMMEDIATE_CATEGORIES = {"adult", "movie_download", "torrent", "unauthorized_browser"}

# Category display names and colors
CATEGORY_META = {
    "social_media":        ("📱 Social Media",      "#f59e0b"),
    "movie_download":      ("🎬 Movie Download",    "#ef4444"),
    "torrent":             ("⬇️ Torrent/P2P",       "#ef4444"),
    "streaming":           ("📺 Streaming",          "#8b5cf6"),
    "gaming":              ("🎮 Gaming",             "#6366f1"),
    "adult":               ("🔞 Adult Content",     "#dc2626"),
    "unauthorized_browser":("🌐 Unauthorized Browser","#f97316"),
    "session_expired":     ("⏰ Session Expired",   "#94a3b8"),
}


class EmailAlertService:
    _instance: Optional[EmailAlertService] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> EmailAlertService:
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._lock = threading.Lock()
        self._pending_violations: List[Dict[str, Any]] = []
        self._last_digest_sent: float = 0.0
        self._digest_interval = 3600  # will be updated from policy
        self._recipients: List[str] = []
        self._immediate_categories: set = IMMEDIATE_CATEGORIES.copy()
        self._digest_thread: Optional[threading.Thread] = None
        self._initialized = True
        self._start_digest_thread()

    def configure(
        self,
        recipients: List[str],
        digest_interval_seconds: int = 3600,
        immediate_categories: Optional[List[str]] = None,
    ) -> None:
        """Called by policy engine after loading config."""
        with self._lock:
            self._recipients = recipients or []
            self._digest_interval = max(60, digest_interval_seconds)
            if immediate_categories:
                self._immediate_categories = set(immediate_categories)

    def add_violation(self, violation: Dict[str, Any]) -> None:
        """Called by enforcement route when a policy violation is detected."""
        if not EMAIL_ENABLED or not self._recipients:
            return
        with self._lock:
            self._pending_violations.append(violation)

        category = violation.get("category", "")
        if category in self._immediate_categories:
            # Fire immediate alert in background thread
            threading.Thread(
                target=self._send_immediate_alert,
                args=(violation,),
                daemon=True,
            ).start()

    def send_blocked_student_alert(self, trno: str, student_name: str, reason: str, station: str) -> None:
        """Sends an immediate account-blocked notification to teachers."""
        if not EMAIL_ENABLED or not self._recipients:
            return
        threading.Thread(
            target=self._send_account_blocked_email,
            args=(trno, student_name, reason, station),
            daemon=True,
        ).start()

    # ─────────────────────────────────────────────────────────────
    # Digest Thread
    # ─────────────────────────────────────────────────────────────

    def _start_digest_thread(self) -> None:
        self._digest_thread = threading.Thread(
            target=self._digest_loop, daemon=True, name="MamalBrowse Sentinel-EmailDigest"
        )
        self._digest_thread.start()
        logger.info("[EmailAlert] Digest thread started (interval: %ds)", self._digest_interval)

    def _digest_loop(self) -> None:
        while True:
            time.sleep(60)  # check every minute
            if not EMAIL_ENABLED or not self._recipients:
                continue
            if time.time() - self._last_digest_sent >= self._digest_interval:
                with self._lock:
                    if self._pending_violations:
                        violations = list(self._pending_violations)
                        self._pending_violations.clear()
                    else:
                        violations = []
                if violations:
                    try:
                        self._send_digest_email(violations)
                        self._last_digest_sent = time.time()
                    except Exception as e:
                        logger.error("[EmailAlert] Digest send error: %s", e)

    # ─────────────────────────────────────────────────────────────
    # Email Builders
    # ─────────────────────────────────────────────────────────────

    def _send_immediate_alert(self, violation: Dict[str, Any]) -> None:
        category = violation.get("category", "unknown")
        cat_label, cat_color = CATEGORY_META.get(category, (category.replace("_", " ").title(), "#ef4444"))
        trno = violation.get("trno", "—")
        host = violation.get("host") or violation.get("url", "—")
        station = violation.get("station", "—")
        ts = violation.get("ts", datetime.now().isoformat(timespec="seconds"))

        subject = f"🚨 MamalBrowse Sentinel ALERT: {cat_label} — Student {trno}"
        body = self._html_wrapper(f"""
        <h2 style="color:{cat_color};margin:0 0 16px;">🚨 Immediate Policy Violation Alert</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <tr><td style="padding:8px 12px;background:#1e293b;color:#94a3b8;width:140px;">Student TRNO</td>
              <td style="padding:8px 12px;font-weight:700;color:#f1f5f9;">{trno}</td></tr>
          <tr><td style="padding:8px 12px;background:#0f172a;color:#94a3b8;">Category</td>
              <td style="padding:8px 12px;"><span style="background:{cat_color}22;color:{cat_color};padding:2px 10px;border-radius:999px;font-weight:700;">{cat_label}</span></td></tr>
          <tr><td style="padding:8px 12px;background:#1e293b;color:#94a3b8;">Site / URL</td>
              <td style="padding:8px 12px;color:#f87171;font-family:monospace;">{host}</td></tr>
          <tr><td style="padding:8px 12px;background:#0f172a;color:#94a3b8;">Workstation</td>
              <td style="padding:8px 12px;color:#f1f5f9;">{station}</td></tr>
          <tr><td style="padding:8px 12px;background:#1e293b;color:#94a3b8;">Time</td>
              <td style="padding:8px 12px;color:#94a3b8;">{ts}</td></tr>
        </table>
        <p style="margin:16px 0 0;padding:12px;background:#1e293b;border-radius:8px;color:#fbbf24;font-size:13px;">
          ⚠️ The student's account has been automatically suspended. Log in to MamalBrowse Sentinel to review or unblock.
        </p>
        """)
        self._send_email(subject, body)

    def _send_account_blocked_email(self, trno: str, student_name: str, reason: str, station: str) -> None:
        subject = f"🔒 MamalBrowse Sentinel: Account Suspended — Student {trno} ({student_name})"
        body = self._html_wrapper(f"""
        <h2 style="color:#ef4444;margin:0 0 16px;">🔒 Student Account Suspended</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <tr><td style="padding:8px 12px;background:#1e293b;color:#94a3b8;width:140px;">Student TRNO</td>
              <td style="padding:8px 12px;font-weight:700;color:#f1f5f9;">{trno}</td></tr>
          <tr><td style="padding:8px 12px;background:#0f172a;color:#94a3b8;">Name</td>
              <td style="padding:8px 12px;color:#f1f5f9;">{student_name or '—'}</td></tr>
          <tr><td style="padding:8px 12px;background:#1e293b;color:#94a3b8;">Reason</td>
              <td style="padding:8px 12px;color:#fca5a5;">{reason}</td></tr>
          <tr><td style="padding:8px 12px;background:#0f172a;color:#94a3b8;">Workstation</td>
              <td style="padding:8px 12px;color:#f1f5f9;">{station}</td></tr>
          <tr><td style="padding:8px 12px;background:#1e293b;color:#94a3b8;">Time</td>
              <td style="padding:8px 12px;color:#94a3b8;">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
        </table>
        <p style="margin:16px 0 0;padding:12px;background:#1e293b;border-radius:8px;color:#f1f5f9;font-size:13px;">
          To unblock this student, log in to the MamalBrowse Sentinel dashboard at <strong>http://10.10.117.76:5000</strong>.
        </p>
        """)
        self._send_email(subject, body)

    def _send_digest_email(self, violations: List[Dict[str, Any]]) -> None:
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        count = len(violations)

        # Group by category
        by_category: Dict[str, List[Dict]] = defaultdict(list)
        for v in violations:
            by_category[v.get("category", "unknown")].append(v)

        rows_html = ""
        for v in violations[-50:]:  # limit to last 50 in email
            cat = v.get("category", "")
            cat_label, cat_color = CATEGORY_META.get(cat, (cat.title(), "#94a3b8"))
            rows_html += f"""
            <tr>
              <td style="padding:7px 10px;border-bottom:1px solid #1e293b;color:#f1f5f9;font-family:monospace;">{v.get('trno','—')}</td>
              <td style="padding:7px 10px;border-bottom:1px solid #1e293b;color:#94a3b8;">{(v.get('ts',''))[:16]}</td>
              <td style="padding:7px 10px;border-bottom:1px solid #1e293b;">
                <span style="background:{cat_color}22;color:{cat_color};padding:2px 8px;border-radius:999px;font-size:11px;">{cat_label}</span>
              </td>
              <td style="padding:7px 10px;border-bottom:1px solid #1e293b;color:#f87171;font-family:monospace;font-size:12px;">{v.get('host') or v.get('url','—')}</td>
              <td style="padding:7px 10px;border-bottom:1px solid #1e293b;color:#94a3b8;">{v.get('station','—')}</td>
            </tr>"""

        cat_summary = "".join(
            f'<span style="background:#1e293b;padding:4px 12px;border-radius:999px;margin:2px;font-size:12px;color:#f1f5f9;display:inline-block;">'
            f'{CATEGORY_META.get(c, (c, "#94a3b8"))[0]} ({len(items)})</span>'
            for c, items in by_category.items()
        )

        subject = f"📊 MamalBrowse Sentinel Hourly Digest — {count} Violations — {date_str}"
        body = self._html_wrapper(f"""
        <h2 style="color:#6366f1;margin:0 0 8px;">📊 MamalBrowse Sentinel Hourly Violation Digest</h2>
        <p style="color:#94a3b8;margin:0 0 16px;font-size:13px;">Report generated: {date_str}</p>
        <div style="margin-bottom:16px;">{cat_summary}</div>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="background:#1e293b;">
              <th style="padding:8px 10px;text-align:left;color:#94a3b8;">TRNO</th>
              <th style="padding:8px 10px;text-align:left;color:#94a3b8;">Time</th>
              <th style="padding:8px 10px;text-align:left;color:#94a3b8;">Category</th>
              <th style="padding:8px 10px;text-align:left;color:#94a3b8;">Site</th>
              <th style="padding:8px 10px;text-align:left;color:#94a3b8;">Station</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        <p style="margin:16px 0 0;font-size:12px;color:#64748b;">
          Full violation log: <a href="http://10.10.117.76:5000" style="color:#6366f1;">Sentinel Dashboard</a>
        </p>
        """)
        self._send_email(subject, body)

    def _html_wrapper(self, content: str) -> str:
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#f1f5f9;margin:0;padding:20px;">
  <div style="max-width:680px;margin:0 auto;background:#0f172a;border:1px solid #1e293b;border-radius:12px;overflow:hidden;">
    <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:20px 24px;display:flex;align-items:center;gap:12px;">
      <div style="font-size:28px;">🛡️</div>
      <div>
        <div style="font-size:18px;font-weight:800;color:#fff;">MamalBrowse Sentinel</div>
        <div style="font-size:12px;color:rgba(255,255,255,0.7);">AJSN School — Lab Management System</div>
      </div>
    </div>
    <div style="padding:24px;">{content}</div>
    <div style="padding:16px 24px;border-top:1px solid #1e293b;font-size:11px;color:#475569;text-align:center;">
      MamalBrowse Sentinel IT Department • AJSN School • This is an automated notification.
    </div>
  </div>
</body></html>"""

    def _send_email(self, subject: str, html_body: str) -> None:
        if not EMAIL_ENABLED:
            logger.debug("[EmailAlert] Email disabled — skipping: %s", subject)
            return
        if not self._recipients or not SMTP_USER or not SMTP_PASS:
            logger.warning("[EmailAlert] SMTP not configured. Set EMAIL_USERNAME/EMAIL_PASSWORD in .env")
            return
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = EMAIL_FROM
            msg["To"]      = ", ".join(self._recipients)
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, self._recipients, msg.as_string())

            logger.info("[EmailAlert] Sent: %s → %s", subject[:60], self._recipients)
        except Exception as e:
            logger.error("[EmailAlert] SMTP error: %s", e)

    def get_pending_count(self) -> int:
        with self._lock:
            return len(self._pending_violations)


# ─── Module-level singleton ───────────────────────────────────────
email_alert_service = EmailAlertService()
