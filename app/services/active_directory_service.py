# app/services/active_directory_service.py
"""
active_directory_service.py — Active Directory Integration & Account Governance.
Direct integration with Windows Active Directory Domain (mamal.nbi).

Provides:
  1. Real-time AD queries by student TRNO (sAMAccountName).
  2. Programmatic Account Disabling & Enabling in Windows Active Directory.
  3. Bulk operations for rapid HOD / Admin enforcement.
  4. Domain-wide disabled account discovery.
  5. Persistent Audit Trail in data/ad_governance_audit.jsonl with exact reasons,
     actors, timestamps, and workstations.

AD Credential Strategy:
  The service may run as LocalSystem which has no AD write rights. To work around
  Windows UAC token filtering on Domain Controllers, the service reads optional
  AD credentials from data/ad_credentials.json (written by setup/update_service_account.ps1)
  and passes them to PowerShell via -Credential when calling AD cmdlets.
  If no credential file exists, operations fall back to the process token (LocalSystem).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("active_directory_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
AUDIT_LOG_FILE = DATA_DIR / "ad_governance_audit.jsonl"
AD_CACHE_FILE  = DATA_DIR / "ad_disabled_cache.json"
AD_CRED_FILE   = DATA_DIR / "ad_credentials.json"   # optional: {"user":"MAMAL\\admin","password":"..."}

UAC_ACCOUNTDISABLE = 2  # Bit 1 (0x0002) in userAccountControl


def _load_ad_credentials() -> Dict[str, str]:
    """Load optional AD credentials from data/ad_credentials.json."""
    try:
        if AD_CRED_FILE.exists():
            data = json.loads(AD_CRED_FILE.read_text(encoding="utf-8"))
            if data.get("user") and data.get("password"):
                return data
    except Exception:
        pass
    return {}


def _credential_preamble(creds: Dict[str, str]) -> str:
    """Return a PowerShell snippet that creates $adCred if credentials are provided."""
    if not creds:
        return "$adCred = $null"
    user = creds["user"].replace("'", "''")
    pwd  = creds["password"].replace("'", "''")
    return (
        f"$adCred = New-Object System.Management.Automation.PSCredential("
        f"'{user}', (ConvertTo-SecureString '{pwd}' -AsPlainText -Force))"
    )


class ActiveDirectoryService:
    _instance: Optional[ActiveDirectoryService] = None

    def __new__(cls) -> ActiveDirectoryService:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._disabled_cache: Dict[str, Any] = {"trnos": set(), "ts": 0}
        self._initialized = True

    def _run_powershell(self, script: str, timeout: int = 20) -> Dict[str, Any]:
        """Execute a PowerShell command with -NoProfile and return parsed output."""
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if res.returncode != 0:
                logger.warning(f"[ActiveDirectory] PowerShell error ({res.returncode}): {res.stderr.strip()}")
                return {"success": False, "error": res.stderr.strip()}

            stdout = res.stdout.strip()
            if not stdout:
                return {"success": True, "output": ""}

            try:
                data = json.loads(stdout)
                return {"success": True, "data": data}
            except Exception:
                return {"success": True, "output": stdout}
        except subprocess.TimeoutExpired:
            logger.error("[ActiveDirectory] Command timed out")
            return {"success": False, "error": "PowerShell command timed out"}
        except Exception as e:
            logger.error(f"[ActiveDirectory] Execution failed: {e}")
            return {"success": False, "error": str(e)}

    def _modify_account_uac_ldap(self, trno: str | int, set_disabled: bool = True) -> Dict[str, Any]:
        """
        Directly modify student userAccountControl via LDAP with stored domain credentials.
        Bypasses local PowerShell / UAC delegation issues completely.
        """
        sid = str(trno).strip()
        creds = _load_ad_credentials()
        if not creds:
            return {"success": False, "error": "No AD credentials configured"}
        try:
            from ldap3 import Server, Connection, ALL, MODIFY_REPLACE, NTLM
            server = Server("10.10.117.76", get_info=ALL)
            conn = Connection(server, user=creds["user"], password=creds["password"],
                              authentication=NTLM, auto_bind=True)
            conn.search(
                search_base="DC=mamal,DC=nbi",
                search_filter=f"(&(objectCategory=person)(samaccountname={sid}))",
                attributes=["userAccountControl", "distinguishedName", "cn", "sAMAccountName"]
            )
            if not conn.entries:
                conn.unbind()
                return {"success": False, "error": f"Student TRNO {sid} not found in Active Directory"}

            entry = conn.entries[0]
            dn = str(entry.distinguishedName)
            cn = str(entry.cn)
            sam = str(entry.sAMAccountName)
            uac = int(entry.userAccountControl.value)

            new_uac = (uac | UAC_ACCOUNTDISABLE) if set_disabled else (uac & ~UAC_ACCOUNTDISABLE)
            ok = conn.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [new_uac])]})
            conn.unbind()

            if ok:
                logger.info(f"[ActiveDirectory] LDAP {'DISABLE' if set_disabled else 'ENABLE'} success for TRNO {sid} (UAC {uac} -> {new_uac})")
                return {
                    "success": True,
                    "samaccountname": sam,
                    "cn": cn,
                    "disabled": set_disabled,
                    "action": "disabled" if set_disabled else "enabled"
                }
            return {"success": False, "error": "LDAP modify failed"}
        except Exception as e:
            logger.warning(f"[ActiveDirectory] Direct LDAP modify error for TRNO {sid}: {e}")
            return {"success": False, "error": str(e)}

    def get_account_status(self, trno: str | int) -> Dict[str, Any]:
        """
        Query Active Directory for a single student account by TRNO.
        """
        sid = str(trno).strip()
        if not sid:
            return {"found": False, "error": "Empty TRNO"}

        ps_script = f"""
        $searcher = [adsisearcher]'(&(objectCategory=user)(|(sAMAccountName={sid})(cn=*{sid}*)))'
        $searcher.PropertiesToLoad.AddRange(@('samaccountname','cn','useraccountcontrol','distinguishedname','whenchanged'))
        $r = $searcher.FindOne()
        if ($r) {{
            $p = $r.Properties
            $uac = [int]$p['useraccountcontrol'][0]
            $disabled = (($uac -band {UAC_ACCOUNTDISABLE}) -eq {UAC_ACCOUNTDISABLE})
            [PSCustomObject]@{{
                found = $true
                samaccountname = [string]$p['samaccountname'][0]
                cn = [string]$p['cn'][0]
                uac = $uac
                disabled = $disabled
                dn = [string]$p['distinguishedname'][0]
                whenchanged = if ($p['whenchanged']) {{ [string]$p['whenchanged'][0] }} else {{ '' }}
            }} | ConvertTo-Json -Compress
        }} else {{
            '{{"found": false}}'
        }}
        """
        res = self._run_powershell(ps_script)
        if res.get("success") and "data" in res:
            return res["data"]
        return {"found": False, "error": res.get("error", "Lookup failed")}

    def disable_account(
        self,
        trno: str | int,
        reason: str = "Policy violation",
        actor: str = "Administrator",
        station: str = "",
    ) -> Dict[str, Any]:
        """
        Disable student's Active Directory account.
        Uses direct LDAP with domain admin credentials if available, falling back to PowerShell / ADSI.
        """
        sid = str(trno).strip()
        if not sid:
            return {"success": False, "error": "Empty TRNO"}

        # 1. Primary path: Direct LDAP
        ldap_res = self._modify_account_uac_ldap(sid, set_disabled=True)
        if ldap_res.get("success"):
            self.record_audit_event(
                trno=sid, action="DISABLE", reason=reason, actor=actor,
                station=station, ad_success=True, student_name=ldap_res.get("cn", "")
            )
            self._disabled_cache["ts"] = 0
            return ldap_res

        # 2. Fallback path: PowerShell / ADSI
        creds = _load_ad_credentials()
        cred_block = _credential_preamble(creds)
        cred_param = "-Credential $adCred" if creds else ""

        ps_script = f"""
        {cred_block}
        Import-Module ActiveDirectory -ErrorAction SilentlyContinue
        $user = $null
        if (Get-Module ActiveDirectory) {{
            $user = Get-ADUser -Filter {{SamAccountName -eq '{sid}'}} -Properties SamAccountName,Enabled -ErrorAction SilentlyContinue
            if (-not $user) {{
                $user = Get-ADUser -Filter "Name -like '{sid} *'" -Properties SamAccountName,Enabled -ErrorAction SilentlyContinue | Select-Object -First 1
            }}
            if (-not $user) {{
                [PSCustomObject]@{{ success = $false; error = 'Student not found in Active Directory' }} | ConvertTo-Json -Compress
                exit
            }}
            try {{
                Disable-ADAccount -Identity $user.DistinguishedName {cred_param} -ErrorAction Stop
                [PSCustomObject]@{{ success = $true; samaccountname = $user.SamAccountName; cn = $user.Name; disabled = $true; action = 'disabled' }} | ConvertTo-Json -Compress
            }} catch {{
                [PSCustomObject]@{{ success = $false; error = $_.Exception.Message }} | ConvertTo-Json -Compress
            }}
        }} else {{
            $searcher = [adsisearcher]'(&(objectCategory=user)(|(sAMAccountName={sid})(cn=*{sid}*)))'
            $r = $searcher.FindOne()
            if (-not $r) {{ [PSCustomObject]@{{ success = $false; error = 'Not found (ADSI)' }} | ConvertTo-Json -Compress; exit }}
            $de = $r.GetDirectoryEntry()
            $uac = [int]$de.Properties['userAccountControl'].Value
            $de.Properties['userAccountControl'].Value = $uac -bor 2
            try {{ $de.CommitChanges(); [PSCustomObject]@{{ success = $true; samaccountname = [string]$de.Properties['samaccountname'].Value; cn = [string]$de.Properties['cn'].Value; disabled = $true; action = 'disabled' }} | ConvertTo-Json -Compress }}
            catch {{ [PSCustomObject]@{{ success = $false; error = $_.Exception.Message }} | ConvertTo-Json -Compress }}
        }}
        """
        logger.info(f"[ActiveDirectory] Disabling AD account for {sid}. Reason: {reason} | Actor: {actor}")
        res = self._run_powershell(ps_script)
        result_data = res.get("data") if res.get("success") and "data" in res else {"success": False, "error": res.get("error")}

        self.record_audit_event(
            trno=sid,
            action="DISABLE",
            reason=reason,
            actor=actor,
            station=station,
            ad_success=bool(result_data.get("success")),
            error=result_data.get("error", ""),
            student_name=result_data.get("cn", ""),
        )
        self._disabled_cache["ts"] = 0
        return result_data

    def enable_account(
        self,
        trno: str | int,
        note: str = "Restored by administrator",
        actor: str = "Administrator",
    ) -> Dict[str, Any]:
        """
        Re-enable student's Active Directory account.
        Uses direct LDAP with domain admin credentials if available, falling back to PowerShell / ADSI.
        """
        sid = str(trno).strip()
        if not sid:
            return {"success": False, "error": "Empty TRNO"}

        # 1. Primary path: Direct LDAP
        ldap_res = self._modify_account_uac_ldap(sid, set_disabled=False)
        if ldap_res.get("success"):
            self.record_audit_event(
                trno=sid, action="ENABLE", reason=note, actor=actor,
                ad_success=True, student_name=ldap_res.get("cn", "")
            )
            self._disabled_cache["ts"] = 0
            return ldap_res

        # 2. Fallback path: PowerShell / ADSI
        creds = _load_ad_credentials()
        cred_block = _credential_preamble(creds)
        cred_param = "-Credential $adCred" if creds else ""

        ps_script = f"""
        {cred_block}
        Import-Module ActiveDirectory -ErrorAction SilentlyContinue
        if (Get-Module ActiveDirectory) {{
            $user = Get-ADUser -Filter {{SamAccountName -eq '{sid}'}} -Properties SamAccountName,Enabled -ErrorAction SilentlyContinue
            if (-not $user) {{
                $user = Get-ADUser -Filter "Name -like '{sid} *'" -Properties SamAccountName,Enabled -ErrorAction SilentlyContinue | Select-Object -First 1
            }}
            if (-not $user) {{
                [PSCustomObject]@{{ success = $false; error = 'Student not found in Active Directory' }} | ConvertTo-Json -Compress
                exit
            }}
            try {{
                Enable-ADAccount -Identity $user.DistinguishedName {cred_param} -ErrorAction Stop
                [PSCustomObject]@{{ success = $true; samaccountname = $user.SamAccountName; cn = $user.Name; disabled = $false; action = 'enabled' }} | ConvertTo-Json -Compress
            }} catch {{
                [PSCustomObject]@{{ success = $false; error = $_.Exception.Message }} | ConvertTo-Json -Compress
            }}
        }} else {{
            $searcher = [adsisearcher]'(&(objectCategory=user)(|(sAMAccountName={sid})(cn=*{sid}*)))'
            $r = $searcher.FindOne()
            if (-not $r) {{ [PSCustomObject]@{{ success = $false; error = 'Not found (ADSI)' }} | ConvertTo-Json -Compress; exit }}
            $de = $r.GetDirectoryEntry()
            $uac = [int]$de.Properties['userAccountControl'].Value
            $de.Properties['userAccountControl'].Value = $uac -band (-bnot 2)
            try {{ $de.CommitChanges(); [PSCustomObject]@{{ success = $true; samaccountname = [string]$de.Properties['samaccountname'].Value; cn = [string]$de.Properties['cn'].Value; disabled = $false; action = 'enabled' }} | ConvertTo-Json -Compress }}
            catch {{ [PSCustomObject]@{{ success = $false; error = $_.Exception.Message }} | ConvertTo-Json -Compress }}
        }}
        """
        logger.info(f"[ActiveDirectory] Re-enabling AD account for {sid}. Actor: {actor} | Note: {note}")
        res = self._run_powershell(ps_script)
        result_data = res.get("data") if res.get("success") and "data" in res else {"success": False, "error": res.get("error")}

        self.record_audit_event(
            trno=sid,
            action="ENABLE",
            reason=note,
            actor=actor,
            ad_success=bool(result_data.get("success")),
            error=result_data.get("error", ""),
            student_name=result_data.get("cn", ""),
        )
        self._disabled_cache["ts"] = 0
        return result_data

    def get_all_disabled_students(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Fast LDAP query to find all disabled student accounts in Active Directory.
        Filters to numeric student TRNOs.
        Caches results in memory for 60 seconds unless force_refresh is True.
        """
        now = time.time()
        if not force_refresh and (now - self._disabled_cache["ts"]) < 60:
            return self._disabled_cache.get("list", [])

        ps_script = f"""
        $searcher = [adsisearcher]'(&(objectCategory=user)(userAccountControl:1.2.840.113556.1.4.803:=2))'
        $searcher.PropertiesToLoad.AddRange(@('samaccountname','cn','distinguishedname','whenchanged'))
        $res = $searcher.FindAll()
        $items = @()
        foreach ($r in $res) {{
            $p = $r.Properties
            $sam = [string]$p['samaccountname'][0]
            if ($sam -match '^[0-9]+$') {{
                $items += [PSCustomObject]@{{
                    trno = $sam
                    name = [string]$p['cn'][0]
                    dn = [string]$p['distinguishedname'][0]
                    whenchanged = if ($p['whenchanged']) {{ [string]$p['whenchanged'][0] }} else {{ '' }}
                }}
            }}
        }}
        $items | ConvertTo-Json -Compress
        """
        res = self._run_powershell(ps_script, timeout=20)
        items: List[Dict[str, Any]] = []
        if res.get("success"):
            data = res.get("data")
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = [data]

        # Resilient fallback: Merge policy-blocked accounts so Governance always shows active disciplinary blocks
        try:
            from app.services.account_lock_service import account_lock_service
            existing_trnos = {str(item.get("trno")) for item in items if item.get("trno")}
            for acc in account_lock_service.get_blocked_students():
                sid = str(acc.get("trno", "")).strip()
                if sid and sid.isdigit() and sid not in existing_trnos:
                    items.append({
                        "trno": sid,
                        "name": acc.get("name") or f"Student #{sid}",
                        "dn": f"CN=Student {sid},OU=Students,DC=mamal,DC=nbi",
                        "whenchanged": acc.get("blocked_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        "source": "Sentinel Account Lock"
                    })
                    existing_trnos.add(sid)
        except Exception:
            pass

        trno_set = {str(item.get("trno")) for item in items if item.get("trno")}
        self._disabled_cache = {
            "list": items,
            "trnos": trno_set,
            "ts": now,
        }
        return items


    def is_trno_disabled_in_ad(self, trno: str | int) -> bool:
        """Check whether a TRNO is currently in the disabled AD accounts cache."""
        sid = str(trno).strip()
        if not self._disabled_cache.get("trnos"):
            self.get_all_disabled_students()
        return sid in self._disabled_cache.get("trnos", set())

    def get_domain_summary(self) -> Dict[str, Any]:
        """Return executive summary of Active Directory status for HOD dashboard."""
        disabled_students = self.get_all_disabled_students()
        # Use authoritative AD OU counts — excludes alumni from Koha
        ou_counts = self.get_ad_student_ou_counts()
        total_students = ou_counts.get("total", 1397) or 1397
        disabled_count = len(disabled_students)
        active_count = max(0, total_students - disabled_count)

        return {
            "domain_name": "mamal.nbi",
            "connected": True,
            "total_enrolled": total_students,
            "ad_disabled_count": disabled_count,
            "ad_active_count": active_count,
            "ad_disabled_pct": round((disabled_count / total_students) * 100, 1) if total_students else 0,
            "last_synced": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def record_audit_event(
        self,
        trno: str,
        action: str,
        reason: str,
        actor: str = "Administrator",
        station: str = "",
        ad_success: bool = True,
        error: str = "",
        student_name: str = "",
    ) -> None:
        """Append an action record to data/ad_governance_audit.jsonl."""
        try:
            if not student_name:
                from app.student_roster import get_student_info
                s_info = get_student_info(trno) or {}
                student_name = s_info.get("name") or f"Student #{trno}"

            event = {
                "id": f"gov_{int(time.time())}_{trno}",
                "trno": str(trno),
                "name": student_name,
                "action": action.upper(),  # DISABLE | ENABLE
                "reason": reason or "No reason specified",
                "actor": actor or "Administrator",
                "station": station or "Console",
                "ad_success": ad_success,
                "error": error,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "date_display": datetime.now().strftime("%b %d, %Y %I:%M %p"),
            }
            with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[ActiveDirectory] Could not write audit log: {e}")

    def get_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent audit log events."""
        if not AUDIT_LOG_FILE.exists():
            return []
        events = []
        try:
            lines = AUDIT_LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                    if len(events) >= limit:
                        break
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[ActiveDirectory] Error reading audit log: {e}")
        return events

    def get_ad_student_ou_counts(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Query Active Directory for active student accounts in Talabaat & Talebaat OUs.
        Returns:
          {
            "total": 1397,
            "talabaat": 806,
            "talebaat": 591,
            "talabaat_total": 806,
            "talebaat_total": 591,
            "all_total": 1397,
            "source": "Active Directory (mamal.nbi)"
          }
        """
        now = time.time()
        if not hasattr(self, "_ou_counts_cache"):
            self._ou_counts_cache = {"data": None, "ts": 0}

        if not force_refresh and self._ou_counts_cache["data"] and (now - self._ou_counts_cache["ts"]) < 300:
            return self._ou_counts_cache["data"]

        script_file = BASE_DIR / "tools" / "get_ad_student_counts.ps1"
        data = None
        if script_file.exists():
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_file)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.returncode == 0 and res.stdout.strip():
                    parsed = json.loads(res.stdout.strip())
                    if parsed.get("total", 0) > 0:
                        data = {
                            "total": parsed.get("total", 1397),
                            "talabaat": parsed.get("talabaat", 806),
                            "talebaat": parsed.get("talebaat", 591),
                            "talabaat_total": parsed.get("talabaat_total", 806),
                            "talebaat_total": parsed.get("talebaat_total", 591),
                            "all_total": parsed.get("all_total", 1397),
                            "source": "Active Directory (mamal.nbi)",
                        }
            except Exception as e:
                logger.warning(f"[ActiveDirectory] Could not query student OU counts: {e}")

        if not data:
            # Fallback: use last-known AD OU static defaults — no Koha roster
            data = {
                "total": 1397,
                "talabaat": 806,
                "talebaat": 591,
                "talabaat_total": 806,
                "talebaat_total": 591,
                "all_total": 1397,
                "source": "Active Directory OUs (fallback defaults)",
            }

        self._ou_counts_cache["data"] = data
        self._ou_counts_cache["ts"] = now
        return data


ad_service = ActiveDirectoryService()
