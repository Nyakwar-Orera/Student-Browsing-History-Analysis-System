# app/services/alert_dispatch_service.py
"""
alert_dispatch_service.py — Multi-Channel Enterprise Alerting System.
Dispatches high-priority incident notifications to Telegram, Discord webhooks,
SMTP email, and local audit logs.

Triggers:
  - Strike 4 Warning (Pre-lockout alert)
  - Strike 5 Account Lockout & Workstation Lockdown
  - Prohibited / Rogue Application Termination (Tor, uTorrent, Opera GX, etc.)
  - Manual Administrator Suspensions
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests
import yaml

logger = logging.getLogger("alert_dispatch_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "alert_channels.yaml"
DATA_DIR = BASE_DIR / "data"
LOG_FILE = DATA_DIR / "alert_dispatch_history.jsonl"


DEFAULT_CONFIG = {
    "channels": {
        "telegram": {
            "enabled": False,
            "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
            "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        },
        "discord": {
            "enabled": False,
            "webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        },
        "email": {
            "enabled": True,
        },
        "local_audit": {
            "enabled": True,
        },
    },
    "triggers": {
        "strike_4_warning": True,
        "strike_5_lockout": True,
        "prohibited_executable": True,
        "manual_suspension": True,
    },
}


class AlertDispatchService:
    _instance: Optional[AlertDispatchService] = None
    _lock = threading.RLock()

    def __new__(cls) -> AlertDispatchService:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()
        self._initialized = True

    def _load_config(self) -> Dict[str, Any]:
        if not CONFIG_FILE.exists():
            try:
                CONFIG_FILE.write_text(yaml.dump(DEFAULT_CONFIG, default_flow_style=False), encoding="utf-8")
            except Exception:
                pass
            return DEFAULT_CONFIG.copy()
        try:
            raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            cfg = DEFAULT_CONFIG.copy()
            if "channels" in raw:
                cfg["channels"].update(raw["channels"])
            if "triggers" in raw:
                cfg["triggers"].update(raw["triggers"])
            return cfg
        except Exception as e:
            logger.warning(f"[AlertDispatch] Could not load config: {e}")
            return DEFAULT_CONFIG.copy()

    def dispatch(
        self,
        event_type: str,
        title: str,
        details: Dict[str, Any],
        severity: str = "HIGH",
    ) -> None:
        """Asynchronously dispatch an alert across all active channels."""
        threading.Thread(
            target=self._send_sync,
            args=(event_type, title, details, severity),
            daemon=True,
            name=f"MamalBrowse-Alert-{int(time.time())}"
        ).start()

    def _send_sync(
        self,
        event_type: str,
        title: str,
        details: Dict[str, Any],
        severity: str = "HIGH",
    ) -> None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "timestamp": now_str,
            "event_type": event_type,
            "severity": severity,
            "title": title,
            "details": details,
        }

        # 1. Local audit logging
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[AlertDispatch] Failed writing audit log: {e}")

        # Formatted message text
        msg_text = (
            f"🛡️ *MAMALBROWSE SENTINEL ALERT* 🛡️\n"
            f"*{title}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• *Severity:* {severity}\n"
            f"• *Student:* {details.get('student_name', 'Student')} (TRNO: `{details.get('trno', 'N/A')}`)\n"
            f"• *Darajah:* {details.get('darajah', 'General')}\n"
            f"• *Station:* `{details.get('station', 'N/A')}`\n"
            f"• *Action Taken:* {details.get('action', 'Flagged')}\n"
            f"• *Reason / Domain:* {details.get('reason') or details.get('domain') or details.get('process') or 'N/A'}\n"
            f"• *Time:* {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Al-Jamea tus-Saifiyah Institutional Surveillance_"
        )

        channels = self.config.get("channels", {})

        # 2. Telegram Dispatch
        tg = channels.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            try:
                tg_url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
                payload = {
                    "chat_id": tg["chat_id"],
                    "text": msg_text,
                    "parse_mode": "Markdown",
                }
                requests.post(tg_url, json=payload, timeout=3.5)
            except Exception as e:
                logger.debug(f"[AlertDispatch] Telegram dispatch failed: {e}")

        # 3. Discord Dispatch
        dc = channels.get("discord", {})
        if dc.get("enabled") and dc.get("webhook_url"):
            try:
                dc_color = 0xef4444 if severity == "HIGH" else 0xf59e0b
                dc_payload = {
                    "embeds": [{
                        "title": title,
                        "description": f"**Student:** {details.get('student_name')} (`{details.get('trno')}`)\n"
                                       f"**Darajah:** {details.get('darajah')} | **Station:** `{details.get('station')}`\n"
                                       f"**Action:** {details.get('action')}\n"
                                       f"**Detail:** {details.get('reason') or details.get('domain') or details.get('process')}",
                        "color": dc_color,
                        "footer": {"text": "MamalBrowse Sentinel Security Engine"},
                        "timestamp": datetime.utcnow().isoformat(),
                    }]
                }
                requests.post(dc["webhook_url"], json=dc_payload, timeout=3.5)
            except Exception as e:
                logger.debug(f"[AlertDispatch] Discord webhook dispatch failed: {e}")

    def dispatch_strike_alert(
        self,
        trno: str,
        student_name: str,
        darajah: str,
        station: str,
        domain: str,
        strike_count: int,
        max_warnings: int,
        action: str,
    ) -> None:
        """Trigger an alert for pre-lockout (Strike 4) or final lockout (Strike 5)."""
        if strike_count == max_warnings:
            title = f"🚨 ACCOUNT DISABLED — Student Exceeded {max_warnings} Policy Strikes"
            severity = "HIGH"
        else:
            title = f"⚠️ CRITICAL WARNING — Strike {strike_count} of {max_warnings}"
            severity = "MEDIUM"

        self.dispatch(
            event_type="policy_strike",
            title=title,
            details={
                "trno": trno,
                "student_name": student_name or f"Student #{trno}",
                "darajah": darajah,
                "station": station,
                "domain": domain,
                "strike_count": strike_count,
                "max_warnings": max_warnings,
                "action": action.upper(),
            },
            severity=severity,
        )

    def dispatch_executable_alert(
        self,
        trno: str,
        darajah: str,
        station: str,
        process_name: str,
    ) -> None:
        """Trigger alert when a prohibited executable is killed on a workstation."""
        self.dispatch(
            event_type="prohibited_process",
            title="🚫 Prohibited Application Force-Terminated",
            details={
                "trno": trno,
                "student_name": f"Student #{trno}",
                "darajah": darajah,
                "station": station,
                "process": process_name,
                "action": "KILLED",
            },
            severity="HIGH",
        )

    def get_recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return most recently dispatched alerts."""
        if not LOG_FILE.exists():
            return []
        items = []
        try:
            lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
            for l in reversed(lines):
                if not l.strip():
                    continue
                try:
                    items.append(json.loads(l))
                    if len(items) >= limit:
                        break
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[AlertDispatch] Could not read alert history: {e}")
        return items


alert_dispatch_service = AlertDispatchService()
