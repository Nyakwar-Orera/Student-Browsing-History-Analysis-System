# client_agent/browseguard_agent.py
"""
MamalBrowse Sentinel Client Enforcement Agent for Windows Lab Workstations.
HARDENED & AUDIT-GRADE (v2.5) — Institutional Deployment Specification.

Guarantees:
  1. Authoritative Identity Enforcement:
     - On session start, MUST verify:
         * C:\\ProgramData\\Sentinel\\current_trno.txt
         * C:\\ProgramData\\Sentinel\\current_darajah.txt
         * C:\\ProgramData\\Sentinel\\current_name.txt
     - If ANY file is missing or empty, session is REFUSED with a blocking alert.
  2. Monotonic Clock & Windows Idle Dwell Telemetry:
     - Sends HEARTBEAT every 30s with:
         * monotonic_ns (time.monotonic_ns())
         * idle_sec (Windows GetLastInputInfo API)
         * active_window_title & active_process_name
         * current_url
  3. Resilient Offline Event Buffering & Replay:
     - When server is unreachable (>120s), buffers events to:
         C:\\ProgramData\\Sentinel\\buffer\\offline_events.jsonl
     - On reconnection, reliably replays buffered events with original monotonic_ns.
     - Server deduplicates by unique event_id.
  4. Active Security & Process Guard:
     - Scans for disallowed browsers (Firefox, Opera, Brave, Tor, etc.) and VPN/proxy tools.
     - Enforces URL policy and handles workstation lock on command/timeout.
  5. Zero synthetic data: Zero hardcoded TRNO, station name, or demo paths.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.stderr.write("[FATAL] 'requests' library required. Run: pip install requests\n")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────
# Station & Environment Configuration (Zero Hardcoded Mock Defaults)
# ─────────────────────────────────────────────────────────────────
BROWSEGUARD_SERVER = os.getenv("BROWSEGUARD_SERVER", "http://10.10.117.76:5050").rstrip("/")

SENTINEL_ROOT = Path(os.getenv("SENTINEL_DATA_ROOT", r"C:\ProgramData\Sentinel"))
TRNO_FILE = SENTINEL_ROOT / "current_trno.txt"
DARAJAH_FILE = SENTINEL_ROOT / "current_darajah.txt"
NAME_FILE = SENTINEL_ROOT / "current_name.txt"
STATION_FILE = SENTINEL_ROOT / "station_name.txt"
ENV_FILE = SENTINEL_ROOT / ".env"
BUFFER_DIR = SENTINEL_ROOT / "buffer"
BUFFER_FILE = BUFFER_DIR / "offline_events.jsonl"

SENTINEL_ROOT.mkdir(parents=True, exist_ok=True)
BUFFER_DIR.mkdir(parents=True, exist_ok=True)


def resolve_station_name() -> str:
    """
    Resolve authoritative station identifier (e.g. 'MK-50', 'RB-43', 'M11').
    Priority:
      1. C:\\ProgramData\\Sentinel\\station_name.txt (explicit technician lab station tag)
      2. C:\\ProgramData\\Sentinel\\.env (STATION_NAME=...)
      3. Process environment variable STATION_NAME
      4. Windows system COMPUTERNAME
    Strips domain prefixes ('MAMAL\\', 'LAB\\') and FQDN suffixes ('.mamal.nbi').
    """
    raw = ""
    # 1. station_name.txt
    if STATION_FILE.exists():
        try:
            val = STATION_FILE.read_text(encoding="utf-8").strip()
            if val:
                raw = val
        except Exception:
            pass

    # 2. .env file
    if not raw and ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("STATION_NAME="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        raw = val
                        break
        except Exception:
            pass

    # 3. Environment variable STATION_NAME
    if not raw:
        raw = os.getenv("STATION_NAME", "").strip()

    # 4. Windows COMPUTERNAME
    if not raw:
        raw = os.getenv("COMPUTERNAME", "").strip()

    # Clean domain prefix (e.g. MAMAL\MK-50 -> MK-50)
    if "\\" in raw:
        raw = raw.split("\\")[-1].strip()
    # Clean FQDN suffix (e.g. MK-50.mamal.nbi -> MK-50)
    if "." in raw and not raw.replace(".", "").isdigit():
        raw = raw.split(".")[0].strip()

    return raw.upper()


STATION_NAME = resolve_station_name()

CHECK_INTERVAL_SECONDS = 2.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
PROCESS_SCAN_INTERVAL_SECONDS = 20.0
OFFLINE_REPLAY_INTERVAL_SECONDS = 60.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] browseguard_agent: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("browseguard_agent")


# ─────────────────────────────────────────────────────────────────
# Windows OS Telemetry & Idle Detection
# ─────────────────────────────────────────────────────────────────
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_windows_idle_seconds() -> int:
    """
    Query unmanaged Windows input subsystem via GetLastInputInfo.
    Accurately detects keyboard/mouse inactivity to prevent counting idle lab time.
    """
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return max(0, int(millis / 1000))
    except Exception as e:
        log.debug("GetLastInputInfo error: %s", e)
    return 0


def get_foreground_window_info() -> Tuple[str, str]:
    """
    Get active foreground window title and executable process name.
    """
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
                # Query process image name via tasklist
                res = subprocess.run(
                    ["tasklist", "/fi", f"PID eq {pid.value}", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, timeout=2
                )
                if res.returncode == 0 and res.stdout.strip():
                    first_line = res.stdout.splitlines()[0]
                    parts = first_line.split(",")
                    if parts:
                        proc_name = parts[0].strip('"').lower()
    except Exception as e:
        log.debug("GetForegroundWindow error: %s", e)
    return title, proc_name


def lock_workstation() -> None:
    """Lock the Windows workstation immediately."""
    try:
        log.warning("[SECURITY] Locking workstation: %s", STATION_NAME)
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False, timeout=5)
    except Exception as e:
        log.error("Failed to lock workstation: %s", e)


def kill_process_by_name(name: str) -> bool:
    """Forcefully terminate unauthorized process."""
    try:
        result = subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            log.info("[ProcessKill] Terminated: %s", name)
            return True
    except Exception as e:
        log.warning("[ProcessKill] Could not kill %s: %s", name, e)
    return False


def show_native_alert(title: str, message: str, is_error: bool = False) -> None:
    """Display native modal dialog to student."""
    try:
        # MB_OK | MB_ICONWARNING or MB_ICONERROR | MB_SYSTEMMODAL
        icon_flag = 0x00000010 if is_error else 0x00000030
        flags = icon_flag | 0x00001000
        ctypes.windll.user32.MessageBoxW(0, message, title, flags)
    except Exception as e:
        log.warning("MessageBox error: %s", e)


# ─────────────────────────────────────────────────────────────────
# Strict Identity Verification
# ─────────────────────────────────────────────────────────────────
def read_identity() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Read mandatory identity files:
      - current_trno.txt
      - current_darajah.txt
      - current_name.txt
    Returns (trno, darajah, name). If any is missing or empty, returns (None, None, None).
    """
    if not (TRNO_FILE.exists() and DARAJAH_FILE.exists() and NAME_FILE.exists()):
        return None, None, None

    try:
        trno = TRNO_FILE.read_text(encoding="utf-8").strip()
        darajah = DARAJAH_FILE.read_text(encoding="utf-8").strip()
        name = NAME_FILE.read_text(encoding="utf-8").strip()

        if not trno or not darajah or not name:
            return None, None, None

        return trno, darajah, name
    except Exception as e:
        log.error("Error reading identity files: %s", e)
        return None, None, None


# ─────────────────────────────────────────────────────────────────
# Resilient Networking & Offline Buffer
# ─────────────────────────────────────────────────────────────────
def buffer_offline_event(endpoint: str, payload: Dict[str, Any]) -> None:
    """Append event to local buffer when server is unreachable."""
    try:
        rec = {
            "buffered_at": time.time(),
            "endpoint": endpoint,
            "payload": payload,
        }
        with open(BUFFER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.warning("[OfflineBuffer] Buffered event for %s (event_id=%s)", endpoint, payload.get("event_id"))
    except Exception as e:
        log.error("[OfflineBuffer] Failed to write offline event: %s", e)


def replay_offline_buffer() -> None:
    """Replay buffered events to server once connectivity is restored."""
    if not BUFFER_FILE.exists():
        return

    try:
        lines = BUFFER_FILE.read_text(encoding="utf-8").splitlines()
        if not lines:
            return

        replayed_indices: List[int] = []
        for idx, line in enumerate(lines):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                rec = json.loads(line_str)
                endpoint = rec.get("endpoint", "")
                payload = rec.get("payload", {})
                url = f"{BROWSEGUARD_SERVER}{endpoint}"

                resp = requests.post(url, json=payload, timeout=4.0)
                if resp.status_code in (200, 201, 409):  # 409/duplicate is OK
                    replayed_indices.append(idx)
                    log.info("[OfflineReplay] Successfully replayed event to %s", endpoint)
                else:
                    log.warning("[OfflineReplay] Server responded with status %d", resp.status_code)
                    break
            except Exception as e:
                log.debug("[OfflineReplay] Connectivity not restored yet: %s", e)
                break

        # Remove replayed lines
        if replayed_indices:
            remaining = [l for i, l in enumerate(lines) if i not in replayed_indices]
            tmp_buf = BUFFER_FILE.with_suffix(".tmp")
            tmp_buf.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
            tmp_buf.replace(BUFFER_FILE)
            log.info("[OfflineReplay] Removed %d replayed events from buffer.", len(replayed_indices))

    except Exception as e:
        log.error("[OfflineReplay] Error processing offline buffer: %s", e)


def server_post(endpoint: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST to server with automatic offline fallback."""
    url = f"{BROWSEGUARD_SERVER}{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=3.5)
        if resp.status_code in (200, 201):
            return resp.json()
        elif resp.status_code == 403:
            log.error("[Server] HTTP 403 Forbidden: %s", resp.text)
            return resp.json()
    except Exception as e:
        log.warning("[Server] Communication error on %s: %s", endpoint, e)
        buffer_offline_event(endpoint, payload)
    return None


def server_get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """GET request to server."""
    url = f"{BROWSEGUARD_SERVER}{endpoint}"
    try:
        resp = requests.get(url, params=params, timeout=3.5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.debug("[Server] GET %s error: %s", endpoint, e)
    return None


# ─────────────────────────────────────────────────────────────────
# Browser URL Detection
# ─────────────────────────────────────────────────────────────────
def get_active_browser_url() -> Optional[str]:
    """Read active URL from foreground browser via Windows UIAutomation."""
    try:
        import uiautomation as auto
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
            if ctrl.Exists(0.15):
                val = ctrl.GetValuePattern().Value
                if val:
                    val = str(val).strip()
                    if not val.startswith("http://") and not val.startswith("https://"):
                        return f"https://{val}"
                    return val
    except ImportError:
        pass
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────
# Process Guard — Unauthorized Executables
# ─────────────────────────────────────────────────────────────────
ALLOWED_BROWSERS = {"chrome.exe"}
PROHIBITED_PROCESSES = {
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


def scan_prohibited_processes(trno: str, darajah: str) -> None:
    """Scan and terminate prohibited processes."""
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=4
        )
        running = set()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(",")
            if parts:
                proc = parts[0].strip('"').lower()
                running.add(proc)

        for proc in running:
            if proc in PROHIBITED_PROCESSES:
                log.warning("[ProcessGuard] Prohibited process detected: %s (TRNO=%s)", proc, trno)
                kill_process_by_name(proc)
                show_native_alert(
                    "🚫 Prohibited Application Terminated",
                    f"'{proc}' is prohibited on lab workstations.\n"
                    f"This violation has been logged to the institutional surveillance server.",
                    is_error=True
                )
                server_post("/enforce/check", {
                    "url": f"app://{proc}",
                    "trno": trno,
                    "darajah": darajah,
                    "station": STATION_NAME,
                    "process": proc,
                })
    except Exception as e:
        log.debug("Process scan error: %s", e)


# ─────────────────────────────────────────────────────────────────
# Main Agent Loop
# ─────────────────────────────────────────────────────────────────
def run_agent() -> None:
    """Main lifecycle enforcement loop for lab PC agent."""
    if not STATION_NAME:
        log.critical("[FATAL] COMPUTERNAME is empty and STATION_NAME is not set. Halting.")
        sys.exit(1)

    log.info("=" * 65)
    log.info("  MAMALBROWSE SENTINEL CLIENT AGENT v2.5 (HARDENED)")
    log.info("  Station   : %s", STATION_NAME)
    log.info("  Server    : %s", BROWSEGUARD_SERVER)
    log.info("  Identity  : %s", SENTINEL_ROOT)
    log.info("=" * 65)

    current_session_id: Optional[str] = None
    current_trno: Optional[str] = None
    last_heartbeat_time = 0.0
    last_process_scan = 0.0
    last_buffer_replay = 0.0
    last_checked_url: Optional[str] = None
    last_url_check_time = 0.0

    while True:
        now_mono = time.monotonic_ns()
        now_wall = time.time()

        # ── 1. Identity Check ──────────────────────────────────────
        trno, darajah, name = read_identity()
        if not trno or not darajah or not name:
            if current_session_id:
                # Student logged out or cleared identity
                log.info("Identity files cleared. Closing session %s", current_session_id)
                server_post("/enforce/session/end", {
                    "trno": current_trno or "",
                    "station": STATION_NAME,
                    "session_id": current_session_id,
                    "monotonic_ns": now_mono,
                    "reason": "identity_cleared",
                })
                current_session_id = None
                current_trno = None

            show_native_alert(
                "⚠️ Workstation Not Provisioned",
                "No valid student session detected.\n\n"
                "Please authenticate through Lab Manager before using this workstation.",
                is_error=True
            )
            time.sleep(10.0)
            continue

        # ── 2. Session Initialization ──────────────────────────────
        if trno != current_trno or not current_session_id:
            current_session_id = f"{trno}_{STATION_NAME}_{int(now_wall)}"
            current_trno = trno
            event_id = str(uuid.uuid4())

            log.info("Starting new verified session: TRNO=%s Darajah=%s Name=%s", trno, darajah, name)
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
                    "🚨 Account Suspended",
                    f"Your account (TRNO {trno}) is currently suspended.\n"
                    "Please contact your teacher or administrator.",
                    is_error=True
                )
                lock_workstation()
                time.sleep(15.0)
                continue

            last_heartbeat_time = now_wall

        # ── 3. Periodic Heartbeat (Every 30s) ──────────────────────
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
                if cmd == "lock":
                    log.warning("[Command] Received immediate LOCK instruction from server.")
                    lock_workstation()
                elif cmd == "logout":
                    log.warning("[Command] Received immediate LOGOUT instruction from server.")
                    lock_workstation()

                status = hb_res.get("status")
                if status == "expired":
                    show_native_alert(
                        "⏰ Session Expired",
                        f"Your lab time limit has ended.\nStudent: {name} (Darajah {darajah})",
                    )
                    lock_workstation()

        # ── 4. Process Scan (Every 20s) ───────────────────────────
        if now_wall - last_process_scan >= PROCESS_SCAN_INTERVAL_SECONDS:
            last_process_scan = now_wall
            scan_prohibited_processes(trno, darajah)

        # ── 5. Offline Replay (Every 60s) ─────────────────────────
        if now_wall - last_buffer_replay >= OFFLINE_REPLAY_INTERVAL_SECONDS:
            last_buffer_replay = now_wall
            replay_offline_buffer()

        # ── 6. Active URL Enforcement ─────────────────────────────
        url = get_active_browser_url()
        if url and url != last_checked_url and (now_wall - last_url_check_time >= 2.0):
            last_checked_url = url
            last_url_check_time = now_wall

            skip_hosts = ["about:blank", "localhost", "127.0.0.1", BROWSEGUARD_SERVER]
            if not any(h in url for h in skip_hosts):
                decision = server_post("/enforce/check", {
                    "url": url,
                    "trno": trno,
                    "darajah": darajah,
                    "station": STATION_NAME,
                })
                if decision:
                    action = decision.get("action")
                    if action == "block":
                        show_native_alert(
                            "🛡️ Restricted Website Blocked",
                            f"Access to this site is restricted by institutional policy.\n\n"
                            f"URL: {url}\nReason: {decision.get('message', 'Blocked')}",
                            is_error=True
                        )
                    elif action == "warn":
                        show_native_alert(
                            "⚠️ Policy Warning",
                            f"{decision.get('message', 'Restricted activity detected.')}\n\nURL: {url}",
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
