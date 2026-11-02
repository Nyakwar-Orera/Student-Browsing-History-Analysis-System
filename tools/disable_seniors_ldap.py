"""
disable_seniors_ldap.py
=======================
Disables all Darajah 8-11 student AD accounts using LDAP3 with explicit
domain admin credentials - bypasses UAC token filtering on the DC.
Also saves credentials to data/ad_credentials.json for the Sentinel service.
"""
import sys
import json
import re
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    from ldap3 import Server, Connection, ALL, MODIFY_REPLACE, NTLM, SIMPLE
    from ldap3.core.exceptions import LDAPException
except ImportError:
    print("ERROR: ldap3 not installed. Run: MamalLab\\Scripts\\pip.exe install ldap3")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
DOMAIN       = "mamal.nbi"
DC_HOST      = "10.10.117.76"          # this server IS the DC
LDAP_USER    = "MAMAL\\admin"
LDAP_PASS    = "100%SECUREDadmin"
BASE_DIR     = Path(__file__).resolve().parent.parent
CRED_FILE    = BASE_DIR / "data" / "ad_credentials.json"
LOG_DIR      = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE     = LOG_DIR / f"senior_disable_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

UAC_NORMAL   = 512          # normal enabled account
UAC_DISABLED = 512 | 2      # 514 = normal + disabled bit

SENIOR_OUS = [
    "OU=Darajah 8,OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi",
    "OU=Darajah 9,OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi",
    "OU=Darajah 10,OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi",
    "OU=Darajah 11,OU=TALABAAT,OU=MAMAL,DC=mamal,DC=nbi",
]

def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

log("=== MamalBrowse Sentinel - Disable Senior AD Accounts (Darajah 8-11) ===")
log(f"Connecting to LDAP: {DC_HOST} as {LDAP_USER}")

# Connect
server = Server(DC_HOST, get_info=ALL)
conn = None
for auth_type, user_str in [(NTLM, LDAP_USER), (SIMPLE, f"admin@mamal.nbi")]:
    try:
        conn = Connection(server, user=user_str, password=LDAP_PASS,
                          authentication=auth_type, auto_bind=True)
        log(f"Connected via {'NTLM' if auth_type==NTLM else 'SIMPLE'}: {conn.extend.standard.who_am_i()}")
        break
    except LDAPException as e:
        log(f"Auth attempt failed ({auth_type}): {e}")
        conn = None

if not conn:
    log("LDAP connection failed with all auth methods")
    sys.exit(1)

disabled_count = 0
already_disabled = 0
not_found = 0
errors = 0

for ou in SENIOR_OUS:
    log(f"\n--- {ou} ---")
    conn.search(
        search_base=ou,
        search_filter="(&(objectCategory=person)(objectClass=user)(samaccountname=*))",
        attributes=["sAMAccountName", "userAccountControl", "cn", "distinguishedName"],
        search_scope="SUBTREE"
    )
    entries = conn.entries
    log(f"  Found {len(entries)} accounts")

    for entry in entries:
        sam = str(entry.sAMAccountName)
        dn  = str(entry.distinguishedName)

        # Only process numeric TRNOs
        if not re.match(r"^\d{4,6}$", sam):
            continue

        try:
            uac = int(entry.userAccountControl.value)
        except Exception:
            uac = 512

        is_disabled = (uac & 2) == 2

        if is_disabled:
            log(f"  ALREADY DISABLED: {sam}")
            already_disabled += 1
            continue

        # Disable: set userAccountControl = current | 2
        new_uac = uac | 2
        success = conn.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [new_uac])]})
        if success:
            log(f"  DISABLED: {sam} ({entry.cn}) UAC {uac} -> {new_uac}")
            disabled_count += 1
        else:
            log(f"  ERROR: {sam} - {conn.result['description']}")
            errors += 1

conn.unbind()

log(f"\n=== SUMMARY ===")
log(f"Disabled now    : {disabled_count}")
log(f"Already disabled: {already_disabled}")
log(f"Errors          : {errors}")
log(f"Log: {LOG_FILE}")

# Save credentials for Sentinel service to use going forward
cred_data = {"user": LDAP_USER, "password": LDAP_PASS}
CRED_FILE.write_text(json.dumps(cred_data, indent=2), encoding="utf-8")
log(f"\nCredentials saved to: {CRED_FILE}")
log("Sentinel service will now use these to enable/disable accounts automatically.")

print(f"\nDONE: {disabled_count} disabled, {already_disabled} were already off, {errors} errors")
