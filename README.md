# 🛡️ MamalBrowse Sentinel
### Student Browsing History Analysis & Enforcement System
**Al-Jamea tus-Saifiyah, Nairobi Campus (AJSN) — `mamal.nbi` Domain**

---

> **Version:** 2.8.1 Production (Stealth & Zero-Blink Edition)  
> **Server Host:** `MAMALAD` (`10.10.117.76` — Windows Domain Controller)  
> **Internal LAN URL:** http://10.10.117.76:5050 or http://localhost:5050  
> **External Cloudflare Tunnel:** Assigned dynamically at startup (see `data\external_url.txt` and Desktop `ACCESS_LINKS.txt`)  
> **Effective Policy Date:** 31st October 2026  
> **Last Updated:** November 2, 2026  

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture & Technology Stack](#2-architecture--technology-stack)
3. [Core Policy Rules (Effective 31st Oct 2026)](#3-core-policy-rules-effective-31st-oct-2026)
4. [Client Agent v2.8.1 (Zero-Blink & Stealth Architecture)](#4-client-agent-v281-zero-blink--stealth-architecture)
5. [Browser & Process Enforcement Policy](#5-browser--process-enforcement-policy)
6. [User Roles & Authentication](#6-user-roles--authentication)
7. [Active Directory & LDAP Integration](#7-active-directory--ldap-integration)
8. [External Access (Cloudflare Tunnel)](#8-external-access-cloudflare-tunnel)
9. [Starting, Stopping & Operating the System](#9-starting-stopping--operating-the-system)
10. [Dashboard Pages & Key Features](#10-dashboard-pages--key-features)
11. [Student Photo Integration](#11-student-photo-integration)
12. [Scheduled Automation Tasks](#12-scheduled-automation-tasks)
13. [Domain Deployment & Maintenance Tools](#13-domain-deployment--maintenance-tools)
14. [Troubleshooting & FAQ](#14-troubleshooting--faq)

---

## 1. System Overview

**MamalBrowse Sentinel** is an enterprise-grade real-time web browsing monitoring, analytics, and policy enforcement platform designed specifically for the Al-Jamea tus-Saifiyah Nairobi campus computer laboratories. Running directly on the Windows Domain Controller (`MAMALAD`), Sentinel monitors student lab activity, enforces strict educational boundaries, and manages user accounts directly in Active Directory (`mamal.nbi`).

### Key Capabilities

| Capability | Description |
|---|---|
| 🔍 **Real-Time Monitoring** | Ingests workstation browsing history continuously from network shares and background client agents |
| 🌐 **Permitted Browsers** | Authorizes **Google Chrome** (`chrome.exe`) and **Microsoft Edge** (`msedge.exe`); terminates unauthorized browsers in < 2 seconds |
| 🛡️ **Zero-Blink In-Memory Execution** | 100% in-memory process scanning & termination via Win32 API — zero black command prompt flashes on student monitors |
| ⏱️ **3-Second Auto-Dismiss Alerts** | Policy notifications appear via `user32.MessageBoxTimeoutW` and automatically vanish after 3 seconds (never stick on screen) |
| 🚫 **Policy Enforcement** | Automatically blocks prohibited content and issues progressive warnings or disables accounts according to policy |
| 🛡️ **Active Directory Sync** | Directly modifies student User Account Control (UAC) flags in AD via LDAP3 |
| 🎓 **Fresh Start Governance** | Enforces the 31st October 2026 policy cutoff — re-enabled students start clean |
| 🔒 **Senior Laptop Isolation** | Permanently maintains Darajah 8–11 senior students locked in AD |
| 📺 **Differentiated YouTube Policy** | Blocks YouTube Shorts; warns on entertainment/movies; fully allows educational and Bohra community content |
| 📌 **Ad & Bookmark Protection** | Exempts browser bookmarks and transient ad-redirects from penalties; requires instant ad closure |
| 🔁 **14-Day Suspension Expiry** | Automatically calculates suspension remaining days and restores eligible accounts |
| 📊 **Executive & Audit Reports** | Comprehensive analytics dashboards, print-friendly reports, and PDF export |

### Monitored Student Population

- **Authoritative Student Roster:** 1,397 active students across two Organizational Units (OUs):
  - `TALABAAT` — Male students (Boys Labs: M-Series, MK-Series)
  - `TALEBAAT` — Female students (Girls Labs: F-Series, RB-Series)
- **Workstations Covered:** Over 120 managed workstations across four laboratory complexes:
  - **F-Series:** F-01 to F-40 (Girls Computer Lab)
  - **M-Series:** M01 to M45 (Boys Computer Lab)
  - **MK-Series:** MK-01 to MK-60 (Outer Labs)
  - **RB-Series:** RB-41 to RB-60 (Outer Labs)
- **Classes Covered:** Darajah 1 through 7 (Junior/Middle) and Darajah 8 through 11 (Seniors), Dars Burhani, and IGCSE.

---

## 2. Architecture & Technology Stack

```
┌────────────────────────────────────────────────────────────────────────┐
│                        MAMAL.NBI CAMPUS NETWORK                         │
│                                                                        │
│  ┌──────────────────────┐    Browsing CSVs    ┌────────────────────┐   │
│  │ Student Workstations │ ──────────────────► │ MamalBrowse        │   │
│  │ F-01..40, M01..45,   │  \\10.10.117.76\    │ Sentinel Server    │   │
│  │ MK-01..60, RB-41..60 │  StudentsBrowsing...│ Port :5050         │   │
│  └──────────────────────┘                     └────────┬───────────┘   │
│             ▲                                          │               │
│             │ Sentinel v2.8.1 Agent                    │               │
│             │ (In-memory, Zero-Blink, 3s Alerts)       │               │
│                                                        │               │
│  ┌──────────────────────┐    LDAP3 (NTLM Auth)┌────────▼───────────┐   │
│  │ Active Directory     │ ◄────────────────── │ Waitress WSGI      │   │
│  │ MAMALAD (mamal.nbi)  │  UAC 66048 / 66050  │ Flask Web Engine   │   │
│  │ OUs: TALABAAT/TALEB. │                     │ (32 Worker Threads)│   │
│  └──────────────────────┘                     └────────┬───────────┘   │
│                                                        │               │
│  ┌──────────────────────┐    MySQL (Port 3306)┌────────▼───────────┐   │
│  │ Koha Library DB      │ ◄────────────────── │ Data Storage Layer │   │
│  │ 152.55.221.78        │  Circuit-Breaker 8s │ JSON, CSV, JSONL   │   │
│  └──────────────────────┘                     └────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
                                │
                       Cloudflare Tunnel
                                │
                     ┌──────────▼──────────┐
                     │ Public HTTPS Access │
                     │ *.trycloudflare.com │
                     └─────────────────────┘
```

- **Backend:** Python 3.13 + Flask + Waitress WSGI server (multi-threaded, 32 workers)
- **Dynamic Policy Engine:** Automatically hot-reloads `config/access_policy.yaml` with a 30-second TTL (no server restart required)
- **Directory Services:** Direct LDAP3 with NTLM authentication (`data/ad_credentials.json`)
- **Remote Access:** Cloudflare Tunnel (`cloudflared.exe`) managed via `tools/tunnel_manager.py`
- **Database / Storage:** Localized JSON persistence, partitioned JSONL violation logs, CSV browsing caches, and pooled MySQL for Koha integration
- **Client Agent:** In-memory Win32 Python agent (`browseguard_agent.py`) executed windowless via `pythonw.exe` and VBScript startup wrappers

---

## 3. Core Policy Rules (Effective 31st Oct 2026)

### 1. Fresh Start Policy (October 31, 2026 Cutoff)
- **Rule:** All prior browsing history and violations recorded before October 31, 2026 are excluded from progressive account suspension for junior students (Darajah 1–7).
- **Clearing Infractions:** Once a student account is enabled, their previous violation history is marked cleared. They will only accumulate strikes if they commit new infractions after being enabled.

### 2. Senior Student Directive (Darajah 8 to 11)
- **Rule:** All senior students (Darajah 8, 9, 10, and 11) must remain **permanently disabled** in Active Directory on lab workstations (designated for personal laptops).
- **Enforcement:** The retention sweep and auto-enable tasks explicitly guard against re-enabling any account matching Darajah 8–11. Their UAC is maintained at `66050` (Account Disabled).

### 3. Pinterest Allowed for Design & Curriculum
- **Rule:** Pinterest domains (`pinterest.com`, `uk.pinterest.com`, `in.pinterest.com`, `pinimg.com`, `i.pinimg.com`) are **whitelisted** as permitted academic design research tools.

### 4. YouTube Differentiated Policy
- **YouTube Shorts (`/shorts/`, `#shorts`):** Strictly **blocked** on lab workstations.
- **Entertainment / Movies / Pranks / Music:** Watching entertainment videos during lab hours generates a **Time-Passing Warning** (`warn`). It alerts the student that time is passing on non-academic content. **It never results in an immediate account disablement.**
- **Academic & Bohra Community Content:** Educational tutorials, STEM lectures, Khan Academy, coding, Arabic grammar, and Bohra community content (Aljamea-tus-Saifiyah lectures, waaz, bayan, Quran hifz/tilawat, madeh, marsiya) are **completely allowed**.

### 5. Ad & Redirect Fair-Enforcement Exemption
- **Rule:** Accidental navigation from third-party advertising or redirects does not penalize students.
- **Enforcement:** Transient visits (`< 20s`, low hit count) generate an instant advisory notice with **zero violation strikes**.

### 6. Browser Bookmarks Exemption
- **Rule:** Academic and resource links bookmarked by students in their browser are recognized study shortcuts and never lead to account penalties.

---

## 4. Client Agent v2.8.1 (Zero-Blink & Stealth Architecture)

**Location:** `client_agent/browseguard_agent.py`  
Sentinel Agent v2.8.1 is engineered for complete invisibility on student desktops:

### 1. Elimination of Monitor Blinking / Console Flashing
- **Process Enumeration:** Runs 100% in-memory via Windows `kernel32.CreateToolhelp32Snapshot` and `Process32First` / `Process32Next` (no `tasklist.exe` console windows).
- **Process Termination:** Terminated in-memory via `kernel32.OpenProcess` with `PROCESS_TERMINATE` and `kernel32.TerminateProcess` (no `taskkill.exe` subprocesses).
- **Tab Closing:** Violating browser tabs are closed in-memory using `user32.keybd_event` pressing `Ctrl+W` (no PowerShell subprocesses).
- **Zero Subprocesses During Active Loop:** Active polling runs without spawning any Windows command prompt or console host (`conhost.exe`).

### 2. 3-Second Auto-Dismiss Popups (Non-Sticking)
- When a student attempts to launch an unauthorized browser, run an installer, or visit a prohibited site:
- Alerts are displayed using **`user32.MessageBoxTimeoutW`** with `timeout_ms = 3000` (3 seconds).
- The message box appears on top, alerts the student, and **automatically disappears after 3 seconds** without requiring mouse clicks or keyboard input.
- Popups run in background daemon threads and will never freeze applications or pile up on the desktop.

### 3. Session 0 Safety & Desktop Switch Protection
- The agent detects its Windows session ID (`ProcessIdToSessionId`).
- When running under SYSTEM context (Session 0), GUI alerts and `LockWorkStation()` calls are suppressed to prevent Windows desktop-switch screen blackouts.

### 4. Windowless Stealth Startup
- Installed to Windows Common Startup as **`SentinelAgent.vbs`**:
  ```vb
  Set WshShell = CreateObject("WScript.Shell")
  WshShell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\ProgramData\Sentinel\launch_agent.ps1""", 0, False
  ```
- Uses `SW_HIDE` (`0`) to launch `pythonw.exe` completely hidden from the desktop view.

---

## 5. Browser & Process Enforcement Policy

### Permitted Browsers
Only the following web browsers are permitted for academic research:
1. **Google Chrome** (`chrome.exe`)
2. **Microsoft Edge** (`msedge.exe`)

### Prohibited Browsers (Auto-Terminated in < 2s)
All other browsers are terminated immediately upon detection:
- Firefox (`firefox.exe`)
- Opera & Opera GX (`opera.exe`, `opera_gx.exe`)
- Brave (`brave.exe`)
- Tor Browser (`tor.exe`, `torbrowser.exe`)
- Vivaldi (`vivaldi.exe`)
- Arc (`arc.exe`)
- DuckDuckGo (`duckduckgo.exe`)
- UC Browser, Safari, Waterfox, LibreWolf, Wave Browser, Comet, etc.

### Prohibited Installers & Software Setups
- Executable installers (`setup.exe`, `installer.exe`, `install.exe`, `robloxplayerinstaller.exe`, `steamsetup.exe`, `discordsetup.exe`, `utorrent.exe`, etc.) are blocked.

### Protected Windows System Processes (Never Terminated)
The agent safelists critical Windows operating system services to ensure system stability:
- `trustedinstaller.exe`, `msiexec.exe`, `tiworker.exe`, `wuauclt.exe`, `svchost.exe`, `lsass.exe`, `explorer.exe`, `csrss.exe`, `msedgewebview2.exe` (Windows WebView2 runtime).

---

## 6. User Roles & Authentication

The Sentinel dashboard provides role-based access control (RBAC):

| Role | Username | Default Password | Capabilities |
|---|---|---|---|
| **Administrator** | `admin` | See `.env` | Full system control: AD unlock/lock, policy configuration, audit logs, raw data export |
| **Lab Technician** | `labtech` | See `.env` | Live workstation monitoring, active sessions, manual unblock queue, print daily logs |
| **Supervisor / HOD** | `supervisor` | See `.env` | Executive summary, class violation breakdowns, compliance metrics, PDF reporting |

To change credentials, update the corresponding keys in `.env`.

---

## 7. Active Directory & LDAP Integration

Sentinel operates on the primary Domain Controller (`MAMALAD`).

### Direct LDAP3 Architecture
Windows Domain Controllers enforce UAC token filtering on local administrative logons. Sentinel connects directly to Active Directory via **LDAP3 with NTLM authentication**.

- **Credentials File:** `data/ad_credentials.json`
- **Permissions:** Account Operator / Domain Admin (`MAMAL\admin`)
- **LDAP Port:** TCP 389 (local loopback `127.0.0.1` / `MAMALAD`)
- **UAC Flag Manipulation:**
  - Active Account: `66048` (`DONT_EXPIRE_PASSWORD | NORMAL_ACCOUNT`)
  - Disabled Account: `66050` (`DONT_EXPIRE_PASSWORD | ACCOUNTDISABLE | NORMAL_ACCOUNT`)

---

## 8. External Access (Cloudflare Tunnel)

MamalBrowse Sentinel includes a built-in Cloudflare Tunnel manager providing secure public HTTPS access:

- **Tunnel Binary:** `tools/cloudflared.exe`
- **Manager Script:** `tools/tunnel_manager.py`
- **Output URL File:** `data/external_url.txt`
- **Desktop Links:** Automatically updates `ACCESS_LINKS.txt` and `.url` shortcuts on the administrator's desktop.

### Getting Current External URL
```powershell
Get-Content data\external_url.txt
```

---

## 9. Starting, Stopping & Operating the System

### 1. One-Click Desktop Start
Double-click **`run_server.bat`** on the Administrator Desktop. Launches the Waitress WSGI application server and the Cloudflare tunnel daemon.

### 2. Manual PowerShell Start
```powershell
cd "C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System"
MamalLab\Scripts\python.exe app_production.py
```

### 3. Server Health Check
```powershell
# Verify Port 5050
curl.exe -I http://localhost:5050

# Verify Cloudflare URL
curl.exe -I (Get-Content data\external_url.txt).Trim()
```

---

## 10. Dashboard Pages & Key Features

1. **Active Lab Sessions (`/`)**: Real-time view of all student workstations, logged-in TRNOs, active domains, dwell time, and current policy status.
2. **14-Day Violation Audit Log (`/audit`)**: Authoritative log of recent policy infractions, time remaining in suspension window, category breakdown, and manual unblock button.
3. **Student Directory (`/students`)**: Searchable roster of 1,397 students with photos, Darajah, group, total visits, and policy status. Includes single-click AD unlock.
4. **Suspension Expiry Queue (`/reinstatement`)**: Real-time list of accounts serving temporary suspensions with countdown timers and bulk auto-unblock.
5. **Executive Reports (`/report`)**: HOD compliance metrics, daily violation patterns, category heatmaps, and optimized print/PDF generator.
6. **Policy Configuration (`/security/policy`)**: View and adjust active domain blocklists, streaming thresholds, and enforcement flags.

---

## 11. Student Photo Integration

- **Photo Storage Location:** `F:\Shares\Common Share Files\userimageprofiles\<TRNO>\myloginphoto.png`
- **Local Thumbnail Cache:** `data/photo_cache/<TRNO>.jpg` (200×200 JPEG, instant loading)
- **Pre-Cache Utility:**
  ```powershell
  MamalLab\Scripts\python.exe tools\precache_photos.py
  ```

---

## 12. Scheduled Automation Tasks

Windows Task Scheduler runs two automated maintenance tasks:

### 1. `MamalBrowse-AutoEnable`
- **Schedule:** Daily at **06:00 AM**
- **Action:** Runs `tools/auto_enable_expired_suspensions.py`
- **Behavior:** Re-enables junior accounts that have served their 14-day suspension in Active Directory and Sentinel DB. Permanently ignores Darajah 8–11 seniors.

### 2. `MamalBrowse-RetentionSweep`
- **Schedule:** Hourly
- **Action:** Runs `app/services/retention_policy_service.py`
- **Behavior:** Archives audit records older than 14 days into `students/violations/archive/` to keep UI query performance instantaneous.

---

## 13. Domain Deployment & Maintenance Tools

All utilities are located in `tools/` and `client_agent/`:

### 1. Mass Deployment Across All Labs
Discovers computers in Active Directory OUs and installs the agent, launcher, and VBS startup script:
```powershell
powershell -ExecutionPolicy Bypass -File client_agent\mass_deploy_all_labs.ps1
```

### 2. Fast Parallel Update & Relaunch (v2.8.1)
Updates `browseguard_agent.py` and restarts the stealth agent across all active workstations simultaneously in ~15-20 seconds:
```powershell
MamalLab\Scripts\python.exe tools\push_and_relaunch_v281.py
```

### 3. Emergency Stop All Client Agents
Halts all agent instances across the domain and clears startup entries:
```powershell
powershell -ExecutionPolicy Bypass -File tools\stop_all_client_agents.ps1
```

### 4. Enable Junior Students (Darajah 1–7) & Reset Policy
```powershell
MamalLab\Scripts\python.exe tools\enable_d1_7_and_reset_policy.py
```

### 5. Lock Senior Students (Darajah 8–11) in AD
```powershell
MamalLab\Scripts\python.exe tools\disable_seniors_ldap.py
```

---

## 14. Troubleshooting & FAQ

### Q: Why do student monitors no longer blink or flash black cmd windows?
In Agent v2.8.1, all legacy `tasklist.exe`, `taskkill.exe`, and PowerShell commands were replaced with in-memory Win32 API calls (`CreateToolhelp32Snapshot`, `OpenProcess`, `TerminateProcess`, and `keybd_event`). Startup uses `SentinelAgent.vbs` running `pythonw.exe` with `SW_HIDE`, completely eliminating console creation.

### Q: Can students use Microsoft Edge?
**Yes.** Google Chrome (`chrome.exe`) and Microsoft Edge (`msedge.exe`) are both fully permitted for student browsing. Third-party browsers (Firefox, Opera, Brave, Tor) remain blocked.

### Q: How do policy violation alerts behave on student PCs?
Alerts appear via `user32.MessageBoxTimeoutW` and automatically disappear after **3 seconds**. Students do not need to click "OK", and dialogs never stick or accumulate on the screen.

### Q: Do I need to restart the Sentinel server when updating `access_policy.yaml`?
**No.** The server dynamically reloads `config/access_policy.yaml` on incoming requests with a 30-second cache TTL. Changes take effect automatically without downtime.

---

*MamalBrowse Sentinel © 2026 — Al-Jamea tus-Saifiyah Nairobi Campus*  
*Maintained by AJSN IT & Lab Administration*
