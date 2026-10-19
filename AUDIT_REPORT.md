# MAMALBROWSE SENTINEL — DATA INTEGRITY AUDIT & PURGE REPORT

**System:** MamalBrowse Sentinel Lab-Surveillance Platform  
**Target Environment:** Al Jamea tus Saifiyah Institutional Server (`10.10.117.76:5000`)  
**Audit Date:** 2026-10-09  
**Auditor:** Systems & Data Integrity Engineering Team  
**Status:** COMPLETE & REMEDIATED — Production Dev Mode Default: **OFF (`0`)**

---

## 1. Executive Summary

An exhaustive audit of the MamalBrowse Sentinel codebase was conducted to identify and eliminate all sources of synthetic, mocked, seeded, or pre-baked telemetry and session data. The audit revealed five critical structural vulnerabilities:

1. **Synthetic Browsing History Generation:** `app/main.py` contained automatic bootstrap routines (`_startup_bootstrap_browsing_cache` and `_generate_fallback_browsing_data`) that fabricated 20–180 browsing records per student across 150 roster entries whenever network shares were unreachable, populating `data/browsing_history_cache.csv` with synthetic visits.
2. **Synthetic Violation Seeding:** `seed_violations_data.py` used `random.randint` and `random.sample` to seed fake violation entries into `students/violations/*.jsonl`, fake blocked statuses in `data/student_accounts.json`, and fake entries into `data/browsing_history_cache.csv`. This was automatically invoked on system startup via `_startup_bootstrap_violations()` in `app/main.py`.
3. **Synthetic Dwell & Lab Time Approximations:** Dwell times in `app/main.py` were computed using synthetic formulas (a 2-minute baseline per visit, gap bonuses clipped to 300 seconds) rather than client-reported monotonic timestamps and input idle states.
4. **Canned Dashboard & API Numbers:** Multiple endpoints (such as `/api/campus-stats` and focus score endpoints) returned hardcoded numbers (e.g. `active_students: 876`, `total_visits: 14000`, `avg_campus_score: 85`, `total_lab_hours: 9532.8`) when real data was missing instead of returning `0` with a `"no_data": true` flag.
5. **No Monotonic Single Source of Truth:** Student lab time was derived from disparate, uncoordinated timestamps and wall clocks without monotonic validation, heartbeat tracking, or Windows idle detection.

All offending routines have been purged from production execution paths, hardcoded metrics eliminated, and mock paths permanently gated behind `SENTINEL_DEV_MODE=1`.

---

## 2. Line-by-Line Findings & Remediation Matrix

| Finding ID | File Path | Line Range | Offending Code / Pattern | Severity | Root Cause & Description | Remediation Applied |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **AUD-001** | `app/main.py` | 75–80 | `seed_real_dashboard_data()` invoked at module load | **CRITICAL** | Overwrote `data/active_sessions.json` on server startup with stale/unverified records. | Removed from startup sequence. Gated behind `SENTINEL_DEV_MODE=1`. |
| **AUD-002** | `app/main.py` | 131–340 | `_startup_bootstrap_browsing_cache()` | **CRITICAL** | Fabricated browsing records for 150 students using demo domains pool and wrote to `data/browsing_history_cache.csv`. | Function disabled in production. Gated behind `SENTINEL_DEV_MODE=1`. |
| **AUD-003** | `app/main.py` | 342–364 | `_startup_bootstrap_violations()` | **CRITICAL** | Automatically called `seed_violations_data.seed_violations()` if violation logs were empty. | Automatic invocation removed entirely. Gated behind `SENTINEL_DEV_MODE=1`. |
| **AUD-004** | `app/main.py` | 585–687 | `_generate_fallback_browsing_data()` | **CRITICAL** | Generated synthetic browsing history from Koha roster whenever UNC paths were unreachable. | Purged from production load path. `load_data()` now returns empty DataFrame with `"no_data": true` if data is unavailable. |
| **AUD-005** | `app/main.py` | 835–845 | `fallback_df = _generate_fallback_browsing_data(...)` | **CRITICAL** | Fallback invocation in `load_data()` silently substituted fake data for missing files. | Removed. Production returns empty data structures without synthesis. |
| **AUD-006** | `app/main.py` | 1424–1429 | `total_lab_hours = hijri_trend.get(..., 9532.8)`<br>`total_bandwidth_gb = round((total_visits * 0.045) + ...)` | **HIGH** | Pre-baked fallback numbers and telemetry formulas simulated real bandwidth and threats. | Replaced with live aggregation from `time_accounting_service`. |
| **AUD-007** | `app/main.py` | 1608–1633 | `est_mins = 2.0`<br>`capped = deltas.clip(lower=30, upper=300)` | **HIGH** | Fabricated per-domain dwell time using heuristic clipping instead of real client events. | Replaced with verified event-log and heartbeat idle duration accounting. |
| **AUD-008** | `app/main.py` | 2333–2363 | `/api/campus-stats` canned response (`active_students: 876`, `total_visits: 14000`, etc.) | **CRITICAL** | Returned canned JSON metrics when browsing DataFrame was empty. | Removed canned object. Returns `0` for all metrics with `"no_data": true`. |
| **AUD-009** | `app/main.py` | 2447 | `avg_campus_score: 85` | **MEDIUM** | Hardcoded focus score when data missing. | Returns `0` with `"no_data": true`. |
| **AUD-010** | `seed_violations_data.py` | 1–478 | `random.seed(42)`, `random.sample`, synthetic violation loops | **CRITICAL** | Entire script synthesized fake violations and populated cache files. | Script protected with strict environment check: refuses execution unless `SENTINEL_DEV_MODE=1`. |
| **AUD-011** | `seed_real_dashboard_data.py` | 1–194 | Synthetic reconciliation logic | **HIGH** | Wrote synthetic session snapshots to `data/active_sessions.json`. | Protected with strict environment check: refuses execution unless `SENTINEL_DEV_MODE=1`. |
| **AUD-012** | `app_production.py` | 63–76 | `_readonly_session_sync_loop` called `seed_real_dashboard_data` | **HIGH** | Background thread repeatedly polluted session files. | Replaced with `time_accounting_service` reconciliation loop. |
| **AUD-013** | `client_agent/browseguard_agent.py` | 56, 80–107 | Fallback to `LAB-PC`, permissive missing identity file handling | **HIGH** | Allowed agent to run without valid identity files, risking orphan or misattributed sessions. | Hardened: agent halts with blocking OS alert if TRNO/darajah/name files are missing or empty. |
| **AUD-014** | `data/active_sessions.json` | 1–7667 | 7,667 lines of seeded fake active session records | **CRITICAL** | Seeded stale sessions displayed as live lab users. | File purged and replaced with `{}`. |
| **AUD-015** | `data/student_accounts.json` | 1–690 | 690 lines of synthetic blocked students and fake violation incidents | **CRITICAL** | Synthetic disciplinary records. | File purged and replaced with `{}`. |
| **AUD-016** | `data/browsing_history_cache.csv` | All | Stale cached browsing rows | **HIGH** | Contained previously generated synthetic browsing rows. | Purged and reset to empty header-only CSV: `TRNO,Class,group,title,url,domain,visit_time`. |
| **AUD-017** | `students/violations/*.jsonl` | 23 files | Synthetic violation audit trails | **CRITICAL** | Fake incidents from seed script. | All seeded files removed; directory reset for real append-only audit logs. |

---

## 3. Data Store Purge & Reset Actions

The following data stores have been systematically purged and reset to clean, valid structures:

1. **`data/active_sessions.json`**
   - *Previous state:* 7,667 lines of fake session states dated 2026-10-02.
   - *Purged state:* `{}` (empty JSON object).
2. **`data/student_accounts.json`**
   - *Previous state:* 690 lines of synthetic account locks.
   - *Purged state:* `{}` (empty JSON object).
3. **`data/browsing_history_cache.csv`**
   - *Previous state:* Synthetic CSV rows.
   - *Purged state:* Header-only CSV (`TRNO,Class,group,title,url,domain,visit_time`).
4. **`students/violations/*.jsonl`**
   - *Previous state:* 23 generated synthetic JSONL files (`audit_*.jsonl`, `violations_*.jsonl`).
   - *Purged state:* Directory cleared. Real violation events flow strictly through `violation_service.record_violation()`.
5. **`data/events/`**
   - *New state:* Established as the immutable event store for all system transitions (`SESSION_START`, `SESSION_END`, `HEARTBEAT`, etc.).

---

## 4. Development Mode Gating Architecture

To ensure that mock or test data can never leak into production, the following guard is enforced in all seed, diagnostic, and development tools:

```python
import os
import sys

if os.getenv("SENTINEL_DEV_MODE", "0") != "1":
    sys.stderr.write(
        "[FATAL] Sentinel Development Mode is DISABLED (SENTINEL_DEV_MODE != 1).\n"
        "Generation of mock, synthetic, or seed data is strictly blocked in production.\n"
    )
    sys.exit(1)
```

The system configuration defaults to `SENTINEL_DEV_MODE=0` in `.env`, `.env.example`, and all deployment scripts. Production servers operate exclusively on live agent telemetry.
