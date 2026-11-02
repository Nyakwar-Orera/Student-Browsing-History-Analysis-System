"""
MamalBrowse Sentinel - Cloudflare Tunnel Manager Service
Automatically manages cloudflared.exe, monitors logs for the assigned trycloudflare.com URL,
and writes updated links to data/external_url.txt and the Administrator Desktop.
Designed to run continuously as a Windows Service or background daemon.
"""

import os
import re
import sys
import time
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BASE_DIR / "tools"
CLOUDFLARED_EXE = TOOLS_DIR / "cloudflared.exe"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
URL_FILE = DATA_DIR / "external_url.txt"
LOG_FILE = LOGS_DIR / "cloudflared_manager.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

URL_REGEX = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def update_desktop_shortcuts(url: str):
    try:
        # Save to data/external_url.txt
        with open(URL_FILE, "w", encoding="utf-8") as f:
            f.write(url + "\n")

        # Desktop paths (user profile and public desktop)
        desktops = []
        user_desktop = Path(os.environ.get("USERPROFILE", "C:\\Users\\admin")) / "Desktop"
        if user_desktop.exists():
            desktops.append(user_desktop)
        public_desktop = Path(os.environ.get("PUBLIC", "C:\\Users\\Public")) / "Desktop"
        if public_desktop.exists():
            desktops.append(public_desktop)

        for d in desktops:
            # ACCESS_LINKS.txt
            links_txt = d / "ACCESS_LINKS.txt"
            content = (
                "======================================================================\n"
                "  MAMALBROWSE SENTINEL - ACTIVE ACCESS LINKS\n"
                f"  Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                "======================================================================\n\n"
                "1. INTERNAL ACCESS (Computer Labs & Campus Wi-Fi):\n"
                "   URL: http://10.10.117.76:5050\n"
                "   Localhost: http://localhost:5050\n\n"
                "2. EXTERNAL ACCESS (Anywhere / Home / Mobile):\n"
                f"   URL: {url}\n"
                "   (Powered by Cloudflare Secure Zero-Trust Tunnel)\n\n"
                "3. DEFAULT LOGINS:\n"
                "   - Administrator  : admin\n"
                "   - Lab Technician : labtech\n"
                "   - Supervisor/HOD : supervisor\n"
                "   (Passwords located in .env file)\n\n"
                "======================================================================\n"
            )
            try:
                links_txt.write_text(content, encoding="utf-8")
            except Exception as e:
                log(f"[WARN] Failed to write ACCESS_LINKS.txt to {d}: {e}")

            # .url shortcut
            url_shortcut = d / "Sentinel Dashboard (External).url"
            shortcut_content = (
                "[InternetShortcut]\n"
                f"URL={url}\n"
                "IconIndex=0\n"
                "IconFile=C:\\Windows\\System32\\shell32.dll\n"
            )
            try:
                url_shortcut.write_text(shortcut_content, encoding="ascii")
            except Exception as e:
                log(f"[WARN] Failed to write shortcut to {d}: {e}")

        log(f"[OK] External access URL active and saved: {url}")
    except Exception as e:
        log(f"[ERROR] Failed in update_desktop_shortcuts: {e}")


def run_tunnel():
    if not CLOUDFLARED_EXE.exists():
        log(f"[FATAL] cloudflared.exe not found at {CLOUDFLARED_EXE}")
        sys.exit(1)

    cmd = [
        str(CLOUDFLARED_EXE),
        "tunnel",
        "--url",
        "http://127.0.0.1:5050",
    ]

    while True:
        log("[INFO] Spawning cloudflared tunnel to http://localhost:5050...")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            found_url = False
            for line in iter(proc.stdout.readline, ""):
                line_clean = line.strip()
                if not line_clean:
                    continue

                # Check for URL
                match = URL_REGEX.search(line_clean)
                if match:
                    current_url = match.group(0)
                    log(f"[FOUND] Tunnel URL: {current_url}")
                    update_desktop_shortcuts(current_url)
                    found_url = True

                # Log important events
                if any(k in line_clean.lower() for k in ["registered tunnel", "connection established", "error", "fail", "quick tunnel"]):
                    log(f"[cloudflared] {line_clean}")

            proc.wait()
            log(f"[WARN] cloudflared process exited with code {proc.returncode}. Restarting in 5s...")
        except Exception as e:
            log(f"[ERROR] Error running cloudflared: {e}")

        time.sleep(5)


if __name__ == "__main__":
    log("MamalBrowse Tunnel Manager started.")
    run_tunnel()

