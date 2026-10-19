# MamalBrowse Sentinel — Student Browsing History Analysis & Enforcement System

> **Production Server:** `10.10.117.76:5000` &nbsp;|&nbsp; **Institutional Portal:** `http://10.10.117.76:5000` &nbsp;|&nbsp; **Al Jamea tus Saifiyah**

MamalBrowse Sentinel is an institutional intelligence and computer-lab surveillance platform. It unites real-time website policy enforcement, student session time limits, live interactive analytics dashboards, high-fidelity PDF academic dossiers, remote station enforcement (lock / logout / disable account), and deep integration with student roster directories — all unified under a canonical five-department model.

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Key Capabilities & Modules](#key-capabilities--modules)
3. [Department Model](#department-model)
4. [Project Structure](#project-structure)
5. [Prerequisites](#prerequisites)
6. [Installation & Service Setup](#installation--service-setup)
   - [Server (Automatic via NSSM)](#server-automatic-via-nssm)
   - [Server (Manual Start)](#server-manual-start)
7. [Configuration Reference](#configuration-reference)
   - [.env Configuration](#env-configuration)
   - [config/access_policy.yaml](#configaccess_policyyaml)
8. [Client Lab PC Setup](#client-lab-pc-setup)
9. [Workstation Identity Files](#workstation-identity-files)
10. [Student Academic & Digital Conduct Dossier (PDF Engine)](#student-academic--digital-conduct-dossier-pdf-engine)
11. [Analytics Dashboard & Department Matrix](#analytics-dashboard--department-matrix)
12. [Remote Enforcement & Security](#remote-enforcement--security)
13. [Role-Based Access Control](#role-based-access-control)
14. [Session Time Limits & Retention Policy](#session-time-limits--retention-policy)
15. [API & Service Reference](#api--service-reference)
16. [How to Test the System](#how-to-test-the-system)
17. [Troubleshooting](#troubleshooting)

---

## System Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           Institutional Lab Network                             │
│                                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐    HTTP POST                         │
│  │ Lab PC 1 │  │ Lab PC 2 │  │ Lab PC N │───────────────────┐                  │
│  │(Agent)   │  │(Agent)   │  │(Agent)   │                   │                  │
│  └──────────┘  └──────────┘  └──────────┘                   ▼                  │
│        ▲             ▲             ▲          ┌─────────────────────────────┐   │
│        │ Poll cmds   │             │          │    MamalBrowse Sentinel     │   │
│        └─────────────┴─────────────┘          │    Flask / Waitress WSGI    │   │
│                                               │    Port 5000 (Multi-thread) │   │
│  ┌──────────────────────────┐                 └──────────────┬──────────────┘   │
│  │  data/active_sessions    │◄────── SMB/HTTP ──────────────►│                  │
│  │  data/student_accounts   │                 ┌──────────────▼──────────────┐   │
│  │  data/browsing_history   │                 │   Student Roster Service    │   │
│  └──────────────────────────┘                 │   Canonical Department Map  │   │
│                                               │   (TRNO, ITS, Department)   │   │
│  ┌──────────────────────────┐                 └─────────────────────────────┘   │
│  │   Executive / HOD Web    │                                                   │
│  │   Interactive Dashboard  │◄──── http://10.10.117.76:5000                    │
│  │   & Official Dossier PDF │                                                   │
│  └──────────────────────────┘                                                   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Data Flow

- **Client Agents** (`browseguard_agent.py`) run on each lab PC. They intercept URLs, check against the server policy engine, register sessions, and **poll for remote commands** (lock station / force logout / disable account).
- **Session data** is stored in `data/active_sessions.json` which is synchronized in real-time.
- **Account state** is stored in `data/student_accounts.json`, referenced by client agents on every session start.
- **Browsing logs** accumulate in `data/browsing_history_cache.csv` and `students/violations/` (JSONL per day).

---

## Key Capabilities & Modules

| Module | Description |
| :--- | :--- |
| **Real-time URL Enforcement** | Blocks and warns on 9 categories (social media, gaming, adult, streaming, VPN, piracy, etc.) |
| **Remote Enforcement** | Admin can Lock Station, Force Logout, or Disable Account from the web dashboard — commands delivered to the client agent within seconds |
| **Account Disable/Enable** | Student accounts can be disabled (preventing lab login) or re-enabled by Admin or Lab Tech |
| **Official PDF Academic Dossier** | Generates executive student dossiers in Portrait or Landscape A4 with zero page cutoffs |
| **Top 10 Website Breakdown** | Ranks top 10 domains with favicons, duration bars, dwell pace, and clearance certificates |
| **Monthly Academic Cards** | Visual Fatemi Hijri monthly cards (visits, lab time, sites count, top sites) |
| **AI Academic Focus Rating** | Algorithmic scoring (0–100) analyzing Educational vs Institutional vs Distracting time |
| **Diagnostic Observations** | Clinical pedagogical evaluations by HOD and Class Teacher with actionable study guidance |
| **Temporal Analytics** | Day-of-Week distribution, peak hourly usage, shift activity (Morning, Afternoon, Evening) |
| **Compliance & Department Matrix** | Paginated department-level engagement matrix with drill-down links |
| **Session Timers & Expiry** | Automatic session timeouts customized per department |
| **14-Day Retention Engine** | Hourly background sweep automatically unblocks accounts and archives old violation logs |
| **Emergency Killswitch** | Instantly closes non-compliant browser processes on zero-tolerance infractions |

---

## Department Model

Sentinel uses a canonical five-department structure mapped from Darajah/section data:

| Canonical Department | Maps From |
| :--- | :--- |
| `Culture Générale (Std 1-2)` | Darajah 1 & 2 |
| `Culture Générale (Std 3-4)` | Darajah 3 & 4 |
| `Collegiate I (5-7)` | Darajah 5, 6, 7 |
| `Collegiate II & Higher Studies (Std 8-11)` | Darajah 8, 9, 10, 11 |
| `Dars Burhani` | Dars Burhani / Special programmes |

All dashboard views, analytics, dossiers, and enforcement policies are grouped and displayed using these canonical names.

---

## Project Structure

```text
Student_Browsing_History_Analysis_System/
│
├── app/                              # Core Sentinel Application
│   ├── main.py                       # Flask entry point, routing & APIs
│   ├── student_roster.py             # Roster provider (TRNO & ITS lookup)
│   ├── routes/
│   │   ├── enforcement.py            # /enforce/* client agent & security APIs
│   │   └── darajah.py                # /darajah/* class-level drill-down routes
│   └── services/
│       ├── lab_manager_service.py    # Session data reader & station resolver
│       ├── department_service.py     # Canonical department mapping engine
│       ├── policy_engine.py          # URL classification & threat evaluation
│       ├── account_lock_service.py   # Account suspensions & 14-day retention
│       ├── analytics_service.py      # Dwell time, temporal patterns & matrices
│       ├── session_timer_service.py  # Session limits, warnings & command queue
│       ├── email_alert_service.py    # SMTP notification dispatcher
│       ├── integration_service.py    # Multi-source student profile aggregations
│       └── calendar_utils.py         # Fatemi Hijri ↔ Gregorian calendar
│
├── client_agent/                     # Workstation Client Agent
│   ├── browseguard_agent.py          # URL interception, session timer & remote cmds
│   └── install_agent.bat             # One-click workstation installer
│
├── config/
│   └── access_policy.yaml            # Hot-reloading category policies & session limits
│
├── data/                             # Runtime data stores
│   ├── active_sessions.json          # Live synchronized workstation sessions
│   ├── student_accounts.json         # Account suspension/disable statuses
│   ├── class_map_cache.json          # Department / Section student index
│   ├── browsing_history_cache.csv    # High-performance browsing cache
│   └── lab_manager/                  # Lab Manager mirrored data (usage_data.json, session_history.json)
│
├── students/violations/              # Audit logs (JSONL format per day)
│
├── tools/                            # Deployment tools
│   └── nssm.exe                      # Place NSSM here for service installation
│
├── app_production.py                 # Waitress WSGI multi-threaded production server
├── INSTALL_SERVICES.ps1              # Automatic Windows NSSM service installer
├── UNINSTALL_SERVICES.ps1            # Clean service uninstallation utility
├── requirements.txt                  # Python dependencies
└── README.md                         # This file
```

---

## Prerequisites

- **Operating System:** Windows Server 2019 / Windows 10 / Windows 11 (64-bit)
- **Python:** Python 3.10+ (64-bit) with virtual environment support
- **Network Permissions:**
  - Local port `5000` (MamalBrowse Sentinel Dashboard & WSGI)
  - SMB/HTTP access to `\\10.10.117.76\` shares (student rosters, browsing logs)

---

## Installation & Service Setup

### Server (Automatic via NSSM)

**Prerequisites:**

1. Download `nssm.exe` from https://nssm.cc (64-bit)
2. Place `nssm.exe` inside the `tools\` folder in this project directory
3. Create the Python virtual environment if not already present:
   ```powershell
   python -m venv MamalLab
   ```

**Install:**

Run once in an administrative PowerShell session. This installs Sentinel as an auto-recovering Windows Service, configures firewall rules, stages the client agent, and creates a desktop shortcut:

```powershell
cd "C:\Users\admin\Desktop\Student_Browsing_History_Analysis_System"
Set-ExecutionPolicy -Scope Process Bypass
.\INSTALL_SERVICES.ps1
```

| Step | Operation Performed |
| :--- | :--- |
| **Pre-flight Checks** | Validates Python venv, `nssm.exe` in `tools\`, policy YAML, and runtime directories |
| **Sentinel Service** | Installs `app_production.py` via NSSM on port `5000` with auto-restart |
| **Firewall Rule** | Opens TCP 5000 for inbound dashboard and API access |
| **Desktop Shortcut** | Creates a one-click dashboard shortcut on the server desktop |
| **Client Agent Staging** | Copies `browseguard_agent.py` and `install_agent.bat` to network share `\\SERVER\Maintenance\MamalBrowse Sentinel\` |

---

### Server (Manual Start)

If running in interactive development mode:

```powershell
# 1. Activate Python virtual environment
.\MamalLab\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Launch production WSGI server
python app_production.py
```

---

## Configuration Reference

### `.env` Configuration

```env
# ── Server ──────────────────────────────────────────
HOST=0.0.0.0
PORT=5000
WAITRESS_THREADS=8
SECRET_KEY=mamalbrowse-sentinel-production-key

# ── Data Sources ─────────────────────────────────────
LAB_SERVER_IP=10.10.117.76
LAB_SERVER_URL=http://10.10.117.76/usage_data.json
LAB_SERVER_PATH=\\10.10.117.76\c\Lab Manager\usage_data.json
LAB_SESSION_HISTORY_PATH=\\10.10.117.76\c\Lab Manager\session_history.json
SERVER_BASE_PATH=\\10.10.117.76\StudentsBrowserHistory
BROWSING_LOGS_PATH=\\10.10.117.76\BrowsingLogs

# ── Role Passwords ───────────────────────────────────
PASSWORD_ADMIN=replace-with-secure-password
PASSWORD_LAB_TECH=replace-with-secure-password
PASSWORD_SUPERVISOR=replace-with-secure-password

# ── Email Alerts ─────────────────────────────────────
EMAIL_ENABLED=false
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_USERNAME=sentinel@ajsn.edu.qa
EMAIL_PASSWORD=app-password-here
EMAIL_RECIPIENTS=it@ajsn.edu.qa
```

---

### `config/access_policy.yaml`

This file is **hot-reloaded** automatically by Sentinel — no service restart required:

```yaml
# Session time limits by department
session_time_limits:
  darajah_1: 30     # Culture Générale (Std 1-2): 30 minutes
  darajah_3: 30     # Culture Générale (Std 3-4): 30 minutes
  darajah_5: 60     # Collegiate I (5-7): 60 minutes
  darajah_8: 120    # Collegiate II & Higher Studies (8-11): 120 minutes
  default: 60
session_warning_threshold_minutes: 5

# Categorized domain policies
categories:
  adult:
    action: BLOCK
    domains:
      - pornhub.com
      - xvideos.com
  vpn:
    action: BLOCK
    domains:
      - nordvpn.com
      - protonvpn.com
      - expressvpn.com
  social_media:
    action: WARN
    domains:
      - instagram.com
      - tiktok.com
      - facebook.com
      - twitter.com
      - x.com

whitelist_domains:
  - beta.jameasaifiyah.org
  - its52.com
  - google.com
```

---

## Client Lab PC Setup

Run on each lab workstation as Administrator:

```batch
# Specify your lab workstation tag (e.g. MK-50, RB-43)
cd client_agent
install_agent.bat MK-50

# Or run interactively (will prompt for workstation tag):
install_agent.bat
```

The installer registers a Windows Scheduled Task (`SentinelAgent`) running under the `SYSTEM` account that launches automatically on user logon.

The agent (`browseguard_agent.py`):
- Transmits authentic workstation telemetry using the configured lab station tag (e.g. `MK-50`, `RB-43`)
- Enforces URL category policies in real-time (`ALLOW`, `WARN`, `BLOCK`)
- Monitors foreground process titles and unauthorized browsers/VPNs
- Measures user idle time directly from the Windows input subsystem (`GetLastInputInfo`)
- Enforces session time limits with Windows modal dialog warnings
- **Polls the server for remote commands** — locking the station, forcing logout, or disabling the account upon admin instruction

---

## Workstation Identity Files

When a student logs in, the login system and agent maintain four identity files under `C:\ProgramData\Sentinel\`:

| File | Parameter | Example |
| :--- | :--- | :--- |
| `C:\ProgramData\Sentinel\station_name.txt` | Lab Workstation Tag | `MK-50` or `RB-43` |
| `C:\ProgramData\Sentinel\current_trno.txt` | Student TRNO | `70582` |
| `C:\ProgramData\Sentinel\current_darajah.txt` | Darajah / Class | `1 A M` |
| `C:\ProgramData\Sentinel\current_name.txt` | Full Name | `Mustafa Al-Hasan` |

---

## Student Academic & Digital Conduct Dossier (PDF Engine)

The official student report (`/report/<TRNO>`) generates an executive dossier:

```text
┌────────────────────────────────────────────────────────────────────────┐
│  AL JAMEA TUS SAIFIYAH — ACADEMIC IT DEPARTMENT & LAB SURVEILLANCE     │
│  Confidential Student Academic & Digital Conduct Dossier               │
├────────────────────────────────────────────────────────────────────────┤
│  [Photo]  Student: Mustafa Al-Hasan (#70582)   [AI Focus Score: 88/100]│
│           Dept: Collegiate I (5-7) | PC: LAB-PC-14 | Edu 72% Inst 20%  │
├────────────────────────────────────────────────────────────────────────┤
│  5 KPI RIBBON: Web Time | Avg Dwell | Academic % | Lab Hours | Policy  │
├────────────────────────────────────────────────────────────────────────┤
│  DIGITAL CATEGORY DISTRIBUTION: Segmented multi-color bar chart        │
├────────────────────────────────────────────────────────────────────────┤
│  DIAGNOSTIC BEHAVIORAL OBSERVATIONS & CURRICULAR GUIDANCE              │
├────────────────────────────────────────────────────────────────────────┤
│  WEBSITE TIME SPENT BREAKDOWN (TOP 10 SITES)                           │
│  #1 mail.google.com          ████████████   25.8h (2,368 visits)       │
│  #2 beta.jameasaifiyah.org   ████           6.0h  (382 visits)         │
├────────────────────────────────────────────────────────────────────────┤
│  MONTHLY ACADEMIC BREAKDOWN (FATEMI HIJRI CALENDAR)                    │
│  [ Rabi al-Akhar 1448 H ]  [ Rabi al-Awwal 1448 H ]  [ Safar 1448 H ] │
├────────────────────────────────────────────────────────────────────────┤
│  TRI-PARTY SIGN-OFF: Class Teacher  |  HOD  |  Lab In-Charge           │
└────────────────────────────────────────────────────────────────────────┘
```

- **Dual-Orientation Rendering:** Portrait (2-column compact) and Landscape (3-column spacious)
- **CSS Page-Break Avoidance:** `.pdf-avoid-break` and `.pdf-page-break` prevent awkward splits
- **Top 10 Sites:** Domain favicons, category pills, proportional duration bars, dwell duration, clearance certification

---

## Analytics Dashboard & Department Matrix

The Sentinel Dashboard (`/analytics`) provides institutional surveillance:

1. **KPI Strip:** Live counts — total visits, active stations, unique domains, policy clearance status
2. **Temporal Patterns:** Day-of-week distribution, hourly activity curves, shift breakdown (Morning / Afternoon / Evening)
3. **Department Compliance Snapshot:** Grouped by canonical department with clickable drill-down links
4. **Department Engagement Matrix:** Paginated matrix with search/sort across all five departments

---

## Remote Enforcement & Security

Administrators can take enforcement actions from the dashboard on any active or suspended student:

| Action | Effect | How it works |
| :--- | :--- | :--- |
| **Lock Station** | Immediately locks the workstation screen | Command queued server-side; client agent polls and executes `LockWorkStation()` |
| **Force Logout** | Logs the student out of Windows | Command queued; agent executes `logoff` or `shutdown /l` |
| **Disable Account** | Prevents future lab logins for the student | Account status set to `disabled` in `data/student_accounts.json`; agent refuses session start |
| **Enable Account** | Restores student lab access | Account status cleared; student can log in normally |
| **Block URL Category** | Blocks all domains in a category (e.g. adult, VPN) | Updated in `config/access_policy.yaml`; hot-reloaded by policy engine |

**Command delivery:** Client agents poll `/enforce/session/status?trno={trno}&station={station}` every 15 seconds. Commands remain queued until acknowledged, ensuring delivery even across brief disconnections.

---

## Role-Based Access Control

### ✅ Secure Admin Login
Admin, lab-tech, and supervisor passwords are configured via environment variables (`PASSWORD_ADMIN`, `PASSWORD_LAB_TECH`, `PASSWORD_SUPERVISOR`) in `.env`. See `.env.example` for the required variable names. Never commit `.env`.

| Role | Password Variable | Capabilities |
| :--- | :--- | :--- |
| **Admin** | `PASSWORD_ADMIN` | Full access: edit policies, extend sessions, lock/logout/disable accounts, export dossiers |
| **Lab Tech** | `PASSWORD_LAB_TECH` | Operational: session monitoring, temporary extensions, unblocking |
| **Supervisor** | `PASSWORD_SUPERVISOR` | Read-only: dashboard and audit view only |

---

## Session Time Limits & Retention Policy

**Session Time Limits (by Department):**

| Department | Limit |
| :--- | :--- |
| Culture Générale (Std 1-2) | 30 minutes |
| Culture Générale (Std 3-4) | 30 minutes |
| Collegiate I (5-7) | 60 minutes |
| Collegiate II & Higher Studies (Std 8-11) | 120 minutes |
| Dars Burhani | 60 minutes (configurable) |

- **Warning Prompt:** Windows dialog alert delivered 5 minutes before expiration
- **Enforcement:** On expiry, agent initiates forced logout or locks the station depending on configuration

**14-Day Auto-Retention Policy:**
- Suspended accounts are reviewed every hour; accounts past the 14-day window are automatically unblocked
- Audit records older than 14 days are archived into `students/violations/archive/`

---

## API & Service Reference

| Route | Method | Description |
| :--- | :--- | :--- |
| `/enforce/check` | POST | Real-time URL evaluation for client agents |
| `/enforce/session/start` | POST | Registers a new student workstation session |
| `/enforce/session/end` | POST | Ends an active session on student logout |
| `/enforce/session/status` | GET | Returns queued remote commands for a student/station |
| `/enforce/admin/lock-station` | POST | Queues a Lock Station command |
| `/enforce/admin/logout-station` | POST | Queues a Force Logout command |
| `/enforce/admin/disable-account` | POST | Disables a student account |
| `/enforce/admin/enable-account` | POST | Re-enables a disabled student account |
| `/enforce/admin/sessions-live.json` | GET | Returns all live active sessions |
| `/report/<TRNO>` | GET | Student Academic Dossier & PDF export |
| `/darajah/<name>` | GET | Class-level drill-down with student matrix |
| `/api/classes` | GET | Roster grouped by department |
| `/api/health` | GET | Service status and diagnostic health check |

---

## How to Test the System

### 1. Verify the Server is Running

```powershell
# Check service status
Get-Service MamalBrowseSentinel

# Or open the dashboard in a browser
Start-Process "http://localhost:5000"
```

Expected: Dashboard loads with KPI strip showing visit counts.

---

### 2. Health Check API

```powershell
Invoke-RestMethod -Uri "http://localhost:5000/api/health"
```

Expected response:
```json
{
  "status": "ok",
  "service": "MamalBrowse Sentinel",
  "active_sessions": 0,
  "policy_loaded": true
}
```

---

### 3. Test URL Policy Enforcement

```powershell
# BLOCK test (adult/VPN category)
$body = @{ url = "https://nordvpn.com"; trno = "99999"; station = "TEST-PC" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5000/enforce/check" -Method POST -Body $body -ContentType "application/json"
# Expected: { "action": "BLOCK", "category": "vpn" }

# ALLOW test (whitelisted domain)
$body = @{ url = "https://google.com"; trno = "99999"; station = "TEST-PC" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5000/enforce/check" -Method POST -Body $body -ContentType "application/json"
# Expected: { "action": "ALLOW" }
```

---

### 4. Test Session Registration

```powershell
# Start a test session
$body = @{ trno = "99999"; station = "TEST-PC-01"; name = "Test Student"; darajah = "5 A M" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5000/enforce/session/start" -Method POST -Body $body -ContentType "application/json"

# Verify it appears in live sessions
Invoke-RestMethod -Uri "http://localhost:5000/enforce/admin/sessions-live.json"
```

---

### 5. Test Remote Enforcement (Lock Station)

```powershell
# Queue a Lock command for the test session
$body = @{ trno = "99999"; station = "TEST-PC-01" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5000/enforce/admin/lock-station" -Method POST -Body $body -ContentType "application/json"

# Simulate what the client agent polls
Invoke-RestMethod -Uri "http://localhost:5000/enforce/session/status?trno=99999&station=TEST-PC-01"
# Expected: { "command": "lock" }
```

---

### 6. Test Account Disable/Enable

```powershell
# Disable an account
$body = @{ trno = "99999" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:5000/enforce/admin/disable-account" -Method POST -Body $body -ContentType "application/json"

# Re-enable it
Invoke-RestMethod -Uri "http://localhost:5000/enforce/admin/enable-account" -Method POST -Body $body -ContentType "application/json"
```

---

### 7. Test Department Classification

```powershell
# Check department API returns canonical names
Invoke-RestMethod -Uri "http://localhost:5000/api/classes"
# Expected: Keys contain "Collegiate I (5-7)", "Culture Générale (Std 1-2)", etc.
```

---

### 8. Test Analytics Dashboard

Open `http://localhost:5000/analytics` in a browser. Verify:
- ✅ KPI strip shows non-zero counts if sessions exist
- ✅ Department matrix shows the five canonical departments
- ✅ Shift and hourly activity charts render correctly

---

### 9. Test PDF Dossier Generation

1. Navigate to `/report/<valid_TRNO>` (replace with a real TRNO from the roster)
2. Click **Export PDF (Portrait)** — verify the PDF opens with no page cutoffs
3. Click **Landscape PDF** — verify the landscape layout renders correctly

---

### 10. Verify the Client Agent (on a Lab PC)

```powershell
# Check if the scheduled task is registered
Get-ScheduledTask -TaskName "BrowseGuardAgent"

# Check if the agent process is running
Get-Process | Where-Object { $_.MainWindowTitle -match "Sentinel" -or $_.Name -match "python" }
```

---

## Troubleshooting

| Issue | Probable Cause | Resolution |
| :--- | :--- | :--- |
| **Dashboard unreachable** | Service stopped or port conflict | Run `python app_production.py` or `Get-Service MamalBrowseSentinel` |
| **Port 5000 blocked** | Windows Firewall restriction | `New-NetFirewallRule -DisplayName "Sentinel" -Protocol TCP -LocalPort 5000 -Action Allow` |
| **"Station not resolved"** | `data/lab_manager/usage_data.json` missing or stale | Verify the SMB path in `.env` is reachable; check `LAB_SERVER_PATH` |
| **PDF blank or cut off** | Browser print scale issue | Use the dedicated Export PDF buttons (not Ctrl+P) |
| **Class/Department shows "Unknown"** | Unrecognized Darajah format in data | Verify the Darajah string matches expected patterns (`1 A M`, `5 B F`, etc.) |
| **Remote Lock command not delivered** | Client agent not running or network issue | Check `BrowseGuardAgent` scheduled task is running; verify server reachable from lab PC |
| **Account disable not respected** | Agent uses cached state | Agent re-reads `student_accounts.json` on every session start — verify the file was updated |
| **NSSM install fails** | `nssm.exe` missing from `tools\` | Download from https://nssm.cc and place `nssm.exe` in the `tools\` folder |
| **pip install fails** | venv not activated | Ensure `MamalLab\Scripts\python.exe` exists; recreate with `python -m venv MamalLab` |

---

## License & Support

MamalBrowse Sentinel is an internal institutional platform developed for Al Jamea tus Saifiyah. For support, contact the Academic IT Department.
