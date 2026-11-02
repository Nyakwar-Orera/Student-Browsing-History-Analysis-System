# app/routes/enforcement.py
"""
enforcement.py — Real-time policy enforcement, live incident feed, and PDF reports.

All violation writes go through violation_service.record_violation() — the
single source of truth. No other code path may write violation JSONL.

INSTITUTIONAL POLICY (2026-10-03):
  - Violation retention window: 14 days.
  - Students blocked for violations are auto-unblocked after 14 days.
  - Audit logs older than 14 days are archived for compliance (not deleted).
  - Weekly violation log automatically uses the 14-day retention window.
  - All manual suspensions remain indefinite until explicitly unblocked OR
    until they exceed the 14-day auto-expiry policy.

Q1 (2026-10-02): No automatic account blocking. All suspensions are manual
via the admin dashboard. The policy engine only warns and records.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from flask import (
    Blueprint, Response, abort, jsonify, render_template,
    request, send_file, session,
)

from app.services.account_lock_service import account_lock_service
from app.services.policy_engine import policy_engine
from app.services.session_timer_service import session_timer_service, DARAJAH_TIME_LIMITS
from app.services.violation_service import violation_service
from app.services.incident_service import incident_service
from app.services.lab_manager_service import lab_manager
from app.services.time_accounting_service import time_accounting_service
from app.services.event_log_service import event_log_service

# ── Retention policy (14-day institutional rule) ─────────────────────────────
try:
    from app.services.retention_policy_service import (
        retention_policy_service,
        VIOLATION_RETENTION_DAYS,
        AUDIT_RETENTION_DAYS,
    )
    _RETENTION_AVAILABLE = True
except ImportError:
    retention_policy_service = None
    VIOLATION_RETENTION_DAYS = 14
    AUDIT_RETENTION_DAYS = 14
    _RETENTION_AVAILABLE = False

logger = logging.getLogger("enforcement")

# Configure logs/sentinel.log rotation (10MB x 10 files)
_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_LOGS_DIR = _BASE_DIR / "logs"
_LOGS_DIR.mkdir(parents=True, exist_ok=True)
_sentinel_log_handler = RotatingFileHandler(
    filename=str(_LOGS_DIR / "sentinel.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=10,
    encoding="utf-8",
)
_sentinel_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
    logger.addHandler(_sentinel_log_handler)
logger.setLevel(logging.INFO)

enforcement_bp = Blueprint("enforcement", __name__, url_prefix="/enforce")


BASE_DIR = Path(__file__).resolve().parent.parent.parent
VIOLATIONS_DIR = BASE_DIR / "students" / "violations"
OVERRIDES_FILE = BASE_DIR / "data" / "override_requests.jsonl"

VIOLATIONS_DIR.mkdir(parents=True, exist_ok=True)
OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)


def _require_admin():
    """Ensure user is logged in as administrator or teacher."""
    if not session.get("logged_in"):
        abort(401)


def verify_no_auto_block():
    """Report active policy enforcement state from access_policy.yaml."""
    try:
        p = policy_engine._policy
        if p.account_lock.enabled and p.account_lock.auto_block_categories:
            logger.info(
                "[Enforcement] Auto-block: ENABLED for %s (deliberate time spent). Ad redirects will WARN only.",
                p.account_lock.auto_block_categories,
            )
        else:
            logger.info("[Enforcement] Auto-block: DISABLED (manual-only mode)")
    except Exception as e:
        logger.debug(f"[Enforcement] policy check skipped: {e}")


def _get_retention_window() -> Dict[str, Any]:
    """
    Return the current retention window info for the violation log.
    Uses the retention policy service if available; otherwise falls back to
    a hardcoded 14-day window.
    """
    if _RETENTION_AVAILABLE and retention_policy_service:
        try:
            return retention_policy_service.get_retention_window()
        except Exception as e:
            logger.warning(f"[Enforcement] Retention policy unavailable: {e}")

    # Fallback: hardcoded 14-day window
    now = datetime.now()
    start = now - timedelta(days=VIOLATION_RETENTION_DAYS)
    return {
        "retention_days": VIOLATION_RETENTION_DAYS,
        "window_start": start.strftime("%Y-%m-%d"),
        "window_end": now.strftime("%Y-%m-%d"),
        "policy_text": (
            f"Violations are retained for {VIOLATION_RETENTION_DAYS} days. "
            f"Blocked accounts auto-unblock after {VIOLATION_RETENTION_DAYS} days."
        ),
    }


def _days_remaining_in_policy(blocked_at_str: str) -> int:
    """
    Calculate how many days remain in the 14-day suspension window.
    Returns 0 if the block has already expired or if the timestamp is invalid.
    """
    if not blocked_at_str:
        return 0
    try:
        blocked_at = datetime.fromisoformat(
            str(blocked_at_str).replace("Z", "").split("+")[0].split(".")[0]
        )
    except Exception:
        return 0
    days_elapsed = (datetime.now() - blocked_at).days
    remaining = VIOLATION_RETENTION_DAYS - days_elapsed
    return max(0, remaining)


# ─────────────────────────────────────────────────────────────────
# 1. Real-Time Check Endpoint (Client Agent)
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.post("/check")
def check_url():
    """
    Called by lab PC client agent on navigation.
    Payload: { "url": "...", "trno": "12345", "darajah": "3", "station": "PC-04" }
    Returns decision + incident metadata.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url parameter is required"}), 400

    trno = str(data.get("trno") or "").strip() or None
    darajah = str(data.get("darajah") or "").strip() or None
    station = str(data.get("station") or "").strip() or None
    browser = str(data.get("browser") or data.get("process") or "").strip().lower()

    # Check for prohibited application / process execution
    is_app_proc = url.startswith("app://") or (browser and browser in _PROHIBITED_PROCESSES)
    if is_app_proc:
        proc_name = browser or url.replace("app://", "").strip().lower()
        is_browser = ("browser" in proc_name) or proc_name in (
            "firefox.exe", "msedge.exe", "opera.exe", "opera_gx.exe", "brave.exe",
            "edge.exe", "iexplore.exe", "safari.exe", "arc.exe", "vivaldi.exe", "chromium.exe"
        )
        cat = "unauthorized_browser" if is_browser else "prohibited_process"
        req_raw_ts = data.get("timestamp") or data.get("ts") or ""
        decision = {
            "allowed": False,
            "action": "block",
            "category": cat,
            "message": f"Prohibited application '{proc_name}' terminated on workstation.",
            "station": station or "",
            "ts": req_raw_ts,
            "severity": "HIGH",
        }
        try:
            from app.services.alert_dispatch_service import alert_dispatch_service
            alert_dispatch_service.dispatch_executable_alert(
                trno=trno or "N/A",
                darajah=darajah or "General",
                station=station or "Lab PC",
                process_name=proc_name,
            )
        except Exception as alert_e:
            log.debug("Process alert dispatch error: %s", alert_e)
    else:
        referrer = str(data.get("referrer") or data.get("ref") or "").strip()
        dwell_sec = int(data.get("dwell_sec") or data.get("dwell") or 0)
        hit_count = int(data.get("hit_count") or 1)
        title = str(data.get("title") or "").strip()
        decision = policy_engine.evaluate(
            url=url,
            trno=trno,
            darajah=darajah,
            station=station,
            referrer=referrer,
            dwell_sec=dwell_sec,
            hit_count=hit_count,
            title=title,
        )

    # Attach resolved station & timestamp to response
    resolved_station = decision.get("station") or ""
    resolved_ts = decision.get("ts") or decision.get("timestamp") or ""
    decision["station"] = resolved_station
    decision["timestamp"] = resolved_ts
    decision["resolved_station"] = resolved_station
    decision["resolved_timestamp"] = resolved_ts

    # Every block/warn flows through the canonical writer
    if decision.get("action") in ("block", "warn"):
        req_dt = None
        raw_ts = data.get("timestamp") or data.get("ts")
        if raw_ts:
            try:
                req_dt = datetime.fromisoformat(str(raw_ts).replace("Z", "").split("+")[0])
            except Exception:
                req_dt = None

        record = violation_service.record_violation(
            url=url,
            trno=trno or "",
            category=decision.get("category") or "restricted",
            action=decision.get("action"),
            reason=decision.get("message") or "",
            browser=browser,
            requested_station=station or "",
            requested_timestamp=req_dt,
        )
        # Queue for live dashboard
        incident_service.queue_incident(record)

        # Attach canonical fields to the response
        decision["incident_id"] = record["incident_id"]
        decision["severity"] = record["severity"]
        decision["station"] = record["station"]
        decision["timestamp"] = record["timestamp"]
        decision["hit_count"] = record["hit_count"]

    return jsonify(decision)


# ─────────────────────────────────────────────────────────────────
# 2. Browser Check (Unauthorized Browser Detection)
# ─────────────────────────────────────────────────────────────────
_ALLOWED_BROWSERS = {"chrome.exe"}
_PROHIBITED_PROCESSES = {
    "firefox.exe",
    "msedge.exe",
    "fastinternetbrowser.exe",
    "opera.exe",
    "opera_gx.exe",
    "brave.exe",
    "vivaldi.exe",
    "arc.exe",
    "chromium.exe",
    "iexplore.exe",
    "msedgewebview2.exe",
    "librewolf.exe",
    "waterfox.exe",
    "palemoon.exe",
    "duckduckgo.exe",
    "avastbrowser.exe",
    "avgbrowser.exe",
    "sidekick.exe",
    "whale.exe",
    "slimjet.exe",
    "maxthon.exe",
    "dragon.exe",
    "iron.exe",
    "falkon.exe",
    "seamonkey.exe",
    "safari.exe",
    "qtweb.exe",
    "midori.exe",
    "konqueror.exe",
    "robloxplayerbeta.exe",
    "ace.exe",
    "wave browser.exe",
    "comet.exe",
    "mozilla.exe",
    "robloxplayerinstaller.exe",
    "vlc.exe",
    "edge.exe",
    "wmplayer.exe",
    "tor.exe",
    "torbrowser.exe",
    "epic.exe",
    "psiphon3.exe",
    "ucbrowser.exe",
    "picard-2.13.3-win-x64-setup.exe",
    "clitebrowser.exe",
    "cbrowser.exe",
    "browser.exe",
    "webbrowser.exe",
    "wavebrowser.exe",
    "o-browser.exe",
    "x browser.exe",
    "webexplorer.exe",
    "winstore.app.exe",
    "nordvpn.exe",
    "expressvpn.exe",
    "protonvpn.exe",
    "ultrasurf.exe",
    "utorrent.exe",
    "bittorrent.exe",
    "qbittorrent.exe",
}


@enforcement_bp.post("/browser-check")
def browser_check():
    """
    Called by client agent when a new process is detected.
    Payload: { "process": "firefox.exe", "trno": "12345", "station": "PC-01" }
    """
    data = request.get_json(silent=True) or {}
    process = (data.get("process") or "").strip().lower()
    trno = str(data.get("trno") or "").strip() or None
    station = str(data.get("station") or "").strip() or None

    allowed = (not process or process in _ALLOWED_BROWSERS) and (process not in _PROHIBITED_PROCESSES)
    if allowed:
        return jsonify({"allowed": True, "action": "allow"})

    is_browser = ("browser" in process) or process in (
        "firefox.exe", "msedge.exe", "opera.exe", "opera_gx.exe", "brave.exe",
        "edge.exe", "iexplore.exe", "safari.exe", "arc.exe", "vivaldi.exe", "chromium.exe"
    )
    cat = "unauthorized_browser" if is_browser else "prohibited_process"
    req_dt = None
    raw_ts = data.get("timestamp") or data.get("ts")
    if raw_ts:
        try:
            req_dt = datetime.fromisoformat(str(raw_ts).replace("Z", "").split("+")[0])
        except Exception:
            req_dt = None

    record = violation_service.record_violation(
        url=f"app://{process}",
        trno=trno or "",
        category=cat,
        action="block",
        reason=f"Prohibited application: {process}. Only Chrome is permitted.",
        browser=process,
        requested_station=station or "",
        requested_timestamp=req_dt,
    )
    incident_service.queue_incident(record)

    logger.warning(
        "[BrowserCheck] Prohibited process trno=%s station=%s process=%s severity=%s",
        trno, record["station"], process, record["severity"],
    )

    return jsonify({
        "allowed": False,
        "action": "block",
        "category": "unauthorized_browser",
        "message": record["reason"],
        "process": process,
        "station": record["station"],
        "timestamp": record["timestamp"],
        "resolved_station": record["station"],
        "resolved_timestamp": record["timestamp"],
        "incident_id": record["incident_id"],
        "severity": record["severity"],
    })


# ─────────────────────────────────────────────────────────────────
# 3. Live Alerts Feed (Dashboard Polling)
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/live-alerts.json")
def live_alerts():
    """Live incident feed for dashboard pop-ups. Polls every 5s."""
    _require_admin()
    return jsonify(incident_service.get_live(limit=100))


@enforcement_bp.post("/admin/acknowledge-alert")
def acknowledge_alert():
    """Mark a live incident as reviewed (removes from dashboard queue)."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    incident_id = (data.get("incident_id") or "").strip()
    if not incident_id:
        return jsonify({"ok": False, "error": "incident_id required"}), 400
    ok = incident_service.acknowledge(incident_id)
    return jsonify({"ok": ok, "incident_id": incident_id})


@enforcement_bp.post("/admin/clear-acknowledged")
def clear_acknowledged():
    """Reset the acknowledge suppression list."""
    _require_admin()
    incident_service.clear_acknowledged()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# 4. PDF Reports (Daily / Weekly)
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/report/daily.pdf")
def download_daily_pdf():
    """On-demand daily PDF report (today's incidents)."""
    _require_admin()
    try:
        pdf_bytes = incident_service.generate_daily_pdf()
    except RuntimeError as e:
        logger.error(f"[Reports] Daily PDF failed: {e}")
        return jsonify({"error": str(e)}), 500
    filename = f"mamalbrowse_sentinel_daily_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@enforcement_bp.get("/admin/report/weekly.pdf")
def download_weekly_pdf():
    """On-demand weekly PDF report (last 7 days)."""
    _require_admin()
    try:
        pdf_bytes = incident_service.generate_weekly_pdf()
    except RuntimeError as e:
        logger.error(f"[Reports] Weekly PDF failed: {e}")
        return jsonify({"error": str(e)}), 500
    filename = f"mamalbrowse_sentinel_weekly_{datetime.now().strftime('%Y_W%V')}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@enforcement_bp.get("/admin/report/archive/list.json")
def archive_list():
    """List archived PDFs available for download."""
    _require_admin()
    archive_dir = BASE_DIR / "reports" / "archive"
    if not archive_dir.exists():
        return jsonify({"files": []})
    files = []
    for f in sorted(archive_dir.glob("*.pdf"), reverse=True)[:50]:
        try:
            stat = f.stat()
            files.append({
                "name": f.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
        except Exception:
            continue
    return jsonify({"files": files})


@enforcement_bp.get("/admin/report/archive/<path:filename>")
def download_archived_report(filename: str):
    """Download a specific archived PDF."""
    _require_admin()
    archive_dir = BASE_DIR / "reports" / "archive"
    target = (archive_dir / filename).resolve()
    # Path traversal guard
    if not str(target).startswith(str(archive_dir.resolve())):
        abort(403)
    if not target.exists() or target.suffix.lower() != ".pdf":
        abort(404)
    return send_file(target, mimetype="application/pdf", as_attachment=True, download_name=target.name)


# ─────────────────────────────────────────────────────────────────
# 5. Violations Summary (for dashboard charts)
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/violations-summary.json")
def violations_summary():
    """
    Aggregated incident counts for today from the authoritative audit log.
    Used by dashboard charts and the top-students leaderboard.
    """
    _require_admin()
    day_str = datetime.now().strftime("%Y-%m-%d")
    f = VIOLATIONS_DIR / f"audit_{day_str}.jsonl"

    category_counts: Dict[str, int] = {}
    student_counts: Dict[str, int] = {}
    student_meta: Dict[str, Dict[str, str]] = {}
    browser_flags: List[dict] = []
    violations_list: List[dict] = []

    target_file = f
    if not target_file.exists():
        avail = sorted(VIOLATIONS_DIR.glob("audit_*.jsonl"), reverse=True)
        if avail:
            target_file = avail[0]
            day_str = target_file.stem.replace("audit_", "")

    if target_file.exists():
        for line in target_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            cat = rec.get("category", "unknown")
            trno = str(rec.get("trno", "")).strip()

            category_counts[cat] = category_counts.get(cat, 0) + 1
            if trno:
                student_counts[trno] = student_counts.get(trno, 0) + 1
                if trno not in student_meta:
                    student_meta[trno] = {
                        "name": rec.get("student_name", "") or rec.get("name", ""),
                        "darajah": rec.get("darajah", ""),
                    }

            violations_list.append({
                "incident_id": rec.get("incident_id", ""),
                "severity": rec.get("severity", "LOW"),
                "trno": trno,
                "student_name": rec.get("student_name") or student_meta.get(trno, {}).get("name") or f"Student #{trno}",
                "darajah": rec.get("darajah", ""),
                "group": rec.get("group", ""),
                "station": rec.get("station") or "—",
                "ts": rec.get("timestamp") or rec.get("ts") or "",
                "date": day_str,
                "category": cat,
                "domain": rec.get("domain", ""),
                "url": rec.get("url", ""),
                "action": rec.get("action", "warn"),
                "reason": rec.get("reason", ""),
                "hit_count": rec.get("hit_count", 1),
                "browser": rec.get("browser", "chrome.exe"),
            })

            if cat == "unauthorized_browser":
                browser_flags.append(rec)

    top_students = sorted(
        [
            {
                "trno": t,
                "count": c,
                "name": student_meta.get(t, {}).get("name") or f"Student #{t}",
                "darajah": student_meta.get(t, {}).get("darajah", ""),
            }
            for t, c in student_counts.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    # Sort: HIGH first, then by timestamp descending
    sev_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    violations_list.sort(
        key=lambda v: (sev_rank.get(v.get("severity", "LOW"), 3), ""),
    )
    high_recs = [v for v in violations_list if v.get("severity") == "HIGH"]
    med_recs = [v for v in violations_list if v.get("severity") == "MEDIUM"]
    low_recs = [v for v in violations_list if v.get("severity") == "LOW"]
    for lst in (high_recs, med_recs, low_recs):
        lst.sort(key=lambda v: str(v.get("ts", "")), reverse=True)
    sorted_violations = high_recs + med_recs + low_recs

    return jsonify({
        "date": day_str,
        "category_counts": category_counts,
        "top_students": top_students,
        "violations": sorted_violations,
        "browser_flags": browser_flags[-20:],
        "total_violations": sum(category_counts.values()),
        "total_unauthorized_browsers": len(browser_flags),
        "ts": datetime.now().isoformat(timespec="seconds"),
    })


# ─────────────────────────────────────────────────────────────────
# 6. Student Warning / Account Blocked Screen
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/blocked")
def blocked_page():
    url = request.args.get("url", "")
    reason = request.args.get("reason", "")
    category = request.args.get("category", "")
    trno = request.args.get("trno", "").strip()
    station = request.args.get("station", "")
    status = request.args.get("status", "block")

    student_name = ""
    account_status = "active"
    warning_count = 0

    if trno:
        acc = account_lock_service.get_account(trno)
        student_name = acc.get("name", "")
        account_status = acc.get("status", "active")
        warning_count = acc.get("warning_count", 0)
        if not reason and acc.get("blocked_reason"):
            reason = acc.get("blocked_reason")

    is_account_suspended = (account_status == "blocked") or (status in ("block", "blocked"))

    return render_template(
        "blocked.html",
        url=url,
        reason=reason,
        category=category,
        trno=trno,
        student_name=student_name,
        station=station,
        status=status,
        account_status=account_status,
        is_account_suspended=is_account_suspended,
        warning_count=warning_count,
    )


# ─────────────────────────────────────────────────────────────────
# 7. Student Override / Appeal
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.post("/request-override")
def request_override():
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    url = (data.get("url") or "").strip()
    reason = (data.get("reason") or "").strip()

    if not reason:
        return jsonify({"ok": False, "error": "Please provide an explanation for the request."}), 400

    record = {
        "id": f"{int(datetime.now().timestamp())}_{trno}",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "trno": trno,
        "url": url,
        "reason": reason,
        "status": "pending",
    }

    try:
        with OVERRIDES_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return jsonify({"ok": True, "message": "Override request submitted successfully to your teacher."})
    except Exception as e:
        logger.error(f"[Enforcement] Error saving override request: {e}")
        return jsonify({"ok": False, "error": "Unable to save request."}), 500


# ─────────────────────────────────────────────────────────────────
# 8. Admin: Blocked / Warned Accounts (Manual Only)
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/blocked-students")
def get_blocked_students():
    _require_admin()
    blocked = account_lock_service.get_blocked_students()
    warned = account_lock_service.get_warned_students()

    # Enrich each blocked account with days remaining in the 14-day policy window
    for b in blocked:
        blocked_at = b.get("blocked_at") or b.get("last_incident") or ""
        b["days_remaining"] = _days_remaining_in_policy(blocked_at)
        b["policy_window_days"] = VIOLATION_RETENTION_DAYS
        b["blocked_at"] = blocked_at

    return jsonify({
        "blocked": blocked,
        "warned": warned,
        "total_blocked": len(blocked),
        "total_warned": len(warned),
        "retention_days": VIOLATION_RETENTION_DAYS,
    })


@enforcement_bp.post("/admin/unblock")
def unblock_student():
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    note = str(data.get("note") or "Teacher dashboard unblock").strip()
    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400

    success = account_lock_service.unblock_account(
        trno, admin_user="Teacher / Administrator", note=note,
    )
    # Re-enable in Active Directory domain (mamal.nbi)
    ad_result = None
    try:
        from app.services.active_directory_service import ad_service
        ad_result = ad_service.enable_account(trno)
    except Exception as e:
        logger.warning(f"[Enforcement] Failed to re-enable AD account for {trno}: {e}")

    if success:
        return jsonify({
            "ok": True,
            "message": f"Student {trno} unblocked and restored to Active.",
            "ad_sync": ad_result,
        })
    return jsonify({"ok": False, "error": f"Student account {trno} could not be unblocked."}), 404


@enforcement_bp.post("/admin/block")
def manual_block_student():
    """
    Manually suspend a student account.
    Accepts requests from both the admin dashboard and the client agent
    (for auto-suspension on critical violations like adult content / VPN use).
    """
    # Allow both admin dashboard and client agent (no session required for agent calls)
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    reason = str(data.get("reason") or "Suspended by administrator").strip()
    admin_user = str(data.get("admin_user") or "Administrator").strip()
    sync_ad = bool(data.get("sync_ad", True))
    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400

    acc = account_lock_service.block_account_manually(
        trno, reason=reason, admin_user=admin_user,
    )
    # Also disable Windows Active Directory account if requested
    ad_result = None
    if sync_ad:
        try:
            from app.services.active_directory_service import ad_service
            ad_result = ad_service.disable_account(trno, reason=reason)
        except Exception as e:
            logger.warning(f"[Enforcement] Failed to disable AD account for {trno}: {e}")

    logger.warning(
        "[Enforcement] Student TRNO %s BLOCKED by %s. Reason: %s (AD sync: %s)",
        trno, admin_user, reason, ad_result.get("success") if ad_result else "skipped",
    )
    return jsonify({
        "ok": True,
        "message": f"Student {trno} suspended under the {VIOLATION_RETENTION_DAYS}-day policy window.",
        "account": acc,
        "ad_sync": ad_result,
        "retention_days": VIOLATION_RETENTION_DAYS,
    })


@enforcement_bp.post("/admin/bulk-block")
def bulk_block_students():
    """Block multiple student accounts at once (for teacher emergency action)."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trnos = data.get("trnos", [])
    reason = str(data.get("reason") or "Bulk suspension by administrator").strip()
    if not trnos:
        return jsonify({"ok": False, "error": "trnos list is required"}), 400

    blocked = []
    failed = []
    for trno in trnos:
        sid = str(trno).strip()
        if not sid:
            continue
        try:
            account_lock_service.block_account_manually(
                sid, reason=reason, admin_user="Administrator",
            )
            try:
                from app.services.active_directory_service import ad_service
                ad_service.disable_account(sid, reason=reason)
            except Exception:
                pass
            blocked.append(sid)
        except Exception as e:
            failed.append({"trno": sid, "error": str(e)})

    return jsonify({
        "ok": True,
        "blocked_count": len(blocked),
        "blocked": blocked,
        "failed": failed,
        "message": f"{len(blocked)} student account(s) suspended and disabled in Active Directory.",
    })


@enforcement_bp.post("/admin/disable-account")
def disable_account():
    """
    Permanently disable a student account (both in Sentinel and Windows Active Directory).
    Sets status to 'disabled' — the student cannot log in to Windows lab PCs until manually re-enabled.
    Used by the client agent for critical violations (adult content, VPN) or by admin dashboard.
    """
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    reason = str(data.get("reason") or "Automatic suspension: Critical policy violation").strip()
    station = str(data.get("station") or "").strip()
    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400

    acc = account_lock_service.block_account_manually(
        trno,
        reason=f"DISABLED: {reason} [Station: {station}]",
        admin_user="System/AutoEnforcement",
        station=station,
    )
    # Also end the active session and queue immediate workstation lock
    try:
        from app.services.session_timer_service import session_timer_service
        session_timer_service.queue_command(station=station, command="lock", trno=trno)
        session_timer_service.end_session(trno, reason="account_disabled")
    except Exception:
        pass

    # Disable student in Active Directory domain (mamal.nbi)
    ad_result = None
    try:
        from app.services.active_directory_service import ad_service
        ad_result = ad_service.disable_account(trno, reason=reason)
    except Exception as e:
        logger.warning(f"[Enforcement] Failed to disable AD account for {trno}: {e}")

    logger.error(
        "[CRITICAL] Student TRNO %s account DISABLED in Sentinel & Active Directory. Reason: %s Station: %s (AD: %s)",
        trno, reason, station, ad_result,
    )
    return jsonify({
        "ok": True,
        "message": f"Student {trno} account disabled in Active Directory and Sentinel. Session terminated.",
        "account": acc,
        "ad_sync": ad_result,
    })


@enforcement_bp.post("/admin/enable-account")
def enable_account():
    """Re-enable a previously disabled/blocked student account in Sentinel and Active Directory."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    note = str(data.get("note") or "Re-enabled by administrator").strip()
    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400

    success = account_lock_service.unblock_account(
        trno, admin_user="Administrator", note=note,
    )
    ad_result = None
    try:
        from app.services.active_directory_service import ad_service
        ad_result = ad_service.enable_account(trno)
    except Exception as e:
        logger.warning(f"[Enforcement] Failed to re-enable AD account for {trno}: {e}")

    if success or (ad_result and ad_result.get("success")):
        return jsonify({
            "ok": True,
            "message": f"Student {trno} account re-enabled in Sentinel and Active Directory.",
            "ad_sync": ad_result,
        })
    return jsonify({"ok": False, "error": f"Could not enable account for {trno}."}), 404


@enforcement_bp.get("/admin/ad/status/<trno>")
def get_student_ad_status(trno: str):
    """Query live Active Directory account status for a student."""
    _require_admin()
    sid = str(trno).strip()
    try:
        from app.services.active_directory_service import ad_service
        status = ad_service.get_account_status(sid)
        return jsonify({"ok": True, "trno": sid, "ad_status": status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.post("/admin/ad/disable")
def manual_disable_ad():
    """Directly disable student in Active Directory domain."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    reason = str(data.get("reason") or "Manual AD disable by administrator").strip()
    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400
    try:
        from app.services.active_directory_service import ad_service
        result = ad_service.disable_account(trno, reason=reason)
        return jsonify({"ok": result.get("success", False), "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.post("/admin/ad/enable")
def manual_enable_ad():
    """Directly re-enable student in Active Directory domain."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400
    try:
        from app.services.active_directory_service import ad_service
        result = ad_service.enable_account(trno)
        return jsonify({"ok": result.get("success", False), "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# Active Directory & Account Governance Endpoints (HOD & Administration Hub)
# ══════════════════════════════════════════════════════════════════════════════

@enforcement_bp.get("/admin/governance/summary")
def get_governance_summary():
    """Return executive summary of Active Directory and Sentinel account statuses."""
    _require_admin()
    try:
        from app.services.active_directory_service import ad_service
        ad_summary = ad_service.get_domain_summary()
        blocked_sentinel = account_lock_service.get_blocked_students()
        warned_sentinel = account_lock_service.get_warned_students()
        return jsonify({
            "ok": True,
            "ad": ad_summary,
            "sentinel_blocked_count": len(blocked_sentinel),
            "sentinel_warned_count": len(warned_sentinel),
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:
        logger.error(f"[Governance] Summary error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.get("/admin/governance/accounts")
def get_governance_accounts():
    """
    Return merged list of student accounts with Active Directory & Sentinel states,
    reasons for disabling, actors, dates, and workstation info.
    Supports filters:
      - filter: 'all' | 'disabled' | 'active' | 'violations'
      - darajah: e.g. '3 A M'
      - search: string search across TRNO, name, and reason
    """
    _require_admin()
    filter_mode = request.args.get("filter", "disabled").lower()
    darajah_filter = request.args.get("darajah", "").strip()
    search_q = request.args.get("search", "").strip().lower()

    try:
        from app.student_roster import load_student_roster
        from app.services.active_directory_service import ad_service

        roster = load_student_roster() or {}
        accounts = account_lock_service.get_all_accounts()
        disabled_ad_list = ad_service.get_all_disabled_students()
        disabled_ad_map = {d["trno"]: d for d in disabled_ad_list}

        records = []
        for sid, prof in roster.items():
            sid_str = str(sid).strip()
            name = prof.get("name") or prof.get("StudentName") or f"Student #{sid_str}"
            darajah = prof.get("darajah") or prof.get("class") or "Unknown"
            gender = prof.get("gender") or ("Girls" if "female" in str(prof).lower() else "Boys")

            # Sentinel status
            acc = accounts.get(sid_str, {})
            sentinel_status = acc.get("status", "active")
            warning_count = acc.get("warning_count", 0)

            # AD status
            is_ad_disabled = sid_str in disabled_ad_map
            ad_info = disabled_ad_map.get(sid_str, {})

            # Reason resolution
            reason = "Good standing / Active"
            disabled_by = "—"
            disabled_at = ""
            station_raw = acc.get("last_station", "")
            station = lab_manager.normalize_station_name(station_raw) or lab_manager.get_workstation_for_trno(sid_str) or ""

            if acc.get("blocked_reason"):
                reason = acc["blocked_reason"]
                disabled_by = acc.get("unblocked_by", "Administrator / Policy Engine")
                disabled_at = acc.get("blocked_at", "")
            elif is_ad_disabled:
                reason = "Disabled in Windows Active Directory domain (mamal.nbi)"
                disabled_by = "System / Domain Administrator"
                disabled_at = ad_info.get("whenchanged", "")
            elif warning_count > 0:
                reason = f"{warning_count} active policy warning(s)"
                disabled_by = "Policy Engine"
                disabled_at = acc.get("last_incident", "")

            # Apply filters
            if filter_mode == "disabled" and not (is_ad_disabled or sentinel_status == "blocked"):
                continue
            elif filter_mode == "active" and (is_ad_disabled or sentinel_status == "blocked"):
                continue
            elif filter_mode == "violations" and warning_count == 0 and sentinel_status != "blocked":
                continue

            if darajah_filter and darajah_filter.lower() not in darajah.lower():
                continue

            if search_q:
                haystack = f"{sid_str} {name} {darajah} {reason}".lower()
                if search_q not in haystack:
                    continue

            records.append({
                "trno": sid_str,
                "name": name,
                "darajah": darajah,
                "gender": gender,
                "ad_disabled": is_ad_disabled,
                "sentinel_status": sentinel_status,
                "warning_count": warning_count,
                "reason": reason,
                "disabled_by": disabled_by,
                "disabled_at": disabled_at,
                "station": station,
                "last_incident": acc.get("last_incident", ""),
                "ad_dn": ad_info.get("dn", ""),
            })

        # Sort: disabled accounts first, then by warning count descending
        records.sort(key=lambda r: (not r["ad_disabled"], r["sentinel_status"] != "blocked", -r["warning_count"]))

        return jsonify({
            "ok": True,
            "total_count": len(records),
            "records": records[:2000],  # Return up to 2000 for crisp client rendering and full pagination
            "filter_mode": filter_mode,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:
        logger.error(f"[Governance] Accounts error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.post("/admin/governance/disable")
def governance_disable_account():
    """Disable student account in both Windows Active Directory and Sentinel with full reason audit."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    reason = str(data.get("reason") or "Administrative policy enforcement").strip()
    actor = str(data.get("actor") or "HOD / Administrator").strip()
    station = str(data.get("station") or "Management Console").strip()

    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400

    try:
        from app.services.active_directory_service import ad_service
        # 1. Update Sentinel state
        acc = account_lock_service.block_account_manually(
            trno, reason=f"Governance lockout: {reason}", admin_user=actor, station=station,
        )
        # 2. Terminate session
        try:
            from app.services.session_timer_service import session_timer_service
            session_timer_service.end_session(trno, reason="hod_disabled")
        except Exception:
            pass

        # 3. Disable in Active Directory
        ad_res = ad_service.disable_account(trno, reason=reason, actor=actor, station=station)

        return jsonify({
            "ok": True,
            "message": f"Student #{trno} has been DISABLED in Active Directory and Sentinel.",
            "account": acc,
            "ad_sync": ad_res,
        })
    except Exception as e:
        logger.error(f"[Governance] Disable failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.post("/admin/governance/enable")
def governance_enable_account():
    """Re-enable student account in both Windows Active Directory and Sentinel."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    note = str(data.get("note") or "Restored by HOD / Administrator").strip()
    actor = str(data.get("actor") or "HOD / Administrator").strip()

    if not trno:
        return jsonify({"ok": False, "error": "TRNO is required"}), 400

    try:
        from app.services.active_directory_service import ad_service
        # 1. Restore in Sentinel
        account_lock_service.unblock_account(trno, admin_user=actor, note=note)

        # 2. Re-enable in Active Directory
        ad_res = ad_service.enable_account(trno, note=note, actor=actor)

        return jsonify({
            "ok": True,
            "message": f"Student #{trno} has been RE-ENABLED in Active Directory and restored to Active.",
            "ad_sync": ad_res,
        })
    except Exception as e:
        logger.error(f"[Governance] Enable failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.post("/admin/governance/bulk-enable")
def governance_bulk_enable():
    """Bulk re-enable multiple student accounts."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trnos = data.get("trnos", [])
    note = str(data.get("note") or "Bulk restoration by administrator").strip()
    actor = str(data.get("actor") or "HOD / Administrator").strip()

    if not trnos:
        return jsonify({"ok": False, "error": "trnos list is required"}), 400

    from app.services.active_directory_service import ad_service
    enabled = []
    failed = []

    for trno in trnos:
        sid = str(trno).strip()
        if not sid:
            continue
        try:
            account_lock_service.unblock_account(sid, admin_user=actor, note=note)
            ad_service.enable_account(sid, note=note, actor=actor)
            enabled.append(sid)
        except Exception as e:
            failed.append({"trno": sid, "error": str(e)})

    return jsonify({
        "ok": True,
        "enabled_count": len(enabled),
        "enabled": enabled,
        "failed": failed,
        "message": f"Successfully re-enabled {len(enabled)} student account(s) in Active Directory.",
    })


# ─────────────────────────────────────────────────────────────────
# Bulk Pardon: Unblock all students blocked for a specific domain
# ─────────────────────────────────────────────────────────────────

@enforcement_bp.get("/admin/bulk-unblock-preview")
def bulk_unblock_preview():
    """
    Preview all students whose block reason or violation domain matches a given domain keyword.
    Query param: ?domain=youtube.com (or any keyword like 'youtube', 'tiktok', etc.)
    """
    _require_admin()
    keyword = (request.args.get("domain") or "").strip().lower()
    if not keyword:
        return jsonify({"ok": False, "error": "domain query param is required"}), 400

    from app.student_roster import load_student_roster
    roster = load_student_roster() or {}
    all_accs = account_lock_service.get_all_accounts()

    matches = []
    for sid, acc in all_accs.items():
        if acc.get("status") != "blocked":
            continue

        # Check the blocked_reason text
        reason = (acc.get("blocked_reason") or "").lower()
        # Check any violation domain / url / category
        violation_match = any(
            keyword in (str(v.get("domain") or "")).lower()
            or keyword in (str(v.get("url") or "")).lower()
            or keyword in (str(v.get("host") or "")).lower()
            or keyword in (str(v.get("category") or "")).lower()
            for v in (acc.get("violations") or [])
        )
        reason_match = keyword in reason

        if not (violation_match or reason_match):
            continue

        info = roster.get(sid, {})
        import re as _re
        name = _re.sub(r'^\d+\s*[-–—]\s*', '', str(info.get("name") or f"Student #{sid}")).strip()

        matches.append({
            "trno": sid,
            "name": name,
            "darajah": info.get("darajah") or info.get("class") or "—",
            "group": info.get("group") or "Talabaat",
            "blocked_at": acc.get("blocked_at") or "",
            "blocked_reason": acc.get("blocked_reason") or "",
            "last_station": acc.get("last_station") or "",
            "violations_count": len(acc.get("violations") or []),
            "warning_count": acc.get("warning_count", 0),
        })

    matches.sort(key=lambda x: x["blocked_at"], reverse=True)
    return jsonify({"ok": True, "keyword": keyword, "count": len(matches), "students": matches})


@enforcement_bp.post("/admin/bulk-unblock-by-domain")
def bulk_unblock_by_domain():
    """
    Pardon (unblock + re-enable in AD) all students whose violation domain matches a keyword.
    Body JSON: { "domain": "youtube.com", "note": "Pardoned by admin", "actor": "Admin Name" }
    """
    _require_admin()
    data = request.get_json(silent=True) or {}
    keyword = (data.get("domain") or "").strip().lower()
    note = str(data.get("note") or f"Bulk pardon: {keyword} violations cleared").strip()
    actor = str(data.get("actor") or "Administrator").strip()

    if not keyword:
        return jsonify({"ok": False, "error": "domain field is required"}), 400

    from app.services.active_directory_service import ad_service
    all_accs = account_lock_service.get_all_accounts()

    pardoned = []
    failed = []
    skipped_senior = []

    for sid, acc in all_accs.items():
        if acc.get("status") != "blocked":
            continue

        reason = (acc.get("blocked_reason") or "").lower()
        violation_match = any(
            keyword in (str(v.get("domain") or "")).lower()
            or keyword in (str(v.get("url") or "")).lower()
            or keyword in (str(v.get("host") or "")).lower()
            or keyword in (str(v.get("category") or "")).lower()
            for v in (acc.get("violations") or [])
        )
        reason_match = keyword in reason

        if not (violation_match or reason_match):
            continue

        try:
            success = account_lock_service.unblock_account(
                sid,
                admin_user=actor,
                note=f"{note} (domain: {keyword})",
            )
            if success:
                ad_service.enable_account(sid, note=note, actor=actor)
                pardoned.append(sid)
            else:
                # Darajah 8-11 have personal laptops — lab accounts remain locked permanently
                skipped_senior.append(sid)
        except Exception as e:
            failed.append({"trno": sid, "error": str(e)})

    logger.info(
        f"[Enforcement] Bulk pardon '{keyword}': pardoned={len(pardoned)}, "
        f"skipped_senior={len(skipped_senior)}, failed={len(failed)} — by {actor}"
    )

    return jsonify({
        "ok": True,
        "keyword": keyword,
        "pardoned_count": len(pardoned),
        "pardoned": pardoned,
        "skipped_senior": skipped_senior,
        "failed": failed,
        "message": (
            f"Cleared {len(pardoned)} student(s) blocked for '{keyword}' violations. "
            + (f"{len(skipped_senior)} Darajah 8-11 account(s) remain locked (students have personal laptops, lab PC access not permitted)." if skipped_senior else "")
        ),
    })


@enforcement_bp.get("/admin/governance/audit-log")
def get_governance_audit_log():
    """Return historical audit log of AD disable / enable actions."""
    _require_admin()
    try:
        from app.services.active_directory_service import ad_service
        events = ad_service.get_audit_log(limit=100)
        return jsonify({"ok": True, "count": len(events), "events": events})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.get("/admin/governance/export-disabled")
def export_disabled_csv():
    """Export currently disabled students CSV with reasons and dates."""
    _require_admin()
    try:
        from app.student_roster import load_student_roster
        from app.services.active_directory_service import ad_service

        roster = load_student_roster() or {}
        accounts = account_lock_service.get_all_accounts()
        disabled_ad_list = ad_service.get_all_disabled_students()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["TRNO", "Student Name", "Class / Darajah", "Active Directory Status", "Reason for Disabling", "Disabled By", "Date / Timestamp", "Workstation"])

        for item in disabled_ad_list:
            sid = str(item.get("trno")).strip()
            prof = roster.get(sid, {})
            name = prof.get("name") or prof.get("StudentName") or item.get("name") or f"Student #{sid}"
            darajah = prof.get("darajah") or prof.get("class") or "Unknown"
            acc = accounts.get(sid, {})
            reason = acc.get("blocked_reason") or "Domain Active Directory Policy Lockout"
            by = acc.get("unblocked_by") or "Domain Administrator"
            dt = acc.get("blocked_at") or item.get("whenchanged") or ""
            station = acc.get("last_station") or ""

            writer.writerow([sid, name, darajah, "DISABLED", reason, by, dt, station])

        output.seek(0)
        filename = f"disabled_students_audit_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.post("/admin/station/lock")
def station_lock():
    """
    Remote workstation lock command.
    Stores a pending lock command for the specified station that the agent polls.
    """
    _require_admin()
    data = request.get_json(silent=True) or {}
    station = str(data.get("station") or "").strip()
    trno = str(data.get("trno") or "").strip()
    if not station:
        return jsonify({"ok": False, "error": "station is required"}), 400

    # End any active session for this TRNO and queue immediate client lock command
    try:
        from app.services.session_timer_service import session_timer_service
        session_timer_service.queue_station_command(station, "lock", trno=trno)
        if trno:
            session_timer_service.end_session(trno, reason="remote_station_lock")
    except Exception as e:
        logger.error("[RemoteControl] Error queueing lock command: %s", e)

    logger.warning("[RemoteControl] Lock command issued for station=%s trno=%s", station, trno)
    return jsonify({
        "ok": True,
        "command": "lock",
        "station": station,
        "trno": trno,
        "message": f"Lock command queued for station {station}.",
    })


@enforcement_bp.post("/admin/station/logout")
def station_logout():
    """Force logout of a student session from the admin dashboard."""
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    station = str(data.get("station") or "").strip()
    if not trno:
        return jsonify({"ok": False, "error": "trno is required"}), 400

    try:
        from app.services.session_timer_service import session_timer_service
        if station:
            session_timer_service.queue_station_command(station, "logout", trno=trno)
        ok = session_timer_service.end_session(trno, reason="admin_remote_logout")
    except Exception:
        ok = False

    logger.info("[RemoteControl] Force logout: trno=%s station=%s", trno, station)
    return jsonify({
        "ok": ok,
        "message": f"Session for student {trno} at {station} terminated." if ok else f"No active session for {trno}.",
    })





# ─────────────────────────────────────────────────────────────────
# 9. Admin: Policy Management
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/policy")
def get_policy():
    _require_admin()
    policy_engine.reload(force=True)
    p = policy_engine._policy
    return jsonify({
        "default_action": p.default_action,
        "account_lock": {
            "enabled": p.account_lock.enabled,
            "auto_block_categories": p.account_lock.auto_block_categories,
            "max_warnings_before_block": p.account_lock.max_warnings_before_block,
        },
        "categories": {
            name: {
                "action": c.action,
                "message": c.message,
                "domains": c.domains,
                "allow_override": c.allow_override,
            } for name, c in p.categories.items()
        },
        "whitelist_domains": sorted(p.whitelist_domains),
        "blocklist_domains": sorted(p.blocklist_domains),
        "time_windows": [
            {"days": w.days, "start": w.start, "end": w.end, "blocked_categories": w.blocked_categories}
            for w in p.time_windows
        ],
        "per_darajah_extra_block": p.per_darajah_extra_block,
    })


@enforcement_bp.post("/admin/quick-block")
def quick_block():
    _require_admin()
    data = request.get_json(silent=True) or {}
    domain = (data.get("domain") or "").strip().lower()
    if not domain:
        return jsonify({"error": "domain is required"}), 400

    policy_path = policy_engine._path
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        raw.setdefault("blocklist_domains", [])
        if domain not in raw["blocklist_domains"]:
            raw["blocklist_domains"].append(domain)
        policy_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        policy_engine.reload(force=True)
        return jsonify({"ok": True, "blocked": domain, "message": f"'{domain}' added to blocklist."})
    except Exception as e:
        logger.error(f"[Enforcement] Error updating blocklist YAML: {e}")
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# 10. CSV Export & Recent Violations Feed
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/violations-recent.json")
def violations_recent():
    """Return the most recent N incidents from today's audit log."""
    _require_admin()
    limit = int(request.args.get("limit", 30))
    day_str = datetime.now().strftime("%Y-%m-%d")
    f = VIOLATIONS_DIR / f"audit_{day_str}.jsonl"
    if not f.exists():
        return jsonify([])
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
        rows = []
        for l in lines[-limit:]:
            if l.strip():
                try:
                    rows.append(json.loads(l))
                except json.JSONDecodeError:
                    continue
        return jsonify(list(reversed(rows)))
    except Exception as e:
        logger.warning(f"[Enforcement] Failed reading recent violations: {e}")
        return jsonify([])


@enforcement_bp.get("/admin/violations.csv")
def violations_csv():
    """Export today's audit log as CSV."""
    _require_admin()
    date_param = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    f = VIOLATIONS_DIR / f"audit_{date_param}.jsonl"
    rows: List[dict] = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not rows:
        buf = io.StringIO()
        buf.write("no incidents recorded for this date\n")
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=mamalbrowse_sentinel_audit_{date_param}.csv"},
        )

    # Preserve the complete record structure — no field dropping
    fieldnames = [
        "incident_id", "severity", "trno", "student_name", "darajah", "group",
        "station", "timestamp", "url", "domain", "category", "action",
        "reason", "browser", "hit_count", "resolved_by",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=mamalbrowse_sentinel_audit_{date_param}.csv"},
    )


# ─────────────────────────────────────────────────────────────────
# 11. Weekly Violation Log — Grouped by Student (14-Day Policy Window)
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/weekly-violation-log.json")
def weekly_violation_log():
    """
    Returns violations grouped by TRNO across the 14-day retention policy window.
    Sources from authoritative audit_*.jsonl files — never fabricated.

    Policy: Students blocked for violations are suspended for 14 days.
    The log surfaces 'days_remaining' for each blocked student so supervisors
    can see how long remains before auto-unblock.

    Query params (all optional):
      - date_from: ISO date (YYYY-MM-DD) — start of window
      - date_to:   ISO date (YYYY-MM-DD) — end of window
      - days:      integer N (1-90) — alternate way to specify a rolling window
    """
    _require_admin()
    if _RETENTION_AVAILABLE and retention_policy_service:
        try:
            retention_policy_service.auto_expire_blocked_accounts()
        except Exception as e:
            logger.debug(f"[Enforcement] Auto-expire sweep during log fetch: {e}")
    try:
        account_lock_service._load_from_disk()
    except Exception:
        pass

    today = datetime.now().date()
    window = _get_retention_window()
    default_days = window.get("retention_days", VIOLATION_RETENTION_DAYS)

    date_from_str = request.args.get("date_from")
    date_to_str = request.args.get("date_to")
    days_param = request.args.get("days")

    if date_from_str and date_to_str:
        try:
            week_start = datetime.strptime(date_from_str, "%Y-%m-%d").date()
            week_end = datetime.strptime(date_to_str, "%Y-%m-%d").date()
        except Exception:
            week_start = today - timedelta(days=default_days - 1)
            week_end = today
    elif days_param:
        try:
            d = max(1, min(int(days_param), 90))
            week_start = today - timedelta(days=d - 1)
            week_end = today
        except Exception:
            week_start = today - timedelta(days=default_days - 1)
            week_end = today
    else:
        # Default: full 14-day retention policy window
        week_start = today - timedelta(days=default_days - 1)
        week_end = today

    all_events: List[dict] = []
    seen_event_keys = set()
    num_days = (week_end - week_start).days + 1
    for delta in range(num_days):
        day = week_start + timedelta(days=delta)
        f = VIOLATIONS_DIR / f"audit_{day.isoformat()}.jsonl"
        if not f.exists():
            f = VIOLATIONS_DIR / f"violations_{day.isoformat()}.jsonl"
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        rec = json.loads(line)
                        rec.setdefault("date", day.isoformat())

                        # Ignore Google SSO authentication redirects (SetSID / Gmail login)
                        dom_low = str(rec.get("domain") or rec.get("host") or "").lower().strip()
                        url_low = str(rec.get("url") or "").lower()
                        if dom_low in ("accounts.youtube.com", "accounts.google.com") or "/accounts/setsid" in url_low:
                            continue

                        trno_val = str(rec.get("trno", "")).strip()
                        ts_val = str(rec.get("timestamp") or rec.get("ts") or "")[:16] # up to the minute
                        ev_key = (trno_val, ts_val, dom_low) if trno_val and ts_val else rec.get("incident_id")

                        if ev_key not in seen_event_keys:
                            seen_event_keys.add(ev_key)
                            all_events.append(rec)
                        else:
                            # Update existing with higher hit count or richer details if duplicate exists
                            for existing in all_events:
                                cur_sig = (
                                    str(existing.get("trno", "")).strip(),
                                    str(existing.get("timestamp") or existing.get("ts") or "")[:16],
                                    str(existing.get("domain") or existing.get("host") or "").lower().strip()
                                )
                                if cur_sig == ev_key:
                                    if (rec.get("hit_count") or 0) > (existing.get("hit_count") or 0):
                                        existing.update(rec)
                                    break
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue

    all_events.sort(
        key=lambda r: str(r.get("timestamp") or r.get("ts") or ""),
        reverse=True,
    )

    # Preload roster and all accounts once for fast in-memory resolution
    from app.student_roster import load_student_roster
    roster = load_student_roster() or {}
    all_accounts = account_lock_service.get_all_accounts()
    station_cache = {}

    # Group by TRNO
    from collections import OrderedDict
    by_student: OrderedDict = OrderedDict()
    for ev in all_events:
        trno = str(ev.get("trno", "unknown")).strip()
        by_student.setdefault(trno, []).append(ev)

    result = []
    for trno, ev_list in by_student.items():
        # Deduplicate student's incidents so repeated hits in the same minute/domain are collapsed into 1 row
        deduped_evs = []
        seen_student_sigs = set()
        for ev in ev_list:
            sig = (
                str(ev.get("timestamp") or ev.get("ts") or "")[:16],
                str(ev.get("domain") or ev.get("host") or "").lower().strip()
            )
            if sig in seen_student_sigs:
                continue
            seen_student_sigs.add(sig)
            deduped_evs.append(ev)
        ev_list = deduped_evs

        first = ev_list[0]
        worst_sev = "LOW"
        norm_st = station_cache.get(trno)
        for r in ev_list:
            s = r.get("severity", "LOW")
            if s == "HIGH":
                worst_sev = "HIGH"
            elif s == "MEDIUM" and worst_sev != "HIGH":
                worst_sev = "MEDIUM"

            if not norm_st:
                raw_st = r.get("station") or r.get("computer") or ""
                norm_st = lab_manager.normalize_station_name(raw_st)
                if not norm_st:
                    ev_ts = r.get("timestamp") or r.get("ts")
                    norm_st = lab_manager.get_workstation_for_trno(trno, timestamp=ev_ts)
                if not norm_st:
                    norm_st = lab_manager.get_workstation_for_trno(trno) or "—"
                station_cache[trno] = norm_st
            r["station"] = norm_st

        account_status = "active"
        blocked_at = ""
        days_remaining = 0
        acc = all_accounts.get(trno, {})
        if acc:
            account_status = acc.get("status", "active")
            blocked_at = acc.get("blocked_at") or acc.get("last_incident") or ""
            if account_status == "blocked" and blocked_at:
                days_remaining = _days_remaining_in_policy(blocked_at)

        s_info = roster.get(trno, {})
        student_name = first.get("student_name")
        darajah = first.get("darajah") or s_info.get("darajah") or s_info.get("class") or ""
        group = first.get("group") or s_info.get("group") or "Talabaat"
        if not student_name or str(student_name).strip().lower() in ("unknown", "student #", ""):
            student_name = s_info.get("name") or lab_manager.get_student_name(trno)

        # Resolve authoritative incident timestamp
        ev_ts = first.get("timestamp") or first.get("ts") or ""
        resolved_last_seen = ev_ts
        if blocked_at and (not ev_ts or blocked_at > ev_ts):
            resolved_last_seen = blocked_at

        result.append({
            "trno": trno,
            "student_name": student_name or f"Student #{trno}",
            "darajah": darajah or "",
            "group": group or "Talabaat",
            "total_violations": len(ev_list),
            "account_status": account_status,
            "worst_severity": worst_sev,
            "last_seen": resolved_last_seen,
            "blocked_at": blocked_at,
            "days_remaining": days_remaining,
            "policy_window_days": VIOLATION_RETENTION_DAYS,
            "violations": ev_list,
        })

    # Ensure all currently blocked and warned students from account_lock_service are included
    try:
        from app.student_roster import load_student_roster
        account_lock_service._load_from_disk()
        roster = load_student_roster() or {}
        active_locks = account_lock_service.get_blocked_students() + account_lock_service.get_warned_students()
        for acc in active_locks:
            sid = str(acc.get("trno", "")).strip()
            if not sid or sid in by_student or not sid.isdigit():
                continue
            s_profile = roster.get(sid, {}) or {}
            b_at = acc.get("blocked_at") or acc.get("last_incident") or ""
            days_rem = _days_remaining_in_policy(b_at) if acc.get("status") == "blocked" and b_at else 0
            reason = acc.get("blocked_reason") or "Account suspended under institutional policy."
            station = acc.get("station") or acc.get("last_station") or ""
            norm_st = lab_manager.normalize_station_name(station) or lab_manager.get_workstation_for_trno(sid)
            if not norm_st:
                norm_st = "—"
            r_low = reason.lower()
            cat = "policy_violation"
            if any(k in r_low for k in ("movie", "film", "stream", "download", "pirat", "video", "youtube", "netflix")):
                cat = "movie_download"
            elif any(k in r_low for k in ("social", "facebook", "instagram", "tiktok", "whatsapp", "twitter", "threads")):
                cat = "social_media"
            elif any(k in r_low for k in ("game", "gaming", "roblox", "steam")):
                cat = "gaming"
            elif any(k in r_low for k in ("proxy", "vpn", "psiphon", "tor")):
                cat = "proxy"
            elif any(k in r_low for k in ("torrent", "p2p", "utorrent")):
                cat = "torrent"
            elif any(k in r_low for k in ("software", ".exe", "install", "executable")):
                cat = "software"
            elif any(k in r_low for k in ("adult", "prohibited", "porn")):
                cat = "adult"

            v_list = acc.get("violations") or [{
                "action": "block",
                "category": cat,
                "reason": reason,
                "timestamp": b_at,
                "ts": b_at,
                "station": norm_st,
                "url": f"Suspension: {reason}",
                "severity": "HIGH",
            }]
            result.append({
                "trno": sid,
                "student_name": acc.get("name") or s_profile.get("name") or f"Student #{sid}",
                "darajah": acc.get("darajah") or s_profile.get("darajah") or "General",
                "group": s_profile.get("branch") or s_profile.get("group") or "Talabaat",
                "total_violations": len(v_list),
                "account_status": acc.get("status", "blocked"),
                "worst_severity": "HIGH",
                "last_seen": acc.get("last_incident") or b_at or "",
                "blocked_at": b_at,
                "blocked_reason": reason,
                "days_remaining": days_rem,
                "policy_window_days": VIOLATION_RETENTION_DAYS,
                "violations": v_list,
            })
            for ev in v_list:
                all_events.append(ev)
    except Exception as _e:
        logger.warning("Error appending blocked accounts to weekly violation log: %s", _e)

    # Sort: blocked first, then most recent incident timestamp DESCENDING (today, yesterday, then older), then HIGH severity
    def parse_ts(val):
        try:
            return datetime.fromisoformat(str(val)[:19]).timestamp()
        except Exception:
            return 0.0

    def sort_key(r):
        status_order = {"blocked": 0, "warned": 1, "active": 2}
        sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        ts = parse_ts(r.get("last_seen") or r.get("blocked_at"))
        return (
            status_order.get(r["account_status"], 9),
            -ts,
            sev_order.get(r["worst_severity"], 3),
            -r["total_violations"],
        )
    result.sort(key=sort_key)

    resp = jsonify({
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "retention_days": VIOLATION_RETENTION_DAYS,
        "policy_note": window.get("policy_text", ""),
        "total_students": len(result),
        "total_events": len(all_events),
        "students": result,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ─────────────────────────────────────────────────────────────────
# 12. Lab Manager Sync Status
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/lab-manager-sync-status.json")
def lab_manager_sync_status():
    """Live sync telemetry from Lab Manager Service."""
    _require_admin()
    return jsonify(lab_manager.get_sync_telemetry())


# ─────────────────────────────────────────────────────────────────
# 13. DNS Blocklist Export
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/dns-blocklist.txt")
def dns_blocklist():
    path = policy_engine.export_dns_blocklist()
    return Response(path.read_text(encoding="utf-8"), mimetype="text/plain")


# ─────────────────────────────────────────────────────────────────
# 14. Session Lifecycle & Time Truth Endpoints (Hardened)
# ─────────────────────────────────────────────────────────────────

class ValidationError(ValueError):
    """Lightweight validation error replacement for pydantic.ValidationError."""
    def __init__(self, message: str, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self._errors = errors or [{"msg": message}]

    def errors(self) -> List[Dict[str, Any]]:
        return self._errors


class SessionStartModel:
    def __init__(
        self,
        trno: str = "",
        station: str = "",
        darajah: Optional[str] = "",
        session_id: Optional[str] = None,
        student_name: Optional[str] = "",
        is_igcse: Optional[bool] = False,
        monotonic_ns: Optional[int] = 0,
        **kwargs,
    ):
        if not str(trno or "").strip() or not str(station or "").strip():
            raise ValidationError(
                "trno and station are required",
                [{"loc": ["trno"], "msg": "Station and student TRNO must both be provided."}]
            )
        self.trno = str(trno).strip()
        self.station = str(station).strip()
        self.darajah = str(darajah or "")
        self.session_id = str(session_id) if session_id else None
        self.student_name = str(student_name or "")
        self.is_igcse = bool(is_igcse)
        self.monotonic_ns = int(monotonic_ns or 0)


class SessionHeartbeatModel:
    def __init__(
        self,
        trno: str = "",
        station: str = "",
        session_id: str = "",
        monotonic_ns: int = 0,
        idle_sec: int = 0,
        current_url: Optional[str] = "",
        agent_uptime_sec: Optional[int] = 0,
        active_window: Optional[str] = "",
        process_name: Optional[str] = "",
        **kwargs,
    ):
        if not str(trno or "").strip() or not str(station or "").strip() or not str(session_id or "").strip():
            raise ValidationError(
                "trno, station, and session_id are required",
                [{"loc": ["session"], "msg": "trno, station, and session_id are required"}]
            )
        self.trno = str(trno).strip()
        self.station = str(station).strip()
        self.session_id = str(session_id).strip()
        self.monotonic_ns = int(monotonic_ns or 0)
        self.idle_sec = int(idle_sec or 0)
        self.current_url = str(current_url or "")
        self.agent_uptime_sec = int(agent_uptime_sec or 0)
        self.active_window = str(active_window or "")
        self.process_name = str(process_name or "")


class SessionEndModel:
    def __init__(
        self,
        trno: str = "",
        station: Optional[str] = "",
        session_id: Optional[str] = "",
        reason: Optional[str] = "normal_logout",
        monotonic_ns: Optional[int] = 0,
        **kwargs,
    ):
        if not str(trno or "").strip():
            raise ValidationError(
                "trno is required",
                [{"loc": ["trno"], "msg": "trno is required"}]
            )
        self.trno = str(trno).strip()
        self.station = str(station or "")
        self.session_id = str(session_id or "")
        self.reason = str(reason or "normal_logout")
        self.monotonic_ns = int(monotonic_ns or 0)


@enforcement_bp.post("/session/start")
def session_start():
    """
    Register or resume a student lab session.
    Idempotent by (trno, station, session_id).
    Validates account lock status, rejects backwards monotonic clock.
    """
    raw_data = request.get_json(silent=True) or {}
    try:
        req = SessionStartModel(**raw_data)
    except ValidationError as ve:
        logger.warning("[SessionStart] Validation error: %s", ve)
        return jsonify({
            "error": "Validation failed",
            "code": "VALIDATION_ERROR",
            "detail": ve.errors()
        }), 400

    trno = req.trno.strip()
    station = req.station.strip().upper()
    if not trno or not station:
        return jsonify({
            "error": "TRNO and Station are required",
            "code": "MISSING_IDENTITY",
            "detail": "Station and student TRNO must both be provided."
        }), 400

    # Reject if account disabled
    if account_lock_service.is_account_blocked(trno):
        logger.warning("[SessionStart] Refused for suspended student TRNO=%s", trno)
        return jsonify({
            "error": "Account is disabled",
            "code": "ACCOUNT_DISABLED",
            "detail": f"Student TRNO {trno} account is suspended. Contact teacher or lab administrator."
        }), 403

    session_id = req.session_id or f"{trno}_{station}_{int(time.time())}"

    try:
        result = session_timer_service.start_session(
            trno=trno,
            darajah=req.darajah or "",
            station=station,
            student_name=req.student_name or "",
            is_igcse=bool(req.is_igcse),
            session_id=session_id,
            monotonic_ns=req.monotonic_ns or 0,
        )
    except ValueError as e:
        logger.error("[SessionStart] Monotonic clock error: %s", e)
        return jsonify({
            "error": "Invalid monotonic clock",
            "code": "MONOTONIC_ERROR",
            "detail": str(e)
        }), 400

    return jsonify({
        "ok": True,
        "server_seq": event_log_service._server_seq,
        "server_ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": session_id,
        "limit_minutes": result.get("limit_minutes", 60),
        "expires_at": result.get("expires_at"),
        "status": result.get("status", "active"),
        **result
    })


@enforcement_bp.post("/session/heartbeat")
def session_heartbeat():
    """
    Heartbeat update received every 30s from client agent.
    Computes idle-aware duration and checks for pending commands.
    """
    raw_data = request.get_json(silent=True) or {}
    try:
        req = SessionHeartbeatModel(**raw_data)
    except ValidationError as ve:
        logger.warning("[SessionHeartbeat] Validation error: %s", ve)
        return jsonify({
            "error": "Validation failed",
            "code": "VALIDATION_ERROR",
            "detail": ve.errors()
        }), 400

    trno = req.trno.strip()
    station = req.station.strip().upper()
    session_id = req.session_id.strip()

    try:
        sess = time_accounting_service.record_heartbeat(
            trno=trno,
            station=station,
            session_id=session_id,
            monotonic_ns=req.monotonic_ns,
            idle_sec=req.idle_sec,
            current_url=req.current_url or "",
            agent_uptime_sec=req.agent_uptime_sec or 0,
            active_window=req.active_window or "",
            process_name=req.process_name or "",
        )
    except ValueError as e:
        logger.error("[SessionHeartbeat] Clock error: %s", e)
        return jsonify({
            "error": "Monotonic clock regression",
            "code": "MONOTONIC_ERROR",
            "detail": str(e)
        }), 400

    # Pop any administrative command
    cmd = session_timer_service.pop_pending_command(station=station, trno=trno)

    # Immediate enforcement check: if account is blocked/disabled, forcefully lock terminal
    if account_lock_service.is_account_blocked(trno):
        logger.warning(f"[SessionHeartbeat] Student TRNO {trno} is SUSPENDED. Forcing terminal LOCK on {station}.")
        return jsonify({
            "ok": True,
            "command": "lock",
            "status": "account_disabled",
            "code": "ACCOUNT_DISABLED",
            "message": "Student account is suspended by policy. Workstation locked.",
            "remaining_seconds": 0,
        })

    # Check status against time limit
    status_info = session_timer_service.get_session_status(trno=trno, station=station)
    status = status_info.get("status", "active")
    rem_sec = status_info.get("remaining_seconds", 3600)

    return jsonify({
        "ok": True,
        "command": cmd or status_info.get("command"),
        "status": status,
        "remaining_seconds": rem_sec,
        "duration_sec": sess.get("duration_sec", 0),
        "active_lab_sec": sess.get("active_lab_sec", 0),
        "idle_away_sec": sess.get("idle_away_sec", 0),
    })


@enforcement_bp.post("/session/end")
def session_end():
    """Idempotently close session truth record with final duration."""
    raw_data = request.get_json(silent=True) or {}
    try:
        req = SessionEndModel(**raw_data)
    except ValidationError as ve:
        return jsonify({
            "error": "Validation failed",
            "code": "VALIDATION_ERROR",
            "detail": ve.errors()
        }), 400

    trno = req.trno.strip()
    station = (req.station or "").strip().upper()
    session_id = (req.session_id or "").strip()
    reason = req.reason or "normal_logout"

    ok = session_timer_service.end_session(trno, reason=reason)
    closed = time_accounting_service.record_session_end(
        trno=trno,
        station=station,
        session_id=session_id,
        monotonic_ns=req.monotonic_ns or 0,
        reason=reason,
    )

    return jsonify({
        "ok": True,
        "session_id": session_id,
        "final_duration_sec": closed.get("duration_sec", 0),
        "message": f"Session ended for TRNO {trno}",
    })


@enforcement_bp.get("/session/status")
def session_status():
    """Query current session status and pop queued remote commands."""
    trno = str(request.args.get("trno") or "").strip()
    station = str(request.args.get("station") or "").strip()
    if not trno and not station:
        return jsonify({
            "error": "trno or station is required",
            "code": "MISSING_PARAM",
            "detail": "Provide either ?trno= or ?station="
        }), 400
    return jsonify(session_timer_service.get_session_status(trno, station=station))


# ─────────────────────────────────────────────────────────────────
# Health, Audit Trace & Time Truth Endpoints
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/api/health")
def api_health():
    """Authoritative system health, telemetry freshness, and policy status."""
    freshness = time_accounting_service.get_freshness_metrics()
    dev_mode = os.getenv("SENTINEL_DEV_MODE", "0") == "1"

    return jsonify({
        "active_sessions": freshness["active_sessions_count"],
        "stale_sessions": freshness["stale_sessions_count"],
        "last_heartbeat_age_sec": freshness["last_heartbeat_age_sec"],
        "event_log_today_count": freshness["today_events_count"],
        "rollup_last_computed": freshness["last_rollup_computed"],
        "policy_loaded": getattr(policy_engine, "_policy", None) is not None,
        "dev_mode": dev_mode,
        "data_freshness_ok": freshness["data_freshness_ok"],
        "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


@enforcement_bp.get("/api/session/<session_id>/trace")
def api_session_trace(session_id: str):
    """Retrieve audit-grade event trace for a specific session."""
    session_id = str(session_id).strip()
    events = event_log_service.get_events_for_session(session_id)
    return jsonify({
        "session_id": session_id,
        "event_count": len(events),
        "events": events,
        "source": "event_log",
    })


@enforcement_bp.get("/api/student/<trno>/time-truth")
def api_student_time_truth(trno: str):
    """Retrieve authoritative lab dwell time and active minutes for student."""
    trno = str(trno).strip()
    truth = time_accounting_service.get_student_time_truth(trno)
    return jsonify(truth)


@enforcement_bp.get("/student/<trno>/info")
def api_student_profile_info(trno: str):
    """
    Resolve student profile (name, darajah, class, gender) for client agent auto-discovery.
    Uses Koha LMS roster first, then Lab Manager users.xlsx, then account lock records.
    """
    trno = str(trno).strip()
    if not trno:
        return jsonify({"ok": False, "error": "TRNO required"}), 400

    name = ""
    darajah = ""
    group = "Talabaat"
    cls = ""
    gender = "Mixed"

    # 1. Koha student roster
    try:
        from app.student_roster import get_student_info
        info = get_student_info(trno)
        if info:
            name = info.get("name") or ""
            darajah = info.get("darajah") or ""
            cls = info.get("class") or ""
            gender = info.get("gender") or "Mixed"
            group = info.get("group") or group
    except Exception as e:
        logger.debug("[Enforcement] Koha lookup error for %s: %s", trno, e)

    # 2. Lab Manager users.xlsx fallback
    if not name or not darajah:
        try:
            roster = lab_manager.get_users_roster()
            if trno in roster:
                u = roster[trno]
                name = name or u.get("name", "")
                darajah = darajah or u.get("class", "")
        except Exception as e:
            logger.debug("[Enforcement] LabManager roster lookup error: %s", e)

    # 3. Account lock service fallback
    if not name:
        acc = account_lock_service.get_account(trno)
        name = acc.get("name") or f"Student #{trno}"
        darajah = darajah or acc.get("darajah") or "General"

    name = name or f"Student #{trno}"
    darajah = darajah or "General"

    return jsonify({
        "ok": True,
        "trno": trno,
        "name": name,
        "darajah": darajah,
        "class": cls or darajah,
        "gender": gender,
        "group": group,
    })


@enforcement_bp.get("/station/<station>/active-student")
def api_station_active_student(station: str):
    """
    Discover which student is currently active on this workstation from Lab Manager or live sessions.
    Allows client agents to dynamically provision without manual files.
    """
    station_norm = lab_manager.normalize_station_name(station)
    if not station_norm:
        return jsonify({"ok": False, "found": False, "error": "Invalid station"}), 400

    # 1. Check live Sentinel session timer
    active_sessions = session_timer_service.get_all_active_sessions()
    for s in active_sessions:
        s_mach = lab_manager.normalize_station_name(s.get("station"))
        if s_mach == station_norm and s.get("status") in ("active", "warned"):
            trno = str(s.get("trno", "")).strip()
            name = s.get("student_name") or s.get("name") or ""
            darajah = s.get("darajah") or ""
            return jsonify({
                "ok": True,
                "found": True,
                "trno": trno,
                "name": name or f"Student #{trno}",
                "darajah": darajah or "General",
                "station": station_norm,
                "source": "sentinel_session",
            })

    # 2. Check Lab Manager session_history.json
    raw_sessions = lab_manager.get_all_sessions()
    for s in raw_sessions:
        if not isinstance(s, dict):
            continue
        status = str(s.get("status", "")).lower()
        if status in ("active", "running", "logged_in"):
            m = lab_manager.normalize_station_name(s.get("machine_id") or s.get("station") or s.get("computer"))
            if m == station_norm:
                trno = str(s.get("username") or s.get("user") or s.get("trno") or "").strip()
                if trno:
                    name = lab_manager.get_student_name(trno)
                    return jsonify({
                        "ok": True,
                        "found": True,
                        "trno": trno,
                        "name": name,
                        "darajah": "General",
                        "station": station_norm,
                        "source": "lab_manager",
                    })

    return jsonify({"ok": True, "found": False, "station": station_norm})



@enforcement_bp.get("/session-expired")
def session_expired_page():
    trno = request.args.get("trno", "")
    station = request.args.get("station", "")
    darajah = request.args.get("darajah", "")
    limit = int(request.args.get("limit", session_timer_service.get_time_limit(darajah)))

    student_name = ""
    if trno:
        acc = account_lock_service.get_account(trno)
        student_name = acc.get("name", "")

    return render_template(
        "session_expired.html",
        trno=trno,
        station=station,
        darajah=darajah,
        limit_minutes=limit,
        student_name=student_name,
    )


@enforcement_bp.get("/admin/sessions-live.json")
def sessions_live():
    _require_admin()
    sessions = session_timer_service.get_sessions_live()
    active = [s for s in sessions if s.get("status") == "active"]
    warned = [s for s in sessions if s.get("status") == "warned"]
    expired = [s for s in sessions if s.get("status") == "expired"]
    return jsonify({
        "sessions": sessions,
        "total_active": len(active),
        "total_warned": len(warned),
        "total_expired": len(expired),
        "time_limits": DARAJAH_TIME_LIMITS,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })


@enforcement_bp.post("/admin/session-extend")
def admin_extend_session():
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    extra = int(data.get("extra_minutes", 15))
    note = str(data.get("note") or "Extended by administrator").strip()
    if not trno:
        return jsonify({"ok": False, "error": "trno is required"}), 400
    result = session_timer_service.extend_session(trno, extra_minutes=extra, admin_user=note)
    return jsonify(result)


@enforcement_bp.post("/admin/session-end")
def admin_end_session():
    _require_admin()
    data = request.get_json(silent=True) or {}
    trno = str(data.get("trno") or "").strip()
    if not trno:
        return jsonify({"ok": False, "error": "trno is required"}), 400
    ok = session_timer_service.end_session(trno, reason="admin_forced_end")
    return jsonify({"ok": ok})


@enforcement_bp.post("/admin/session-end-all")
def admin_end_all_sessions():
    _require_admin()
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "admin_emergency_end_all").strip()
    count = session_timer_service.end_all_sessions(reason=reason)
    return jsonify({
        "ok": True,
        "count": count,
        "message": f"Successfully ended all {count} active student lab sessions.",
    })


@enforcement_bp.get("/admin/time-limits")
def admin_time_limits():
    _require_admin()
    return jsonify({
        "time_limits": DARAJAH_TIME_LIMITS,
        "warning_threshold_minutes": 5,
        "policy": {
            "darajah_1_to_4": "30 minutes",
            "darajah_5_to_7": "60 minutes",
            "igcse": "120 minutes",
        },
    })


# ─────────────────────────────────────────────────────────────────
# 15. Retention Policy Endpoints (14-Day Institutional Rule)
# ─────────────────────────────────────────────────────────────────
@enforcement_bp.get("/admin/retention-policy.json")
def retention_policy_status():
    """
    Return the current institutional retention policy:
      - Violation retention window: 14 days
      - Audit log archive threshold: 14 days
      - Total active blocked accounts
      - Count of accounts expiring in the next 48 hours
    """
    _require_admin()

    window = _get_retention_window()
    blocked = account_lock_service.get_blocked_students()

    expiring_soon = 0
    for b in blocked:
        blocked_at = b.get("blocked_at") or b.get("last_incident") or ""
        days_left = _days_remaining_in_policy(blocked_at)
        if 0 < days_left <= 2:
            expiring_soon += 1

    return jsonify({
        "retention_days": VIOLATION_RETENTION_DAYS,
        "archive_threshold_days": AUDIT_RETENTION_DAYS,
        "window_start": window.get("window_start"),
        "window_end": window.get("window_end"),
        "policy_text": window.get("policy_text", ""),
        "total_blocked": len(blocked),
        "expiring_within_48h": expiring_soon,
        "service_available": _RETENTION_AVAILABLE,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })


@enforcement_bp.post("/admin/run-retention-sweep")
def run_retention_sweep():
    """
    Manually trigger the retention policy sweep:
      - Auto-unblock accounts past the 14-day window
      - Archive audit logs older than 14 days
    """
    _require_admin()

    if not _RETENTION_AVAILABLE or not retention_policy_service:
        return jsonify({
            "ok": False,
            "error": "Retention policy service is not available. Check server logs.",
        }), 503

    try:
        result = retention_policy_service.run_all()
        return jsonify({
            "ok": True,
            "expired_count": result.get("expired_count", 0),
            "archived_count": result.get("archived_count", 0),
            "retention_window": result.get("retention_window", {}),
            "message": (
                f"Retention sweep complete. "
                f"{result.get('expired_count', 0)} accounts auto-unblocked, "
                f"{result.get('archived_count', 0)} audit logs archived."
            ),
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:
        logger.error(f"[RetentionSweep] Failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@enforcement_bp.post("/admin/sync-violations")
def admin_sync_violations():
    """
    Manually trigger synchronizing real policy violations from browsing cache
    and Lab Manager workstation sessions into the 14-day audit log.
    """
    _require_admin()
    try:
        from tools.sync_real_violations import sync_violations_from_cache
        res = sync_violations_from_cache(days_back=14)
        return jsonify(res)
    except Exception as e:
        logger.error(f"[SyncViolations] Failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500