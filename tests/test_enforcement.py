# tests/test_enforcement.py
"""
Test suite for MamalBrowse Sentinel Website Blocking, Warning & Student Account Suspension System.
Tests:
  - AccountLockService persistence, violation tracking, auto-locking, and unblocking
  - PolicyEngine rule matching, time windows, and student account integration
  - Flask endpoints for real-time check, blocked UI, and admin management
"""
import os
import sys
import unittest
import json
from pathlib import Path

# Add project root and app to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "app"))

from app.services.account_lock_service import account_lock_service, AccountLockService
from app.services.policy_engine import policy_engine, PolicyEngine
from app.main import app

TEST_TRNOS = ["99001", "99002", "99003", "99004", "99005", "99006", "99007"]


class TestEnforcementSystem(unittest.TestCase):
    def setUp(self):
        self.account_service = account_lock_service
        self.policy_engine = policy_engine
        for tr in TEST_TRNOS:
            account_lock_service.unblock_account(tr)
        self.client = app.test_client()
        app.config["TESTING"] = True

    def tearDown(self):
        for tr in TEST_TRNOS:
            account_lock_service.unblock_account(tr)

    def test_account_lock_service_lifecycle(self):
        trno = "99001"
        # 1. Initial state is active
        acc = self.account_service.get_account(trno)
        self.assertEqual(acc["status"], "active")
        self.assertEqual(acc["warning_count"], 0)
        self.assertFalse(self.account_service.is_account_blocked(trno))

        # 2. Record infraction on auto-block category (e.g. instagram.com)
        updated = self.account_service.record_violation(
            trno=trno,
            url="https://www.instagram.com/p/123",
            host="instagram.com",
            category="social_media",
            action="block",
            station="PC-01",
            auto_block_category=True,
            max_warnings=1,
        )
        self.assertEqual(updated["status"], "blocked")
        self.assertEqual(updated["warning_count"], 1)
        self.assertTrue(self.account_service.is_account_blocked(trno))

        # 3. Unblock account
        success = self.account_service.unblock_account(trno, admin_user="TeacherTest", note="Resolved")
        self.assertTrue(success)
        restored = self.account_service.get_account(trno)
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["warning_count"], 0)
        self.assertFalse(self.account_service.is_account_blocked(trno))

    def test_policy_engine_evaluates_instagram(self):
        trno = "99002"
        # Evaluate Instagram navigation
        decision = self.policy_engine.evaluate(
            url="https://www.instagram.com",
            trno=trno,
            darajah="3",
            station="PC-05"
        )
        self.assertEqual(decision["action"], "block")
        self.assertEqual(decision["category"], "social_media")
        self.assertIn("instagram.com", decision["host"])
        self.assertEqual(decision["account_status"], "blocked")

        # Now student account is blocked — any subsequent URL should be blocked
        subsequent = self.policy_engine.evaluate(
            url="https://en.wikipedia.org/wiki/Science",
            trno=trno,
            darajah="3",
            station="PC-05"
        )
        self.assertEqual(subsequent["action"], "block")
        self.assertEqual(subsequent["account_status"], "blocked")
        self.assertEqual(subsequent["category"], "account_suspended")

    def test_policy_engine_allows_educational(self):
        trno = "99003"
        decision = self.policy_engine.evaluate(
            url="https://www.khanacademy.org/math",
            trno=trno,
            darajah="4",
            station="PC-08"
        )
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(decision["account_status"], "active")

    def test_api_check_endpoint(self):
        resp = self.client.post("/enforce/check", json={
            "url": "https://instagram.com/explore",
            "trno": "99004",
            "station": "PC-12",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["action"], "block")
        self.assertEqual(data["category"], "social_media")
        self.assertEqual(data["account_status"], "blocked")

    def test_blocked_template_renders(self):
        resp = self.client.get("/enforce/blocked?url=https://instagram.com&trno=99004&station=PC-12&status=block")
        self.assertEqual(resp.status_code, 200)
        content = resp.get_data(as_text=True)
        self.assertIn("Account Locked", content)
        self.assertIn("99004", content)
        self.assertIn("Request Teacher Override", content)

    def test_request_override_endpoint(self):
        resp = self.client.post("/enforce/request-override", json={
            "trno": "99004",
            "url": "https://instagram.com",
            "reason": "Needed to view school official page for journalism assignment.",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))

    def test_admin_blocked_students_and_unblock(self):
        # Simulate admin session
        with self.client.session_transaction() as sess:
            sess["logged_in"] = True

        # First trigger block on student 99005
        self.client.post("/enforce/check", json={
            "url": "https://instagram.com",
            "trno": "99005",
            "station": "PC-15",
        })

        # Check admin blocked list
        resp = self.client.get("/enforce/admin/blocked-students")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        blocked_trnos = [s["trno"] for s in data.get("blocked", [])]
        self.assertIn("99005", blocked_trnos)

        # Unblock student 99005
        unblock_resp = self.client.post("/enforce/admin/unblock", json={
            "trno": "99005",
            "note": "Counseling complete"
        })
        self.assertEqual(unblock_resp.status_code, 200)
        self.assertTrue(unblock_resp.get_json().get("ok"))

        # Verify no longer in blocked list
        verify_resp = self.client.get("/enforce/admin/blocked-students")
        verify_blocked_trnos = [s["trno"] for s in verify_resp.get_json().get("blocked", [])]
        self.assertNotIn("99005", verify_blocked_trnos)

    def test_admin_quick_block(self):
        with self.client.session_transaction() as sess:
            sess["logged_in"] = True

        resp = self.client.post("/enforce/admin/quick-block", json={
            "domain": "test-unwanted-site.org"
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("ok"))

        # Verify it is now blocked
        check_resp = self.client.post("/enforce/check", json={
            "url": "https://test-unwanted-site.org/page",
            "trno": "99006"
        })
        self.assertEqual(check_resp.get_json().get("action"), "block")

    def test_integration_service_reflects_blocked_status(self):
        from app.services.integration_service import integration_service
        trno = "99007"
        # Block student
        from app.services.account_lock_service import account_lock_service
        account_lock_service.block_account_manually(trno, reason="Instagram policy violation")

        profile = integration_service.get_student_combined_profile(trno)
        self.assertTrue(profile.get("is_blocked"))
        self.assertEqual(profile.get("account_status"), "blocked")
        self.assertEqual(profile.get("risk_level"), "high")


if __name__ == "__main__":
    unittest.main()
