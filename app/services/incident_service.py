# app/services/incident_service.py
"""
incident_service.py — In-memory incident queue + PDF report generator.

Powers the dashboard's live alert pop-ups and daily/weekly PDF reports.
Does NOT write to disk for alerts (that's violation_service's job).
DOES write archived PDFs to reports/archive/.
"""
from __future__ import annotations

import io
import json
import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("incident_service")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
VIOLATIONS_DIR = BASE_DIR / "students" / "violations"
REPORTS_DIR = BASE_DIR / "reports"
ARCHIVE_DIR = REPORTS_DIR / "archive"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

MAX_QUEUE_SIZE = 500


class IncidentService:
    """Thread-safe in-memory incident queue + PDF report generator."""
    _instance: Optional["IncidentService"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._lock = threading.RLock()
        self._queue: deque = deque(maxlen=MAX_QUEUE_SIZE)
        self._acknowledged_ids: set = set()
        self._initialized = True
        logger.info("[IncidentService] Initialized. Queue max=%d", MAX_QUEUE_SIZE)

    # ─────────────────────────────────────────────────────────────────
    # Live queue
    # ─────────────────────────────────────────────────────────────────
    def queue_incident(self, record: Dict[str, Any]) -> None:
        """Push a violation record onto the live dashboard queue."""
        with self._lock:
            self._queue.appendleft(record)

    def get_live(self, limit: int = 100) -> Dict[str, Any]:
        """Return unacknowledged incidents, HIGH first, then MEDIUM, then LOW."""
        with self._lock:
            live = [
                r for r in self._queue
                if r.get("incident_id") not in self._acknowledged_ids
            ]
            high = [r for r in live if r.get("severity") == "HIGH"]
            med = [r for r in live if r.get("severity") == "MEDIUM"]
            low = [r for r in live if r.get("severity") == "LOW"]

            # Within each severity bucket, newest first
            for bucket in (high, med, low):
                bucket.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)

            return {
                "incidents": (high + med + low)[:limit],
                "high_count": len(high),
                "medium_count": len(med),
                "low_count": len(low),
                "total": len(live),
                "ts": datetime.now().isoformat(timespec="seconds"),
            }

    def acknowledge(self, incident_id: str) -> bool:
        with self._lock:
            if not incident_id:
                return False
            self._acknowledged_ids.add(incident_id)
            # Cap at 5000 to prevent unbounded growth
            if len(self._acknowledged_ids) > 5000:
                self._acknowledged_ids = set(
                    list(self._acknowledged_ids)[-2500:]
                )
            return True

    def clear_acknowledged(self) -> None:
        with self._lock:
            self._acknowledged_ids.clear()

    # ─────────────────────────────────────────────────────────────────
    # PDF report generation
    # ─────────────────────────────────────────────────────────────────
    def generate_daily_pdf(self) -> bytes:
        """Generate a PDF of today's incidents. Returns bytes."""
        today = datetime.now().strftime("%Y-%m-%d")
        records = self._read_audit_file(today)
        return self._build_pdf(
            title=f"MamalBrowse Sentinel Daily Incident Report — {today}",
            subtitle=f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            records=records,
            period_label="Daily",
        )

    def generate_weekly_pdf(self) -> bytes:
        """Generate a PDF covering the last 7 days."""
        end = datetime.now().date()
        start = end - timedelta(days=6)
        all_records: List[Dict[str, Any]] = []
        d = start
        while d <= end:
            all_records.extend(self._read_audit_file(d.strftime("%Y-%m-%d")))
            d += timedelta(days=1)
        all_records.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
        return self._build_pdf(
            title=f"MamalBrowse Sentinel Weekly Incident Report — {start} to {end}",
            subtitle=f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            records=all_records,
            period_label="Weekly",
        )

    def archive_weekly_pdf(self) -> Optional[Path]:
        """Auto-archive a weekly PDF. Returns path or None on failure."""
        try:
            pdf_bytes = self.generate_weekly_pdf()
            week_tag = datetime.now().strftime("%Y_W%V")
            out = ARCHIVE_DIR / f"week_{week_tag}.pdf"
            out.write_bytes(pdf_bytes)
            logger.info(f"[IncidentService] Archived weekly report: {out}")
            return out
        except Exception as e:
            logger.error(f"[IncidentService] Weekly archive failed: {e}")
            return None

    def archive_daily_pdf(self) -> Optional[Path]:
        """Auto-archive a daily PDF. Returns path or None on failure."""
        try:
            pdf_bytes = self.generate_daily_pdf()
            day_tag = datetime.now().strftime("%Y-%m-%d")
            out = ARCHIVE_DIR / f"daily_{day_tag}.pdf"
            out.write_bytes(pdf_bytes)
            logger.info(f"[IncidentService] Archived daily report: {out}")
            return out
        except Exception as e:
            logger.error(f"[IncidentService] Daily archive failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    def _read_audit_file(self, day_str: str) -> List[Dict[str, Any]]:
        f = VIOLATIONS_DIR / f"audit_{day_str}.jsonl"
        if not f.exists():
            return []
        records = []
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.warning(f"[IncidentService] Could not read {f}: {e}")
        return records

    def _build_pdf(
        self,
        *,
        title: str,
        subtitle: str,
        records: List[Dict[str, Any]],
        period_label: str,
    ) -> bytes:
        """Build a styled PDF using reportlab."""
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.platypus import (
                SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
            )
        except ImportError:
            logger.error(
                "[IncidentService] reportlab not installed. "
                "Run: pip install reportlab>=4.0.0"
            )
            raise RuntimeError("reportlab is required for PDF reports")

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=landscape(A4),
            leftMargin=12 * mm,
            rightMargin=12 * mm,
            topMargin=14 * mm,
            bottomMargin=14 * mm,
            title=title,
        )

        styles = getSampleStyleSheet()
        h1 = ParagraphStyle(
            "H1",
            parent=styles["Heading1"],
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#1e293b"),
            spaceAfter=4,
        )
        sub = ParagraphStyle(
            "SUB",
            parent=styles["Normal"],
            fontSize=9,
            textColor=colors.HexColor("#64748b"),
            spaceAfter=12,
        )
        cell = ParagraphStyle(
            "CELL",
            parent=styles["Normal"],
            fontSize=7.5,
            leading=9,
        )
        cell_bold = ParagraphStyle(
            "CELLB",
            parent=cell,
            fontName="Helvetica-Bold",
        )

        story = [
            Paragraph(title, h1),
            Paragraph(subtitle, sub),
        ]

        high = sum(1 for r in records if r.get("severity") == "HIGH")
        med = sum(1 for r in records if r.get("severity") == "MEDIUM")
        low = sum(1 for r in records if r.get("severity") == "LOW")
        summary_style = ParagraphStyle(
            "SUM",
            parent=styles["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#0f172a"),
        )
        story.append(Paragraph(
            f"<b>Total incidents:</b> {len(records)} &nbsp;&nbsp; "
            f"<b>HIGH:</b> {high} &nbsp; "
            f"<b>MEDIUM:</b> {med} &nbsp; "
            f"<b>LOW:</b> {low}",
            summary_style,
        ))
        story.append(Spacer(1, 8))

        if not records:
            story.append(Paragraph(
                f"No {period_label.lower()} violations recorded.",
                styles["Normal"],
            ))
            doc.build(story)
            return buf.getvalue()

        header = [
            "#", "DATE & TIME", "SEVERITY", "TRNO", "STUDENT",
            "DARAJAH", "STATION", "DOMAIN", "CATEGORY", "ACTION", "HITS",
        ]
        data = [[Paragraph(f"<b>{h}</b>", cell_bold) for h in header]]

        for idx, r in enumerate(records, 1):
            sev = r.get("severity", "LOW")
            sev_color = {
                "HIGH": "#dc2626",
                "MEDIUM": "#d97706",
                "LOW": "#64748b",
            }.get(sev, "#64748b")
            data.append([
                Paragraph(str(idx), cell),
                Paragraph(
                    str(r.get("timestamp", ""))[:19].replace("T", " "),
                    cell,
                ),
                Paragraph(
                    f'<font color="{sev_color}"><b>{sev}</b></font>',
                    cell,
                ),
                Paragraph(str(r.get("trno", "")), cell),
                Paragraph(str(r.get("student_name", ""))[:40], cell),
                Paragraph(str(r.get("darajah", ""))[:18], cell),
                Paragraph(str(r.get("station", "") or "—")[:14], cell),
                Paragraph(str(r.get("domain", ""))[:40], cell),
                Paragraph(str(r.get("category", ""))[:16], cell),
                Paragraph(str(r.get("action", "")).upper()[:8], cell),
                Paragraph(str(r.get("hit_count", 1)), cell),
            ])

        col_widths = [
            8 * mm, 32 * mm, 18 * mm, 18 * mm, 40 * mm,
            22 * mm, 22 * mm, 44 * mm, 24 * mm, 18 * mm, 12 * mm,
        ]
        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                colors.white, colors.HexColor("#f8fafc"),
            ]),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
        story.append(Spacer(1, 10))
        story.append(Paragraph(
            "<i>MamalBrowse Sentinel — AJSN IT Department. "
            "This report is generated from authoritative Lab Manager &amp; Koha data.</i>",
            sub,
        ))

        doc.build(story)
        return buf.getvalue()


# Global singleton
incident_service = IncidentService()