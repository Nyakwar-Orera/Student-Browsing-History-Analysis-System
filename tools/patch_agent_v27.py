"""
tools/patch_agent_v27.py
Patches browseguard_agent.py from v2.6 -> v2.7 with the following critical fixes:

  FIX 1: Detect SYSTEM / headless context and skip all lock_workstation() and
          show_native_alert() calls to prevent black-screen flashing on student monitors.

  FIX 2: Add Windows system process safelist to PROHIBITED_INSTALLERS wildcard check
          so trustedinstaller.exe, msiexec-system calls, etc. are never killed.

  FIX 3: lock_workstation() only called for genuine ACCOUNT_DISABLED responses,
          never for chat-site visits in SYSTEM context.

  FIX 4: Add msiexec.exe to system-process exempt list (Windows Updates use it).

Run:
    MamalLab\Scripts\python.exe tools\patch_agent_v27.py
"""
import sys, re
from pathlib import Path

AGENT = Path(r"C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System\client_agent\browseguard_agent.py")

print("Reading agent file...")
content = AGENT.read_text(encoding="utf-8", errors="replace")

# ─── PATCH 1: Bump version string ───────────────────────────────────────────
content = content.replace(
    "HARDENED & AUDIT-GRADE (v2.6)",
    "HARDENED & AUDIT-GRADE (v2.7)"
)
content = content.replace(
    "MAMALBROWSE SENTINEL CLIENT AGENT v2.6 (HARDENED)",
    "MAMALBROWSE SENTINEL CLIENT AGENT v2.7 (HARDENED)"
)

# ─── PATCH 2: Add IS_INTERACTIVE_SESSION flag right after UIAUTOMATION block ─
UIAUTOM_BLOCK = """UIAUTOMATION_AVAILABLE = False
try:
    import uiautomation as auto
    UIAUTOMATION_AVAILABLE = True
except Exception:
    pass"""

UIAUTOM_BLOCK_NEW = (
    "UIAUTOMATION_AVAILABLE = False\n"
    "try:\n"
    "    import uiautomation as auto\n"
    "    UIAUTOMATION_AVAILABLE = True\n"
    "except Exception:\n"
    "    pass\n"
    "\n"
    "# -- Session context detection --\n"
    "# When the agent runs as SYSTEM (Session 0) it cannot interact with the\n"
    "# student's interactive desktop. Calling MessageBoxW or LockWorkStation from\n"
    "# Session 0 causes a desktop switch -> student monitor goes BLACK.\n"
    "# We detect this once at startup and gate all UI / lock calls accordingly.\n"
    "def _detect_interactive() -> bool:\n"
    "    # Return True only when running in an interactive user session.\n"
    "    username = os.getenv(\"USERNAME\", \"\").strip().upper()\n"
    "    if username in (\"SYSTEM\", \"LOCAL SERVICE\", \"NETWORK SERVICE\", \"\", \"ADMINISTRATOR\", \"ADMIN\"):\n"
    "        return False\n"
    "    try:\n"
    "        res = subprocess.run(\n"
    "            [\"tasklist\", \"/v\", \"/fi\", \"imagename eq explorer.exe\", \"/fo\", \"csv\", \"/nh\"],\n"
    "            capture_output=True, text=True, timeout=3\n"
    "        )\n"
    "        if res.stdout.strip():\n"
    "            return True\n"
    "    except Exception:\n"
    "        pass\n"
    "    return False\n"
    "\n"
    "IS_INTERACTIVE_SESSION = _detect_interactive()\n"
)

if "IS_INTERACTIVE_SESSION" not in content:
    content = content.replace(UIAUTOM_BLOCK, UIAUTOM_BLOCK_NEW)
    print("PATCH 2 applied: IS_INTERACTIVE_SESSION detection added")
else:
    print("PATCH 2 already present, skipping")

# ─── PATCH 3: Guard lock_workstation() ──────────────────────────────────────
OLD_LOCK = """def lock_workstation() -> None:
    try:
        log.warning("[SECURITY] Locking workstation: %s", STATION_NAME)
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False, timeout=5)
    except Exception as e:
        log.error("Failed to lock workstation: %s", e)"""

NEW_LOCK = """def lock_workstation() -> None:
    if not IS_INTERACTIVE_SESSION:
        log.debug("[SECURITY] lock_workstation() skipped - running as SYSTEM/non-interactive (prevents black screen)")
        return
    try:
        log.warning("[SECURITY] Locking workstation: %s", STATION_NAME)
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False, timeout=5)
    except Exception as e:
        log.error("Failed to lock workstation: %s", e)"""

if 'skipped - running as SYSTEM' not in content:
    content = content.replace(OLD_LOCK, NEW_LOCK)
    print("PATCH 3 applied: lock_workstation() IS_INTERACTIVE_SESSION guard added")
else:
    print("PATCH 3 already present, skipping")

# ─── PATCH 4: Guard show_native_alert() ─────────────────────────────────────
OLD_ALERT = '''def show_native_alert(title: str, message: str, is_error: bool = False) -> None:
    """Non-blocking native alert popup in separate daemon thread."""
    def _display():
        try:
            icon_flag = 0x00000010 if is_error else 0x00000030
            flags = icon_flag | 0x00001000 | 0x00040000  # MB_SYSTEMMODAL | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(0, message, title, flags)
        except Exception:
            pass
    threading.Thread(target=_display, daemon=True).start()'''

NEW_ALERT = '''def show_native_alert(title: str, message: str, is_error: bool = False) -> None:
    """Non-blocking native alert popup in separate daemon thread.
    Skipped when running as SYSTEM to prevent desktop-switch black screen flash."""
    if not IS_INTERACTIVE_SESSION:
        log.debug("[ALERT SUPPRESSED (SYSTEM)] %s: %s", title, message[:80])
        return
    def _display():
        try:
            icon_flag = 0x00000010 if is_error else 0x00000030
            flags = icon_flag | 0x00001000 | 0x00040000  # MB_SYSTEMMODAL | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(0, message, title, flags)
        except Exception:
            pass
    threading.Thread(target=_display, daemon=True).start()'''

if 'ALERT SUPPRESSED (SYSTEM)' not in content:
    content = content.replace(OLD_ALERT, NEW_ALERT)
    print("PATCH 4 applied: show_native_alert() IS_INTERACTIVE_SESSION guard added")
else:
    print("PATCH 4 already present, skipping")

# ─── PATCH 5: Add Windows system process safelist to installer wildcard check ─
OLD_INSTALLER_CHECK = '''            elif (
                proc in PROHIBITED_INSTALLERS
                or (proc.endswith(".exe") and any(k in proc for k in ("setup", "installer", "install_")) and proc not in ("svchost.exe", "explorer.exe"))
            ):'''

NEW_INSTALLER_CHECK = '''            elif (
                proc in PROHIBITED_INSTALLERS
                or (
                    proc.endswith(".exe")
                    and any(k in proc for k in ("setup", "installer", "install_"))
                    and proc not in WINDOWS_SYSTEM_PROCESSES
                )
            ):'''

if 'WINDOWS_SYSTEM_PROCESSES' not in content:
    content = content.replace(OLD_INSTALLER_CHECK, NEW_INSTALLER_CHECK)
    print("PATCH 5a applied: installer wildcard uses WINDOWS_SYSTEM_PROCESSES safelist")
else:
    print("PATCH 5a already present, skipping")

# ─── PATCH 5b: Add WINDOWS_SYSTEM_PROCESSES set before PROHIBITED_BROWSERS ──
SAFELIST_DEF = """# Windows built-in system processes that must NEVER be killed
# These match installer-like keywords but are critical OS components
WINDOWS_SYSTEM_PROCESSES: set = {
    "trustedinstaller.exe",    # Windows Module Installer (Windows Updates)
    "msiexec.exe",             # Windows Installer service (system-initiated)
    "wusa.exe",                # Windows Update Standalone Installer
    "dism.exe",                # Deployment Image Servicing and Management
    "sfc.exe",                 # System File Checker
    "wuauclt.exe",             # Windows Update Agent
    "usoclient.exe",           # Update Session Orchestrator
    "tiworker.exe",            # Windows Module Installer Worker
    "cleanmgr.exe",            # Disk Cleanup utility
    "svchost.exe",             # Service Host process
    "explorer.exe",            # Windows Shell
    "lsass.exe",               # Local Security Authority
    "csrss.exe",               # Client/Server Runtime
    "wininit.exe",             # Windows Initialization
    "winlogon.exe",            # Windows Logon
    "services.exe",            # Service Control Manager
    "spoolsv.exe",             # Print Spooler
    "searchindexer.exe",       # Windows Search Indexer
    "searchprotocolhost.exe",  # Windows Search Protocol Host
    "onedrive.exe",            # Microsoft OneDrive sync (not setup)
    "osk.exe",                 # On-Screen Keyboard
    "narrator.exe",            # Windows Narrator (accessibility)
    "magnify.exe",             # Windows Magnifier (accessibility)
    "msedgewebview2.exe",      # Edge WebView2 (used by system apps, not browsing)
    "runtimebroker.exe",       # Runtime Broker
    "shellexperiencehost.exe", # Shell Experience Host (Start Menu / Taskbar)
    "startmenuexperiencehost.exe",  # Start Menu
    "systemsettings.exe",      # Settings App
}

"""

BROWSERS_ANCHOR = "ALLOWED_BROWSERS: Set[str] = {\"chrome.exe\"}"
if 'WINDOWS_SYSTEM_PROCESSES' not in content:
    content = content.replace(BROWSERS_ANCHOR, SAFELIST_DEF + BROWSERS_ANCHOR)
    print("PATCH 5b applied: WINDOWS_SYSTEM_PROCESSES safelist defined")
else:
    print("PATCH 5b already present, skipping")

# ─── PATCH 6: Remove msiexec.exe from PROHIBITED_INSTALLERS ─────────────────
# msiexec.exe is used by Windows for system updates — killing it is dangerous
OLD_MSI = '    "msiexec.exe",\n    "setup.exe",'
NEW_MSI = '    # "msiexec.exe",  # REMOVED: Windows uses msiexec for system updates; block via policy not process kill\n    "setup.exe",'
if '"msiexec.exe",' in content and "REMOVED: Windows uses msiexec" not in content:
    content = content.replace(OLD_MSI, NEW_MSI)
    print("PATCH 6 applied: msiexec.exe removed from PROHIBITED_INSTALLERS")
else:
    print("PATCH 6 already applied or not found, skipping")

# ─── PATCH 7: Don't lock on chat-site visit from SYSTEM (belt-and-suspenders) ─
# lock_workstation() is already guarded by IS_INTERACTIVE_SESSION in PATCH 3,
# but also gate the chat-site block path to avoid unnecessary server_post storms
OLD_CHAT_LOCK = """                lock_workstation()
                last_checked_url = url
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Rule C: Gaming Sites"""

NEW_CHAT_LOCK = """                if IS_INTERACTIVE_SESSION:
                    lock_workstation()
                last_checked_url = url
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Rule C: Gaming Sites"""

if content.count("# Rule C: Gaming Sites") == 1:
    content = content.replace(OLD_CHAT_LOCK, NEW_CHAT_LOCK)
    print("PATCH 7 applied: lock_workstation() in chat-site path guarded by IS_INTERACTIVE_SESSION")
else:
    print("PATCH 7 already applied or pattern not found, skipping")

# ─── Write the patched file ──────────────────────────────────────────────────
AGENT.write_text(content, encoding="utf-8")
print(f"\n✅  v2.7 patch written to {AGENT}")
print("   Run mass_deploy_all_labs.ps1 to push to all workstations.")
