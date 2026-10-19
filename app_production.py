# app_production.py
"""
MamalBrowse Sentinel Production Server — Waitress WSGI.

2026-10-03 changes:
  - Integrated 14-day violation retention policy service:
      * Hourly sweep auto-unblocks accounts past the 14-day window
      * Audit logs older than 14 days move to students/violations/archive/
      * Preflight logs the current retention window and active suspension count
  - Startup retention sweep runs immediately after preflight

2026-10-02 changes:
  - Background sync is READ-ONLY (only refreshes active_sessions.json)
  - No violation seeding — violations only come from real client agent events
  - Daily PDF report auto-generated at 00:05, weekly at Sunday 00:10
  - Preflight verifies no auto-block is configured
"""
import os
import sys
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from waitress import serve

load_dotenv()

# UTF-8 console on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("production.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("mamalbrowse_sentinel_production")

ROOT_DIR = Path(__file__).resolve().parent
APP_DIR = ROOT_DIR / "app"
for p in [str(ROOT_DIR), str(APP_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from app.main import app
from app.database.koha_connection import koha_db
from app.services.lab_manager_service import lab_manager


# ═════════════════════════════════════════════════════════════════
# Background Loops
# ═════════════════════════════════════════════════════════════════
def _readonly_session_sync_loop():
    """Reconcile active sessions and truth aggregates every 5 minutes."""
    while True:
        time.sleep(300)
        try:
            from app.services.time_accounting_service import time_accounting_service
            res = time_accounting_service.reconcile_aggregates()
            logger.info(
                "[AutoSync] Reconciled %d active sessions from event log and Lab Manager",
                res.get("active_count", 0),
            )
        except Exception as e:
            logger.warning("[AutoSync] Session reconciliation failed: %s", e)


def _retention_policy_loop():
    """
    Enforce the 14-day institutional retention policy every hour.

    Actions per sweep:
      1. Auto-unblock student accounts blocked > 14 days ago
      2. Archive audit logs older than 14 days to students/violations/archive/
      3. Log a compact summary so operators can trace the policy activity
    """
    while True:
        time.sleep(3600)  # every hour
        try:
            from app.services.retention_policy_service import retention_policy_service
            result = retention_policy_service.run_all()

            expired = result.get("expired_count", 0)
            archived = result.get("archived_count", 0)

            if expired > 0 or archived > 0:
                logger.info(
                    "[RetentionPolicy] Hourly sweep: %d account(s) auto-unblocked, "
                    "%d audit log(s) archived",
                    expired, archived,
                )
                if expired > 0:
                    logger.info(
                        "[RetentionPolicy] Auto-unblocked TRNOs: %s",
                        ", ".join(result.get("expired_trnos", [])[:20]),
                    )
            else:
                logger.debug(
                    "[RetentionPolicy] Hourly sweep: no changes (nothing past 14 days)"
                )
        except Exception as e:
            logger.warning("[RetentionPolicy] Hourly sweep failed: %s", e)


def _daily_report_loop():
    """Generate daily PDF at 00:05 every day."""
    while True:
        now = datetime.now()
        next_run = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        time.sleep(sleep_seconds)
        try:
            from app.services.incident_service import incident_service
            incident_service.archive_daily_pdf()
            logger.info("[Reports] Daily PDF archived")
        except Exception as e:
            logger.warning("[Reports] Daily report generation failed: %s", e)


def _weekly_report_loop():
    """Generate weekly PDF every Sunday at 00:10."""
    while True:
        now = datetime.now()
        days_until_sunday = (6 - now.weekday()) % 7
        next_run = now + timedelta(days=days_until_sunday)
        next_run = next_run.replace(hour=0, minute=10, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=7)
        sleep_seconds = (next_run - now).total_seconds()
        time.sleep(sleep_seconds)
        try:
            from app.services.incident_service import incident_service
            incident_service.archive_weekly_pdf()
            logger.info("[Reports] Weekly PDF archived")
        except Exception as e:
            logger.warning("[Reports] Weekly report generation failed: %s", e)


# ═════════════════════════════════════════════════════════════════
# Preflight
# ═════════════════════════════════════════════════════════════════
def _retention_preflight():
    """
    Verify retention policy service is available and run a startup sweep.
    Logs the current 14-day window and any pending actions.
    """
    logger.info("-" * 65)
    logger.info("  14-DAY VIOLATION RETENTION POLICY")
    logger.info("-" * 65)

    try:
        from app.services.retention_policy_service import (
            retention_policy_service,
            VIOLATION_RETENTION_DAYS,
            AUDIT_RETENTION_DAYS,
        )
    except ImportError as e:
        logger.warning(
            "[!] RetentionPolicy service NOT installed: %s. "
            "Falling back to default 14-day policy without auto-sweep.",
            e,
        )
        return

    # Report the active policy constants
    logger.info(
        "[OK] RetentionPolicy: VIOLATION_RETENTION_DAYS=%d, AUDIT_RETENTION_DAYS=%d",
        VIOLATION_RETENTION_DAYS, AUDIT_RETENTION_DAYS,
    )

    # Report the current window
    try:
        window = retention_policy_service.get_retention_window()
        logger.info(
            "[OK] Retention window: %s → %s",
            window.get("window_start"), window.get("window_end"),
        )
    except Exception as e:
        logger.warning("[!] Could not read retention window: %s", e)

    # Run a startup sweep so anything past 14 days is cleared immediately
    try:
        result = retention_policy_service.run_all()
        logger.info(
            "[OK] Startup sweep: %d account(s) auto-unblocked, "
            "%d audit log(s) archived",
            result.get("expired_count", 0),
            result.get("archived_count", 0),
        )
        if result.get("expired_count", 0) > 0:
            logger.info(
                "[OK] Auto-unblocked TRNOs: %s",
                ", ".join(result.get("expired_trnos", [])[:20]),
            )
    except Exception as e:
        logger.warning("[!] Startup retention sweep failed: %s", e)

    # Report how many students are currently suspended
    try:
        from app.services.account_lock_service import account_lock_service
        blocked = account_lock_service.get_blocked_students()
        logger.info(
            "[OK] Currently suspended accounts: %d "
            "(will auto-expire after serving the 14-day policy window)",
            len(blocked),
        )
    except Exception as e:
        logger.warning("[!] Could not count suspended accounts: %s", e)


def preflight_check():
    logger.info("=" * 65)
    logger.info("  BROWSEGUARD ENTERPRISE — PRODUCTION PREFLIGHT CHECK")
    logger.info("=" * 65)

    # ── Koha DB ──
    koha_ok = koha_db.is_connected()
    if koha_ok:
        logger.info("[OK] Koha Database: CONNECTED")
    else:
        logger.warning("[!] Koha Database: UNREACHABLE (using disk cache)")

    # ── Lab Manager ──
    try:
        lm_path = lab_manager.share_path
        if os.path.exists(lm_path):
            logger.info("[OK] Lab Manager: ACCESSIBLE (%s)", lm_path)
        else:
            logger.warning("[!] Lab Manager path not found: %s", lm_path)
    except Exception as e:
        logger.warning("[!] Lab Manager check failed: %s", e)

    # ── Violation & incident services ──
    try:
        from app.services.violation_service import violation_service  # noqa
        from app.services.incident_service import incident_service  # noqa
        logger.info("[OK] ViolationService: READY (single authoritative writer)")
        logger.info("[OK] IncidentService: READY (live alerts + PDF reports)")
    except Exception as e:
        logger.error("[X] Violation/Incident services failed to load: %s", e)

    # ── Verify no auto-block ──
    try:
        from app.routes.enforcement import verify_no_auto_block
        verify_no_auto_block()
    except Exception as e:
        logger.debug("auto-block check skipped: %s", e)

    # ── Required directories ──
    for sub in (
        "data",
        "students/violations",
        "students/violations/archive",   # 14-day archive destination
        "reports/archive",
        "logs",
    ):
        (ROOT_DIR / sub).mkdir(parents=True, exist_ok=True)
    logger.info("[OK] Directories verified")

    # ── 14-Day Retention Policy ──
    _retention_preflight()

    # ── One-shot session sync ──
    try:
        from app.services.time_accounting_service import time_accounting_service
        res = time_accounting_service.reconcile_aggregates()
        logger.info(
            "[OK] Initial Lab Manager session sync complete: %d active session(s)",
            res.get("active_count", 0),
        )
    except Exception as e:
        logger.warning("[!] Initial session sync failed: %s", e)

    # ── Start background loops (daemon threads) ──
    threading.Thread(
        target=_readonly_session_sync_loop,
        daemon=True, name="SessionSync",
    ).start()
    threading.Thread(
        target=_retention_policy_loop,
        daemon=True, name="RetentionPolicySweep",
    ).start()
    threading.Thread(
        target=_daily_report_loop,
        daemon=True, name="DailyReport",
    ).start()
    threading.Thread(
        target=_weekly_report_loop,
        daemon=True, name="WeeklyReport",
    ).start()

    logger.info(
        "[OK] Background loops started: "
        "session sync (5m), retention sweep (1h), "
        "daily report (00:05), weekly report (Sun 00:10)"
    )

    # ── Koha roster prewarm ──
    try:
        from app.student_roster import load_student_roster
        threading.Thread(
            target=load_student_roster,
            daemon=True, name="RosterPrewarm",
        ).start()
        logger.info("[OK] Koha roster prewarm started")
    except Exception as e:
        logger.warning("[!] Roster prewarm failed: %s", e)

    logger.info("=" * 65)
    logger.info("  PREFLIGHT COMPLETE — STARTING SERVER")
    logger.info("=" * 65)


if __name__ == "__main__":
    preflight_check()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 5050))
    threads = int(os.getenv("WAITRESS_THREADS", 24))

    logger.info(
        "Starting Waitress on http://%s:%d with %d worker threads",
        host, port, threads,
    )
    try:
        serve(
            app,
            host=host,
            port=port,
            threads=threads,
            connection_limit=200,
            channel_timeout=30,
            cleanup_interval=30,
        )
    except Exception as e:
        logger.critical("Server crashed: %s", e)
        sys.exit(1)