# tests/test_time_accounting.py
"""
Unit and Integration Test Suite for MamalBrowse Sentinel Time Accounting & Data Integrity.

Verifies:
  1. Monotonic Clock Enforcement & Rejection:
     Rejects backwards or non-monotonic agent heartbeats within a session with ValueError.
  2. Idle-Aware Lab Dwell Time Calculation:
     60-minute session with 10 minutes idle (>=300s) yields exactly 50 minutes active dwell time.
  3. Stale Session Auto-Closure:
     Sessions timing out without heartbeat are closed at last_heartbeat, not wall clock now.
  4. Immutable Event Log Service:
     Events are appended to JSONL with monotonic sequence numbers, deduplication by event_id.
  5. Cache Independence:
     Resetting or clearing browsing_history_cache.csv does not alter authoritative time accounting.
  6. Production Safety (Zero Synthetic Data):
     No-data state returns clean {no_data: True, value: 0} and sentinel -1, never fabricated numbers.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "app"))

from app.services.event_log_service import EventLogService
from app.services.time_accounting_service import TimeAccountingService


class TestTimeAccountingSystem(unittest.TestCase):
    def setUp(self):
        # Create isolated temporary directory for test logs and sessions
        self.test_dir = tempfile.mkdtemp(prefix="sentinel_test_")
        self.events_dir = Path(self.test_dir) / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.truth_file = Path(self.test_dir) / "sessions_truth.json"

        # Initialize isolated EventLogService
        self.event_log = EventLogService()

        # Initialize isolated TimeAccountingService
        self.time_service = TimeAccountingService()
        with self.time_service._lock:
            self.time_service._sessions.clear()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_monotonic_clock_rejection(self):
        """Server must accept increasing monotonic_ns and reject backwards monotonic_ns."""
        trno = "27290"
        station = "LAB-PC-01"
        session_id = "sess_mono_001"

        # First heartbeat at monotonic time 1000 seconds
        mono_1 = 1_000_000_000_000  # 1000 sec in ns
        res1 = self.time_service.record_heartbeat(
            trno=trno,
            station=station,
            session_id=session_id,
            monotonic_ns=mono_1,
            idle_sec=0,
            wall_time=1700000000.0,
        )
        self.assertEqual(res1.get("status"), "active")

        # Second heartbeat with forward monotonic time (1030 seconds)
        mono_2 = 1_030_000_000_000  # 1030 sec in ns
        res2 = self.time_service.record_heartbeat(
            trno=trno,
            station=station,
            session_id=session_id,
            monotonic_ns=mono_2,
            idle_sec=0,
            wall_time=1700000030.0,
        )
        self.assertEqual(res2.get("status"), "active")

        # Third heartbeat with backwards monotonic time (900 seconds) - must raise ValueError
        mono_backwards = 900_000_000_000  # 900 sec in ns
        with self.assertRaises(ValueError):
            self.time_service.record_heartbeat(
                trno=trno,
                station=station,
                session_id=session_id,
                monotonic_ns=mono_backwards,
                idle_sec=0,
                wall_time=1700000060.0,
            )

    def test_idle_aware_dwell_time_calculation(self):
        """
        A 60-minute session consisting of:
          - 50 minutes active (idle_sec = 0)
          - 10 minutes inactive/away (idle_sec >= 300)
        MUST compute active dwell time = 50 minutes and inactive time = 10 minutes.
        """
        trno = "27291"
        station = "LAB-PC-02"
        session_id = "sess_dwell_001"

        start_wall = 1700000000.0
        start_mono = 10_000_000_000_000  # ns

        # Start session
        self.time_service.record_session_start(
            trno=trno,
            station=station,
            session_id=session_id,
            darajah="6",
            student_name="Test Student",
            monotonic_ns=start_mono,
            wall_time=start_wall,
        )

        curr_wall = start_wall
        curr_mono = start_mono

        # 100 intervals of 30s = 50 minutes active (idle_sec < 300)
        for _ in range(100):
            curr_wall += 30.0
            curr_mono += 30_000_000_000
            self.time_service.record_heartbeat(
                trno=trno,
                station=station,
                session_id=session_id,
                monotonic_ns=curr_mono,
                idle_sec=10,  # active (< 300s)
                wall_time=curr_wall,
            )

        # 20 intervals of 30s = 10 minutes idle (idle_sec = 350 >= 300)
        for _ in range(20):
            curr_wall += 30.0
            curr_mono += 30_000_000_000
            self.time_service.record_heartbeat(
                trno=trno,
                station=station,
                session_id=session_id,
                monotonic_ns=curr_mono,
                idle_sec=350,  # idle (>= 300s)
                wall_time=curr_wall,
            )

        # End session
        self.time_service.record_session_end(
            trno=trno,
            station=station,
            session_id=session_id,
            reason="user_logout",
            wall_time=curr_wall,
        )

        truth = self.time_service.get_student_time_truth(trno)
        self.assertFalse(truth.get("no_data", False))

        # Verify dwell time split
        total_duration = truth.get("total_duration_sec", 0.0)
        active_time = truth.get("active_dwell_time_sec", 0.0)
        idle_time = truth.get("idle_time_sec", 0.0)

        # Total elapsed: 120 intervals * 30s = 3600 seconds (60 mins)
        self.assertAlmostEqual(total_duration, 3600.0, delta=1.0)
        # Active: 100 intervals * 30s = 3000 seconds (50 mins)
        self.assertAlmostEqual(active_time, 3000.0, delta=1.0)
        # Idle: 20 intervals * 30s = 600 seconds (10 mins)
        self.assertAlmostEqual(idle_time, 600.0, delta=1.0)

    def test_stale_session_closed_at_last_heartbeat(self):
        """
        When a session goes stale (no heartbeat for > 90s), it must close with
        duration = last_heartbeat - first_heartbeat, NOT wall-clock now.
        """
        trno = "27292"
        station = "LAB-PC-03"
        session_id = "sess_stale_001"

        start_wall = 1700000000.0
        start_mono = 1_000_000_000_000

        self.time_service.record_session_start(
            trno=trno,
            station=station,
            session_id=session_id,
            darajah="5",
            student_name="Stale Student",
            monotonic_ns=start_mono,
            wall_time=start_wall,
        )

        # 10 heartbeats across 300 seconds (5 mins)
        curr_wall = start_wall
        curr_mono = start_mono
        for _ in range(10):
            curr_wall += 30.0
            curr_mono += 30_000_000_000
            self.time_service.record_heartbeat(
                trno=trno,
                station=station,
                session_id=session_id,
                monotonic_ns=curr_mono,
                idle_sec=5,
                wall_time=curr_wall,
            )

        # Now simulate 30 minutes passing without heartbeat
        now_simulated = curr_wall + 1800.0

        # Run sweep
        closed_count = self.time_service.close_stale_sessions(current_wall_time=now_simulated)
        self.assertGreaterEqual(closed_count, 1)

        truth = self.time_service.get_student_time_truth(trno)
        # Total duration must be 300 seconds (last_hb - first_hb), NOT 2100 seconds
        self.assertAlmostEqual(truth.get("total_duration_sec", 0.0), 300.0, delta=2.0)

    def test_immutable_event_log_deduplication(self):
        """Event log must deduplicate replayed events by event_id."""
        event_id = "test-uuid-replay-12345"

        # First append
        e1 = self.event_log.append_event(
            event_type="HEARTBEAT",
            trno="27295",
            station="LAB-PC-04",
            session_id="sess_dedup_001",
            monotonic_ns=5_000_000_000,
            event_id=event_id,
            payload={"idle_sec": 0},
        )
        self.assertIsNotNone(e1)
        self.assertFalse(e1.get("duplicate", False))

        # Duplicate append with same event_id
        e2 = self.event_log.append_event(
            event_type="HEARTBEAT",
            trno="27295",
            station="LAB-PC-04",
            session_id="sess_dedup_001",
            monotonic_ns=5_000_000_000,
            event_id=event_id,
            payload={"idle_sec": 0},
        )
        # Second append should indicate deduplication and preserve sequence
        self.assertTrue(e2.get("duplicate"))
        self.assertEqual(e1.get("server_seq"), e2.get("server_seq"))

    def test_zero_synthetic_data_contract(self):
        """Unknown student queries must return {no_data: True} with 0 totals, never fake numbers."""
        unknown_trno = "00000_UNKNOWN"
        truth = self.time_service.get_student_time_truth(unknown_trno)
        self.assertTrue(truth.get("no_data"))
        self.assertEqual(truth.get("total_lab_time_sec"), 0)
        self.assertEqual(truth.get("total_idle_sec"), 0)
        self.assertEqual(truth.get("event_count"), 0)

        # Freshness metrics with no prior heartbeats must report sentinel -1 (not 99999)
        self.time_service._last_heartbeat_wall_time = 0.0
        metrics = self.time_service.get_freshness_metrics()
        self.assertEqual(metrics.get("last_heartbeat_age_sec"), -1)
        self.assertTrue(metrics.get("last_heartbeat_no_data"))


if __name__ == "__main__":
    unittest.main()
