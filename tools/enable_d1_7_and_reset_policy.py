"""
tools/enable_d1_7_and_reset_policy.py
======================================
1. Re-enables all Darajah 1-7 student accounts in Active Directory via LDAP.
2. Resets their status in data/student_accounts.json:
   - status: active
   - warning_count: 0
   - violations: []
   - blocked_reason: ""
   - blocked_at: None
   - last_incident: None
   - enabled_since: "2026-10-31T00:00:00"
3. Preserves permanent lockout for Darajah 8-11 seniors (Senior Laptop Directive).
4. Clears all violation and audit logs in students/violations/ and students/audit/.
"""
import sys
import os
import json
import re
import glob
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

ACCOUNTS_FILE = BASE_DIR / "data" / "student_accounts.json"
CRED_FILE = BASE_DIR / "data" / "ad_credentials.json"
VIOLATIONS_DIR = BASE_DIR / "students" / "violations"
AUDIT_DIR = BASE_DIR / "students" / "audit"
GOVERNANCE_AUDIT = BASE_DIR / "data" / "ad_governance_audit.jsonl"

NOW_ISO = datetime.now().isoformat(timespec="seconds")
EFFECTIVE_POLICY_DATE = "2026-10-31T00:00:00"
ADMIN_NOTE = "Administrator — Policy Reset & Bulk Enable (Effective 31 Oct 2026)"

def is_senior(darajah: str) -> bool:
    return bool(re.search(r"\b(8|9|10|11)\b", str(darajah or "")))

def main():
    print("=" * 60)
    print("MamalBrowse Sentinel — Bulk Enable & Policy Reset (Darajah 1-7)")
    print(f"Timestamp: {NOW_ISO} | Effective: {EFFECTIVE_POLICY_DATE}")
    print("=" * 60)

    # 1. Connect to Active Directory via LDAP
    ad_conn = None
    if CRED_FILE.exists():
        try:
            from ldap3 import Server, Connection, ALL, MODIFY_REPLACE, NTLM
            creds = json.loads(CRED_FILE.read_text(encoding="utf-8"))
            server = Server("10.10.117.76", get_info=ALL)
            ad_conn = Connection(server, user=creds["user"], password=creds["password"],
                                 authentication=NTLM, auto_bind=True)
            print(f"[AD] Connected to LDAP as {creds['user']}")
        except Exception as e:
            print(f"[AD] Warning: LDAP connection failed: {e}")
    else:
        print("[AD] No ad_credentials.json found, skipping direct AD unlock.")

    # 2. Load student accounts
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    print(f"[Accounts] Loaded {len(accounts)} student accounts.")

    enabled_sentinel = 0
    ad_enabled_count = 0
    skipped_seniors = 0
    already_active = 0

    for trno, acc in accounts.items():
        dar = str(acc.get("darajah") or "")
        status = acc.get("status", "active")

        # Senior students Darajah 8-11: MUST REMAIN BLOCKED
        if is_senior(dar):
            skipped_seniors += 1
            continue

        was_blocked = (status == "blocked") or (acc.get("warning_count", 0) > 0)

        # Reset account state for Darajah 1-7
        acc["status"] = "active"
        acc["warning_count"] = 0
        acc["violations"] = []
        acc["blocked_reason"] = ""
        acc["blocked_at"] = None
        acc["last_incident"] = None
        acc["unblocked_at"] = NOW_ISO
        acc["unblocked_by"] = ADMIN_NOTE
        acc["enabled_since"] = EFFECTIVE_POLICY_DATE  # Effective from 31st October 2026

        accounts[trno] = acc
        if was_blocked:
            enabled_sentinel += 1

        # Re-enable in Active Directory if LDAP connection is active
        if ad_conn:
            try:
                ad_conn.search(
                    search_base="DC=mamal,DC=nbi",
                    search_filter=f"(&(objectCategory=person)(samaccountname={trno}))",
                    attributes=["userAccountControl", "distinguishedName"]
                )
                if ad_conn.entries:
                    entry = ad_conn.entries[0]
                    dn = str(entry.distinguishedName)
                    uac = int(entry.userAccountControl.value)
                    if (uac & 2) == 2:  # disabled
                        new_uac = uac & ~2  # clear disable bit
                        if ad_conn.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [new_uac])]}):
                            ad_enabled_count += 1
                            print(f"  [AD ENABLED] TRNO {trno} (UAC {uac} -> {new_uac})")
            except Exception as e:
                print(f"  [AD ERROR] TRNO {trno}: {e}")

    if ad_conn:
        ad_conn.unbind()

    # Save updated accounts
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n[Accounts] Updated {ACCOUNTS_FILE}")
    print(f"  - Darajah 1-7 Re-enabled: {enabled_sentinel}")
    print(f"  - AD Accounts Unlocked: {ad_enabled_count}")
    print(f"  - Seniors Preserved (Locked): {skipped_seniors}")

    # 3. Clear all violation log files
    print("\n[Logs] Clearing violation logs...")
    patterns = [
        str(VIOLATIONS_DIR / "*.jsonl"),
        str(VIOLATIONS_DIR / "archive" / "*.jsonl"),
        str(AUDIT_DIR / "*.jsonl"),
    ]
    cleared = 0
    for pat in patterns:
        for fpath in glob.glob(pat):
            try:
                os.remove(fpath)
                cleared += 1
            except Exception as e:
                print(f"  Warning: could not remove {fpath}: {e}")

    # Reset governance audit file
    if GOVERNANCE_AUDIT.exists():
        with open(GOVERNANCE_AUDIT, "w", encoding="utf-8") as f:
            rec = {
                "ts": NOW_ISO,
                "action": "POLICY_RESET_BULK_ENABLE",
                "actor": ADMIN_NOTE,
                "enabled_count": enabled_sentinel,
                "ad_enabled_count": ad_enabled_count,
                "effective_date": EFFECTIVE_POLICY_DATE,
                "note": "Re-enabled Darajah 1-7. Cleared violations, reset strikes. Seniors 8-11 remain locked."
            }
            f.write(json.dumps(rec) + "\n")

    print(f"  - Removed {cleared} historical violation log files.")
    print("=" * 60)
    print("COMPLETED SUCCESSFULLY.")
    print("=" * 60)

if __name__ == "__main__":
    main()

