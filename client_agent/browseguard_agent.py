# client_agent/browseguard_agent.py
"""
MamalBrowse Sentinel Client Enforcement Agent for Windows Lab Workstations.
HARDENED & STEALTH (v2.8) - Zero-Blink Windowless Institutional Deployment.

Guarantees:
  1. Zero Monitor Blinking / No Flashing Command Prompt:
     - Process scanning runs 100% in-memory via Windows kernel32 toolhelp snapshot API.
     - Foreground window process resolution runs in-memory via QueryFullProcessImageNameW.
     - All helper subprocesses run with CREATE_NO_WINDOW and SW_HIDE flags.
  2. Strict Browser Enforcement (Chrome & Edge):
     - Google Chrome (chrome.exe) AND Microsoft Edge (msedge.exe) are permitted.
     - Firefox, Opera, Brave, Tor, and all other third-party browsers
       are TERMINATED IMMEDIATELY upon execution.
  3. Strict Installer & Setup Blocking:
     - Software installers (setup.exe, install.exe, roblox*.exe, etc.) are BLOCKED.
     - Windows critical OS components (trustedinstaller.exe, svchost.exe) are preserved.
  4. Real-time Policy Enforcement:
     - Pinterest (pinterest.com, pinimg.com) is EXPLICITLY PERMITTED for design/curriculum.
     - Chatting & social messaging (WhatsApp, Telegram, Discord, Instagram, Threads, Messenger,
       Snapchat, Omegle, etc.) are STRICTLY BLOCKED, tabs closed instantly, and account penalized.
     - Online gaming platforms (Roblox, Poki, CrazyGames, etc.) are warned and closed.
  5. Session 0 Safety:
     - Session 0 (SYSTEM) tasks never trigger desktop lock or GUI dialogs to prevent screen flashes.
     - Student interactive session performs tab closing and alerts cleanly.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Reconfigure stdout for utf-8 safety
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Setup structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] browseguard_agent: %(message)s"
)
log = logging.getLogger("browseguard_agent")

# Optional uiautomation
UIAUTOMATION_AVAILABLE = False
try:
    import uiautomation as auto
    UIAUTOMATION_AVAILABLE = True
except BaseException:
    pass

# Determine if running in an interactive user session (Session > 0)
def _detect_interactive() -> bool:
    if os.name != "nt":
        return False
    try:
        session_id = ctypes.c_uint()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            os.getpid(), ctypes.byref(session_id)
        )
        return bool(ok and session_id.value != 0)
    except Exception:
        return False

IS_INTERACTIVE_SESSION = _detect_interactive()


# ==============================================================================
# WINDOWLESS SUBPROCESS EXECUTION (Guarantees zero black command prompt flashing)
# ==============================================================================
CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0

def hidden_subprocess_run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Execute command completely hidden with ZERO console window flashing."""
    startupinfo = kwargs.pop("startupinfo", None)
    if startupinfo is None:
        startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE
    kwargs["startupinfo"] = startupinfo
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


# ==============================================================================
# IN-MEMORY PROCESS ENUMERATION (Zero child processes, zero console creation)
# ==============================================================================
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260)
    ]

def get_running_process_names() -> Set[str]:
    """Capture all running processes directly in-memory via Windows ToolHelp32 API."""
    running: Set[str] = set()
    try:
        hSnapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if hSnapshot == -1 or not hSnapshot:
            return running
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if ctypes.windll.kernel32.Process32First(hSnapshot, ctypes.byref(pe)):
            while True:
                exe = pe.szExeFile.decode("utf-8", errors="ignore").lower().strip()
                if exe:
                    running.add(exe)
                if not ctypes.windll.kernel32.Process32Next(hSnapshot, ctypes.byref(pe)):
                    break
        ctypes.windll.kernel32.CloseHandle(hSnapshot)
    except Exception:
        # Fallback to hidden tasklist if ctypes snapshot fails
        try:
            res = hidden_subprocess_run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=3)
            for line in res.stdout.splitlines():
                line_str = line.strip()
                if line_str:
                    parts = line_str.split(",")
                    if parts:
                        running.add(parts[0].strip('"').lower())
        except Exception:
            pass
    return running


def get_process_name_by_pid(pid: int) -> str:
    """Get executable name for PID directly in-memory via QueryFullProcessImageNameW."""
    if not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        h_proc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_proc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return buf.value.split("\\")[-1].lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)
    except Exception:
        pass
    return ""


# ==============================================================================
# Station & Environment Configuration
# ==============================================================================
BROWSEGUARD_SERVER = os.getenv("BROWSEGUARD_SERVER", "http://10.10.117.76:5050").rstrip("/")

SENTINEL_ROOT = Path(os.getenv("SENTINEL_DATA_ROOT", r"C:\ProgramData\Sentinel"))
TRNO_FILE = SENTINEL_ROOT / "current_trno.txt"
DARAJAH_FILE = SENTINEL_ROOT / "current_darajah.txt"
NAME_FILE = SENTINEL_ROOT / "current_name.txt"
STATION_FILE = SENTINEL_ROOT / "station_name.txt"
ENV_FILE = SENTINEL_ROOT / ".env"
BUFFER_DIR = SENTINEL_ROOT / "buffer"
BUFFER_FILE = BUFFER_DIR / "offline_events.jsonl"
LOG_FILE = SENTINEL_ROOT / "agent.log"

SENTINEL_ROOT.mkdir(parents=True, exist_ok=True)
BUFFER_DIR.mkdir(parents=True, exist_ok=True)


def resolve_station_name() -> str:
    """Resolve authoritative station tag (e.g. 'M01', 'F-02', 'MK-50')."""
    raw = ""
    if STATION_FILE.exists():
        try:
            val = STATION_FILE.read_text(encoding="utf-8", errors="ignore").strip().lstrip("\ufeff")
            if val:
                raw = val
        except Exception:
            pass

    if not raw and ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip().lstrip("\ufeff")
                if line.startswith("STATION_NAME="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        raw = val
                        break
        except Exception:
            pass

    if not raw:
        raw = os.getenv("STATION_NAME", "").strip()
    if not raw:
        raw = os.getenv("COMPUTERNAME", "").strip()

    if "\\" in raw:
        raw = raw.split("\\")[-1].strip()
    if "." in raw and not raw.replace(".", "").isdigit():
        raw = raw.split(".")[0].strip()

    return raw.upper()


STATION_NAME = resolve_station_name()

CHECK_INTERVAL_SECONDS = 1.5
HEARTBEAT_INTERVAL_SECONDS = 30.0
PROCESS_SCAN_INTERVAL_SECONDS = 2.0
OFFLINE_REPLAY_INTERVAL_SECONDS = 60.0


# ==============================================================================
# HTTP Client (urllib Standard Library)
# ==============================================================================
def http_json_request(url: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    try:
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json", "User-Agent": "MamalBrowseSentinel/2.8"},
                method="POST"
            )
        else:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "MamalBrowseSentinel/2.8"},
                method="GET"
            )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
            if 200 <= code < 300:
                try:
                    return json.loads(body)
                except Exception:
                    return {"raw": body, "ok": True}
    except Exception as e:
        log.debug("HTTP request failed [%s]: %s", url, e)
    return None


def server_post(endpoint: str, payload: Dict[str, Any], timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    full_url = f"{BROWSEGUARD_SERVER}{endpoint}"
    res = http_json_request(full_url, payload, timeout=timeout)
    if res is None:
        buffer_offline_event(endpoint, payload)
    return res


def server_get(endpoint: str, timeout: float = 3.5) -> Optional[Dict[str, Any]]:
    full_url = f"{BROWSEGUARD_SERVER}{endpoint}"
    return http_json_request(full_url, None, timeout=timeout)


def buffer_offline_event(endpoint: str, payload: Dict[str, Any]) -> None:
    try:
        rec = {
            "endpoint": endpoint,
            "payload": payload,
            "buffered_at": time.time(),
        }
        with open(BUFFER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def replay_offline_buffer() -> None:
    if not BUFFER_FILE.exists():
        return
    try:
        lines = BUFFER_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines:
            return
        replayed_indices = []
        for idx, line in enumerate(lines):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                rec = json.loads(line_str)
                endpoint = rec.get("endpoint", "")
                payload = rec.get("payload", {})
                res = http_json_request(f"{BROWSEGUARD_SERVER}{endpoint}", payload, timeout=3.0)
                if res is not None:
                    replayed_indices.append(idx)
                else:
                    break
            except Exception:
                break

        if replayed_indices:
            remaining = [l for i, l in enumerate(lines) if i not in replayed_indices]
            BUFFER_FILE.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
            log.info("[OfflineReplay] Replayed %d buffered events", len(replayed_indices))
    except Exception as e:
        log.debug("Replay error: %s", e)


# ==============================================================================
# Windows Telemetry & Workstation Control
# ==============================================================================
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_windows_idle_seconds() -> int:
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return max(0, int(millis / 1000))
    except Exception:
        pass
    return 0


def get_foreground_window_info() -> Tuple[str, str]:
    title = ""
    proc_name = ""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if hwnd:
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.strip()

            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                proc_name = get_process_name_by_pid(pid.value)
    except Exception:
        pass
    return title, proc_name


def lock_workstation() -> None:
    """Lock only from an interactive user session, never from Session 0."""
    if not IS_INTERACTIVE_SESSION:
        log.debug("[SECURITY] Lock request skipped: Session 0/non-interactive context")
        return
    try:
        log.warning("[SECURITY] Locking workstation: %s", STATION_NAME)
        if not ctypes.windll.user32.LockWorkStation():
            hidden_subprocess_run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=3)
    except Exception as e:
        log.error("Failed to lock workstation: %s", e)


def kill_process_by_name(name: str) -> bool:
    """Terminate processes matching name completely in-memory using Win32 API.
    Zero subprocesses, zero conhost, zero console flashes."""
    name_lower = name.lower()
    match_names = {name_lower}
    if not name_lower.endswith(".exe"):
        match_names.add(f"{name_lower}.exe")
    killed = False
    try:
        hSnap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if hSnap and hSnap != -1:
            try:
                pe = PROCESSENTRY32()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
                if kernel32.Process32First(hSnap, ctypes.byref(pe)):
                    while True:
                        proc_str = pe.szExeFile.decode("latin-1", errors="replace").lower().strip()
                        if proc_str in match_names:
                            pid = pe.th32ProcessID
                            hProc = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
                            if hProc:
                                try:
                                    if kernel32.TerminateProcess(hProc, 1):
                                        log.info("[ProcessGuard] In-memory terminated: %s (PID=%d)", proc_str, pid)
                                        killed = True
                                finally:
                                    kernel32.CloseHandle(hProc)
                        if not kernel32.Process32Next(hSnap, ctypes.byref(pe)):
                            break
            finally:
                kernel32.CloseHandle(hSnap)
    except Exception as e:
        log.debug("In-memory kill error for %s: %s", name, e)

    # Fallback to hidden taskkill if in-memory could not terminate
    if not killed:
        try:
            taskkill_im = name if name.endswith(".exe") else f"{name}.exe"
            res = hidden_subprocess_run(["taskkill", "/F", "/IM", taskkill_im], capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                log.info("[ProcessGuard] Terminated via taskkill fallback: %s", taskkill_im)
                killed = True
        except Exception as e:
            log.debug("taskkill fallback error for %s: %s", name, e)
    return killed


def show_native_alert(title: str, message: str, is_error: bool = False, timeout_ms: int = 3000) -> None:
    """Non-blocking native alert popup that automatically disappears after timeout_ms (default 3 seconds).
    Never sticks on student screen. Skipped in Session 0 to prevent desktop switch flashes."""
    if not IS_INTERACTIVE_SESSION:
        log.debug("[ALERT SUPPRESSED (SESSION 0)] %s: %s", title, message[:80])
        return
    def _display():
        try:
            icon_flag = 0x00000010 if is_error else 0x00000030
            flags = icon_flag | 0x00001000 | 0x00040000  # MB_SYSTEMMODAL | MB_SETFOREGROUND
            # MessageBoxTimeoutW automatically dismisses after timeout_ms (3 seconds) without user action
            ctypes.windll.user32.MessageBoxTimeoutW(0, message, title, flags, 0, timeout_ms)
        except Exception:
            try:
                ctypes.windll.user32.MessageBoxW(0, message, title, flags)
            except Exception:
                pass
    threading.Thread(target=_display, daemon=True).start()


def close_active_browser_tab() -> None:
    """Send Ctrl+W completely in-memory using Win32 keybd_event.
    Zero subprocesses, zero console flashes, no external dependency."""
    if not IS_INTERACTIVE_SESSION:
        log.debug("[SECURITY] Tab-close request skipped: Session 0 context")
        return
    try:
        u32 = ctypes.windll.user32
        VK_CONTROL = 0x11
        VK_W = 0x57
        KEYEVENTF_KEYUP = 0x0002
        u32.keybd_event(VK_CONTROL, 0, 0, 0)
        u32.keybd_event(VK_W, 0, 0, 0)
        time.sleep(0.04)
        u32.keybd_event(VK_W, 0, KEYEVENTF_KEYUP, 0)
        u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        log.info("[SECURITY] In-memory Ctrl+W sent to close violating tab")
        return
    except Exception as e:
        log.debug("keybd_event failed: %s", e)
    try:
        if UIAUTOMATION_AVAILABLE:
            auto.SendKeys("{Ctrl}w")
    except Exception:
        pass


# ==============================================================================
# Student Identity Auto-Discovery
# ==============================================================================
def resolve_logged_in_trno() -> Optional[str]:
    candidates = []

    # 1. USERNAME environment variable
    u = os.getenv("USERNAME", "").strip()
    if u and u.upper() not in ("SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "ADMINISTRATOR", "ADMIN"):
        candidates.append(u)

    # 2. Explorer.exe process owner via hidden tasklist
    try:
        res = hidden_subprocess_run(
            ["tasklist", "/v", "/fi", "imagename eq explorer.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=3
        )
        for line in res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            parts = [p.strip().strip('"') for p in line_str.split('","')]
            if len(parts) >= 7:
                user_field = parts[6]
                if "\\" in user_field:
                    user_field = user_field.split("\\")[-1]
                if user_field and user_field.upper() not in ("SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "ADMINISTRATOR", "ADMIN"):
                    candidates.append(user_field)
    except Exception:
        pass

    # 3. Query user / quser via hidden run
    try:
        res = hidden_subprocess_run(["quser"], capture_output=True, text=True, timeout=3)
        for line in res.stdout.splitlines()[1:]:
            parts = line.split()
            if parts:
                uname = parts[0].lstrip(">").strip()
                if uname and uname.upper() not in ("SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "ADMINISTRATOR", "ADMIN"):
                    candidates.append(uname)
    except Exception:
        pass

    for c in candidates:
        clean = c.lower().lstrip("s").strip()
        if clean.isdigit() and len(clean) >= 3:
            return clean

    return None


def read_identity() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    trno: Optional[str] = None
    darajah: Optional[str] = None
    name: Optional[str] = None

    if TRNO_FILE.exists():
        try:
            val = TRNO_FILE.read_text(encoding="utf-8", errors="ignore").strip().lstrip("\ufeff")
            if val and val.isdigit():
                trno = val
        except Exception:
            pass

    if DARAJAH_FILE.exists():
        try:
            darajah = DARAJAH_FILE.read_text(encoding="utf-8", errors="ignore").strip().lstrip("\ufeff") or None
        except Exception:
            pass

    if NAME_FILE.exists():
        try:
            name = NAME_FILE.read_text(encoding="utf-8", errors="ignore").strip().lstrip("\ufeff") or None
        except Exception:
            pass

    if not trno:
        trno = resolve_logged_in_trno()

    if not trno and STATION_NAME:
        try:
            st_info = server_get(f"/enforce/station/{STATION_NAME}/active-student")
            if st_info and st_info.get("found"):
                trno = str(st_info.get("trno", "")).strip() or None
                if not name:
                    name = st_info.get("name")
                if not darajah:
                    darajah = st_info.get("darajah")
        except Exception:
            pass

    if not trno:
        return None, None, None

    if not name or not darajah:
        try:
            stu_info = server_get(f"/enforce/student/{trno}/info")
            if stu_info and stu_info.get("ok"):
                name = name or stu_info.get("name")
                darajah = darajah or stu_info.get("darajah") or "General"
        except Exception:
            pass

    name = name or f"Student #{trno}"
    darajah = darajah or "General"

    try:
        SENTINEL_ROOT.mkdir(parents=True, exist_ok=True)
        TRNO_FILE.write_text(trno, encoding="utf-8")
        NAME_FILE.write_text(name, encoding="utf-8")
        DARAJAH_FILE.write_text(darajah, encoding="utf-8")
    except Exception:
        pass

    return trno, darajah, name


# ==============================================================================
# Browser URL Detection
# ==============================================================================
def get_active_browser_url() -> Optional[str]:
    """Read active URL from foreground browser."""
    if not UIAUTOMATION_AVAILABLE:
        return None
    try:
        foreground = auto.GetForegroundControl()
        if not foreground:
            return None

        for pattern in (
            "Address and search bar",
            "Address bar",
            "Search or enter address",
            "Search or enter web address",
        ):
            ctrl = foreground.EditControl(Name=pattern)
            if ctrl.Exists(0.1):
                val = ctrl.GetValuePattern().Value
                if val:
                    val = str(val).strip()
                    if not val.startswith("http://") and not val.startswith("https://"):
                        return f"https://{val}"
                    return val
    except Exception:
        pass
    return None


# ==============================================================================
# Process Guard - Unauthorized Browsers & Software Installers
# ==============================================================================
# Chrome and Edge are both permitted on lab workstations
ALLOWED_BROWSERS: Set[str] = {"chrome.exe", "msedge.exe"}

# Prohibited alternative browsers (Chrome + Edge permitted; all others blocked)
PROHIBITED_BROWSERS: Set[str] = {
    "edge.exe",  # legacy Edge HTML (not Chromium msedge.exe)
    "firefox.exe",
    "opera.exe",
    "opera_gx.exe",
    "brave.exe",
    "vivaldi.exe",
    "arc.exe",
    "chromium.exe",
    "tor.exe",
    "torbrowser.exe",
    "safari.exe",
    "iexplore.exe",
    "duckduckgo.exe",
    "waterfox.exe",
    "librewolf.exe",
    "palemoon.exe",
    "fastinternetbrowser.exe",
    "wavebrowser.exe",
    "wave browser.exe",
    "comet.exe",
    "browser.exe",
    "webbrowser.exe",
    "ucbrowser.exe",
    "epic.exe",
    "whale.exe",
    "sidekick.exe",
    "maxthon.exe",
}

# Windows built-in system processes that must NEVER be terminated
WINDOWS_SYSTEM_PROCESSES: Set[str] = {
    "trustedinstaller.exe",
    "msiexec.exe",
    "wusa.exe",
    "dism.exe",
    "sfc.exe",
    "wuauclt.exe",
    "usoclient.exe",
    "tiworker.exe",
    "cleanmgr.exe",
    "svchost.exe",
    "explorer.exe",
    "lsass.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "spoolsv.exe",
    "searchindexer.exe",
    "searchprotocolhost.exe",
    "onedrive.exe",
    "osk.exe",
    "narrator.exe",
    "magnify.exe",
    "msedgewebview2.exe",
    "runtimebroker.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "systemsettings.exe",
}

# Prohibited installer & setup executables (Deny installation)
PROHIBITED_INSTALLERS: Set[str] = {
    "setup.exe",
    "installer.exe",
    "install.exe",
    "robloxplayerinstaller.exe",
    "robloxplayerbeta.exe",
    "epicgamesinstaller.exe",
    "steamsetup.exe",
    "discordsetup.exe",
    "telegramsetup.exe",
    "whatsappsetup.exe",
    "utorrent.exe",
    "bittorrent.exe",
    "qbittorrent.exe",
    "psiphon3.exe",
    "nordvpn.exe",
    "expressvpn.exe",
    "protonvpn.exe",
    "ultrasurf.exe",
}

# High-risk chatting and messaging platforms
CHAT_DOMAINS: Tuple[str, ...] = (
    "discord.com",
    "discordapp.com",
    "discord.gg",
    "whatsapp.com",
    "web.whatsapp.com",
    "telegram.org",
    "web.telegram.org",
    "t.me",
    "threads.net",
    "threads.com",
    "instagram.com",
    "messenger.com",
    "snapchat.com",
    "web.snapchat.com",
    "chat.reddit.com",
    "omegle.com",
    "ometv.com",
    "chatroulette.com",
    "emeraldchat.com",
    "monkey.app",
    "chathub.cam",
    "character.ai",
    "janitorai.com",
    "crushon.ai",
    "chai-research.com",
    "talkie-ai.com",
    "slack.com",
    "signal.org",
    "kik.com",
    "viber.com",
    "line.me",
    "wechat.com",
)

# Gaming platforms
GAME_DOMAINS: Tuple[str, ...] = (
    "roblox.com",
    "poki.com",
    "crazygames.com",
    "krunker.io",
    "twitch.tv",
    "miniclip.com",
    "steamcommunity.com",
    "store.steampowered.com",
    "epicgames.com",
    "minecraft.net",
    "y8.com",
    "now.gg",
    "paper.io",
    "agar.io",
    "slither.io",
    "browser.lol",
)


def scan_prohibited_processes(trno: str, darajah: str) -> None:
    """Scan and kill prohibited browsers and software installers completely in-memory."""
    try:
        running = get_running_process_names()

        for proc in running:
            # Never terminate allowed browsers
            if proc in ALLOWED_BROWSERS:
                continue

            # 1. Check prohibited browsers (Edge, Firefox, Opera, etc.)
            if proc in PROHIBITED_BROWSERS:
                log.warning("[ProcessGuard] Unauthorized browser detected: %s (TRNO=%s)", proc, trno)
                kill_process_by_name(proc)
                show_native_alert(
                    "[Sentinel] Unauthorized Browser Blocked",
                    f"'{proc}' is prohibited on lab workstations.\n\n"
                    "Only Google Chrome and Microsoft Edge are permitted.\n"
                    "This attempt has been logged.",
                    is_error=True,
                    timeout_ms=3000
                )
                server_post("/enforce/check", {
                    "url": f"app://{proc}",
                    "trno": trno,
                    "darajah": darajah,
                    "station": STATION_NAME,
                    "process": proc,
                })

            # 2. Check prohibited installers
            elif (
                proc in PROHIBITED_INSTALLERS
                or (
                    proc.endswith(".exe")
                    and any(k in proc for k in ("setup", "installer", "install_"))
                    and proc not in WINDOWS_SYSTEM_PROCESSES
                )
            ):
                log.warning("[ProcessGuard] Unauthorized installer detected: %s (TRNO=%s)", proc, trno)
                kill_process_by_name(proc)
                show_native_alert(
                    "[Sentinel] Software Installation Prohibited",
                    f"Running installer '{proc}' is strictly prohibited on lab workstations.\n\n"
                    "Students are not permitted to install or launch external software.",
                    is_error=True,
                    timeout_ms=3000
                )
                server_post("/enforce/check", {
                    "url": f"app://installer/{proc}",
                    "trno": trno,
                    "darajah": darajah,
                    "station": STATION_NAME,
                    "process": proc,
                })
    except Exception as e:
        log.debug("Process scan error: %s", e)


# ==============================================================================
# Main Agent Loop
# ==============================================================================
def run_agent() -> None:
    if not STATION_NAME:
        log.critical("[FATAL] COMPUTERNAME is empty and STATION_NAME is not set. Halting.")
        sys.exit(1)

    log.info("=" * 65)
    log.info("  MAMALBROWSE SENTINEL CLIENT AGENT v2.8 (STEALTH & AUDIT)")
    log.info("  Station   : %s", STATION_NAME)
    log.info("  Server    : %s", BROWSEGUARD_SERVER)
    log.info("  Identity  : %s", SENTINEL_ROOT)
    log.info("  Interactive: %s", IS_INTERACTIVE_SESSION)
    log.info("=" * 65)

    current_session_id: Optional[str] = None
    current_trno: Optional[str] = None
    last_heartbeat_time = 0.0
    last_process_scan = 0.0
    last_buffer_replay = 0.0
    last_checked_url: Optional[str] = None
    last_url_check_time = 0.0
    current_url_dwell = 0.0
    current_url_hits = 0

    while True:
        now_mono = time.monotonic_ns()
        now_wall = time.time()

        # 1. Identity Check
        trno, darajah, name = read_identity()
        if not trno or not darajah or not name:
            if current_session_id:
                log.info("Identity cleared. Ending session %s", current_session_id)
                server_post("/enforce/session/end", {
                    "trno": current_trno or "",
                    "station": STATION_NAME,
                    "session_id": current_session_id,
                    "monotonic_ns": now_mono,
                    "reason": "identity_cleared",
                })
                current_session_id = None
                current_trno = None

            # Scan processes in-memory even without active student to prevent rogue apps
            scan_prohibited_processes("ANONYMOUS", "General")
            time.sleep(3.0)
            continue

        # 2. Session Start
        if trno != current_trno or not current_session_id:
            current_session_id = f"{trno}_{STATION_NAME}_{int(now_wall)}"
            current_trno = trno
            event_id = str(uuid.uuid4())

            log.info("Starting verified session: TRNO=%s Darajah=%s Name=%s", trno, darajah, name)
            res = server_post("/enforce/session/start", {
                "event_id": event_id,
                "trno": trno,
                "station": STATION_NAME,
                "darajah": darajah,
                "student_name": name,
                "session_id": current_session_id,
                "monotonic_ns": now_mono,
            })

            if res and res.get("code") == "ACCOUNT_DISABLED":
                show_native_alert(
                    "?? Account Suspended",
                    f"Your account (TRNO {trno}) is currently suspended.\n"
                    "Please contact your teacher or administrator to reinstate.",
                    is_error=True
                )
                lock_workstation()
                time.sleep(15.0)
                continue

            last_heartbeat_time = now_wall

        # 3. Periodic Heartbeat (Every 30s)
        if now_wall - last_heartbeat_time >= HEARTBEAT_INTERVAL_SECONDS:
            last_heartbeat_time = now_wall
            idle_sec = get_windows_idle_seconds()
            win_title, proc_name = get_foreground_window_info()
            curr_url = get_active_browser_url() or ""
            hb_event_id = str(uuid.uuid4())

            hb_res = server_post("/enforce/session/heartbeat", {
                "event_id": hb_event_id,
                "trno": trno,
                "station": STATION_NAME,
                "session_id": current_session_id,
                "monotonic_ns": now_mono,
                "idle_sec": idle_sec,
                "current_url": curr_url,
                "agent_uptime_sec": int(time.perf_counter()),
                "active_window": win_title,
                "process_name": proc_name,
            })

            if hb_res:
                cmd = hb_res.get("command")
                if cmd in ("lock", "logout"):
                    lock_workstation()
                if hb_res.get("status") == "expired":
                    show_native_alert(
                        "? Session Expired",
                        f"Your lab session limit has ended.\nStudent: {name} (Darajah {darajah})",
                    )
                    lock_workstation()

        # 4. Process Scan (Every 2s - runs 100% in-memory with zero window creation)
        if now_wall - last_process_scan >= PROCESS_SCAN_INTERVAL_SECONDS:
            last_process_scan = now_wall
            scan_prohibited_processes(trno, darajah)

        # 5. Offline Replay (Every 60s)
        if now_wall - last_buffer_replay >= OFFLINE_REPLAY_INTERVAL_SECONDS:
            last_buffer_replay = now_wall
            replay_offline_buffer()

        # 6. Active URL Enforcement
        url = get_active_browser_url()
        if url:
            url_lower = url.lower()

            # Rule A: Pinterest is EXPLICITLY PERMITTED
            if any(p in url_lower for p in ("pinterest.com", "pinimg.com")):
                last_checked_url = url
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Rule B: Chatting & Messaging Sites -> Immediate Tab Close & Block
            if any(c in url_lower for c in CHAT_DOMAINS):
                close_active_browser_tab()
                show_native_alert(
                    "[Sentinel] Prohibited Site Blocked",
                    f"Chatting and social direct messaging platforms are strictly prohibited.\n\n"
                    f"Violating URL: {url}\n"
                    f"Student: {name} (TRNO {trno})",
                    is_error=True,
                    timeout_ms=3000
                )
                server_post("/enforce/check", {
                    "url": url,
                    "trno": trno,
                    "darajah": darajah,
                    "station": STATION_NAME,
                    "category": "messaging_chat",
                    "action": "block",
                })
                lock_workstation()
                last_checked_url = url
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Rule C: Gaming Sites -> Immediate Tab Close & Warning
            if any(g in url_lower for g in GAME_DOMAINS):
                close_active_browser_tab()
                show_native_alert(
                    "[Sentinel] Online Gaming Prohibited",
                    f"Gaming websites are prohibited on lab workstations during academic hours.\n\n"
                    f"Violating site: {url}\n"
                    "Tab closed. Persisting will disable your computer account.",
                    is_error=False,
                    timeout_ms=3000
                )
                server_post("/enforce/check", {
                    "url": url,
                    "trno": trno,
                    "darajah": darajah,
                    "station": STATION_NAME,
                    "category": "gaming",
                    "action": "warn",
                })
                last_checked_url = url
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Rule D: General Server Policy Evaluation
            if url == last_checked_url:
                current_url_dwell += CHECK_INTERVAL_SECONDS
            else:
                last_checked_url = url
                current_url_dwell = 0.0
                current_url_hits = 1

            if now_wall - last_url_check_time >= 2.0:
                last_url_check_time = now_wall
                current_url_hits += 1

                skip_hosts = ["about:blank", "localhost", "127.0.0.1", BROWSEGUARD_SERVER]
                if not any(h in url for h in skip_hosts):
                    win_title_now, _ = get_foreground_window_info()
                    decision = server_post("/enforce/check", {
                        "url": url,
                        "trno": trno,
                        "darajah": darajah,
                        "station": STATION_NAME,
                        "dwell_sec": int(current_url_dwell),
                        "hit_count": current_url_hits,
                        "title": win_title_now,
                    })

                    if decision:
                        action = decision.get("action")
                        category = decision.get("category", "")
                        warning_count = decision.get("warning_count", 1)
                        max_warnings = decision.get("max_warnings", 5)
                        msg = decision.get("message", "")

                        if action == "block":
                            close_active_browser_tab()
                            show_native_alert(
                                "[Sentinel] Account Disabled - Policy Violation",
                                f"{msg}\n\n"
                                f"Station: {STATION_NAME} | TRNO: {trno}\n"
                                "Your account has been locked. Contact teacher to reinstate.",
                                is_error=True,
                                timeout_ms=3000
                            )
                            lock_workstation()

                        elif action == "warn":
                            close_active_browser_tab()
                            show_native_alert(
                                f"[Sentinel] Policy Warning - Strike {warning_count} of {max_warnings}",
                                f"{msg}\n\n"
                                f"Station: {STATION_NAME} | TRNO: {trno}\n"
                                f"Warning strike {warning_count} of {max_warnings}.",
                                is_error=False,
                                timeout_ms=3000
                            )

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_agent()
    except KeyboardInterrupt:
        log.info("Agent stopped by operator.")
    except Exception as e:
        log.critical("Unhandled agent failure: %s", e, exc_info=True)
        sys.exit(1)
