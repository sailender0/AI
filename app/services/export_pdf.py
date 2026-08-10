"""
PDF export for daily and weekly activity reports.
Uses fpdf2 (pure Python, no native deps).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.services.activity_query import event_extras

_SOURCE_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "jira": "Jira",
    "teams": "Teams",
    # The Graph connectors write their own source names; the consolidated report
    # shows them split rather than merged, so each needs its own label.
    "teams_chat": "Teams Chat",
    "teams_call": "Teams Calls",
    "outlook_mail": "Outlook Mail",
    "outlook_calendar": "Outlook Meetings",
}

_EVENT_TYPE_LABELS = {
    "commit":           "Commit",
    "pr_merged":        "PR Merged",
    "pr_opened":        "PR Opened",
    "pr_closed":        "PR Closed",
    "pr_reopened":      "PR Reopened",
    "pr_updated":       "PR Updated",
    "issue_opened":     "Issue Opened",
    "issue_updated":    "Issue Updated",
    "issue_closed":     "Issue Closed",
    "merge_request":    "Merge Request",
    "push":             "Push",
    "ticket_updated":   "Ticket Updated",
    "ticket_created":   "Ticket Created",
    "message_sent":     "Message",
    "meeting":          "Meeting",
    "comment":          "Comment",
    "pipeline":         "Pipeline",
    "unknown":          "Event",
}

_SOURCE_RGB = {
    "github": (249, 115, 22),
    "gitlab": (16, 185, 129),
    "jira": (6, 182, 212),
    "teams": (139, 92, 246),
    "teams_chat": (139, 92, 246),
    "teams_call": (139, 92, 246),
    "outlook_mail": (139, 92, 246),
    "outlook_calendar": (139, 92, 246),
}

_INDIGO = (79, 70, 229)
_DARK   = (30, 30, 30)
_MID    = (80, 80, 80)
_LIGHT  = (130, 130, 130)
_BG     = (245, 247, 250)

_NL  = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}
_NOP = {"new_x": XPos.RIGHT,   "new_y": YPos.TOP}


class _ActivityPDF(FPDF):
    def __init__(self, title_line: str):
        super().__init__()
        self._title_line = title_line
        self.set_margins(14, 14, 14)
        self.set_auto_page_break(auto=True, margin=18)

    def header(self):
        half = self.epw / 2
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*_INDIGO)
        self.cell(half, 8, "Developer Activity Tracker", **_NOP)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*_LIGHT)
        self.cell(half, 8, self._title_line, align="R", **_NL)
        self.set_draw_color(*_INDIGO)
        self.set_line_width(0.4)
        self.line(14, self.get_y(), self.w - 14, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-13)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*_LIGHT)
        self.cell(self.epw, 8, f"Page {self.page_no()}", align="C")

    def section_heading(self, text: str):
        self.set_fill_color(*_BG)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*_INDIGO)
        self.cell(self.epw, 7, f"  {text}", fill=True, **_NL)
        self.ln(1)

    def body_text(self, text: str, indent: int = 0):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*_MID)
        self.set_left_margin(14 + indent)
        self.set_x(14 + indent)
        self.multi_cell(0, 5, text)
        self.set_left_margin(14)
        self.set_x(14)

    def kpi_row(self, counts: dict):
        col_w = self.epw / 4
        y_start = self.get_y()
        sources = ["github", "gitlab", "jira", "teams"]
        for i, src in enumerate(sources):
            x = self.l_margin + i * col_w
            r, g, b = _SOURCE_RGB.get(src, (130, 130, 130))
            self.set_xy(x, y_start)
            self.set_font("Helvetica", "B", 16)
            self.set_text_color(r, g, b)
            self.cell(col_w, 9, str(counts.get(src, 0)), align="C", **_NOP)
        self.set_xy(self.l_margin, y_start + 9)
        for i, src in enumerate(sources):
            x = self.l_margin + i * col_w
            self.set_xy(x, self.get_y())
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*_LIGHT)
            self.cell(col_w, 5, _SOURCE_LABELS.get(src, src), align="C", **_NOP)
        self.ln(8)

    def stats_table(self, week_stats: dict):
        """Render per-integration stat cards in a 2×2 grid (matching the UI)."""
        _INTG = [
            ("github", "GitHub",  [("Commits", "commits"), ("Pull Requests", "pull_requests"), ("Issues", "issues")]),
            ("gitlab", "GitLab",  [("Commits", "commits"), ("Merge Requests", "merge_requests"), ("Issues", "issues")]),
            ("jira",   "Jira",    [("Created", "created"), ("Updated", "updated"), ("Comments", "comments")]),
            ("teams",  "Teams",   [("Messages", "messages")]),
        ]
        active = [(src, lbl, rows) for src, lbl, rows in _INTG
                  if any((week_stats.get(src) or {}).get(k, 0) for _, k in rows)]
        if not active:
            return

        col_w   = self.epw / 2
        padding = 3
        row_h   = 6

        for idx, (src, lbl, rows) in enumerate(active):
            data = week_stats.get(src) or {}
            col  = idx % 2
            x    = self.l_margin + col * col_w
            if col == 0 and idx > 0:
                self.ln(2)
            y = self.get_y()
            self.set_xy(x, y)

            r, g, b = _SOURCE_RGB.get(src, (130, 130, 130))
            self.set_fill_color(r, g, b)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 9)
            self.cell(col_w - padding, row_h, f"  {lbl}", fill=True,
                      new_x=XPos.RIGHT if col == 0 else XPos.LMARGIN,
                      new_y=YPos.TOP   if col == 0 else YPos.NEXT)

            card_y = y + row_h
            for stat_lbl, stat_key in rows:
                val = data.get(stat_key, 0)
                row_x = self.l_margin + col * col_w
                self.set_xy(row_x, card_y)
                self.set_fill_color(*_BG)
                self.set_font("Helvetica", "", 8)
                self.set_text_color(*_MID)
                lbl_w = (col_w - padding) * 0.65
                val_w = (col_w - padding) * 0.35
                self.cell(lbl_w, row_h, f"  {stat_lbl}", fill=True, **_NOP)
                self.set_font("Helvetica", "B", 8)
                self.set_text_color(r, g, b)
                self.cell(val_w, row_h, str(val), align="R", fill=True, **_NOP)
                card_y += row_h

            if col == 1 or idx == len(active) - 1:
                self.set_xy(self.l_margin, card_y)

        self.ln(4)


def _safe(text: str, limit: int = 120) -> str:
    """Strip non-Latin-1 chars so core PDF fonts (Helvetica) don't crash."""
    text = (text or "").strip()[:limit]
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _fmt_time(ts: Any) -> str:
    if isinstance(ts, datetime):
        s = ts.strftime("%I:%M %p")
        return s.lstrip("0") or s
    return str(ts)[:5] if ts else ""


def _event_lines(events: list) -> list:
    order = ["github", "gitlab", "jira", "teams"]
    grouped: dict = {}
    for e in events:
        src = e.get("source", "other")
        grouped.setdefault(src, []).append(e)
    result = [(src, grouped[src]) for src in order if src in grouped]
    result += [(src, evts) for src, evts in grouped.items() if src not in order]
    return result


def _write_events(pdf: _ActivityPDF, events: list):
    for src, evts in _event_lines(events):
        r, g, b = _SOURCE_RGB.get(src, (130, 130, 130))
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(r, g, b)
        pdf.set_x(pdf.l_margin)
        pdf.cell(pdf.epw, 6, _SOURCE_LABELS.get(src, src.title()), **_NL)
        for e in evts:
            ts         = _fmt_time(e.get("occurred_at"))
            event_type = e.get("event_type") or "unknown"
            type_label = _EVENT_TYPE_LABELS.get(event_type, event_type.replace("_", " ").title())
            title      = _safe(e.get("title") or "")
            workspace  = _safe(e.get("workspace") or "")
            extras     = event_extras(e.get("source", ""), e, e.get("raw_payload") or {})
            sha, files = extras["sha"], extras["files"]
            sha_str    = f"  [{sha}]" if sha else ""

            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*_MID)
            pdf.set_left_margin(18)
            pdf.set_x(18)
            pdf.multi_cell(0, 5, f"[{ts}]  [{type_label}]  {title}{sha_str}",
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            if workspace:
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_text_color(*_LIGHT)
                pdf.set_x(18)
                pdf.cell(0, 4, f"   Repo: {workspace}", **_NL)

            if files:
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_text_color(*_LIGHT)
                pdf.set_x(18)
                pdf.cell(0, 4, "   Files: " + ",  ".join(_safe(f, 60) for f in files[:4]), **_NL)

            pdf.set_left_margin(14)
            pdf.set_x(14)
        pdf.ln(2)


def _fmt_mins(m: int) -> str:
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _kpi_grid(pdf: _ActivityPDF, pairs: list[tuple[str, str]]):
    """A value-over-label strip that adapts to however many connectors are in play
    — unlike kpi_row, which is fixed to the original four."""
    if not pairs:
        return
    per_row = min(len(pairs), 5)
    col_w = pdf.epw / per_row
    for i in range(0, len(pairs), per_row):
        chunk = pairs[i:i + per_row]
        y = pdf.get_y()
        for j, (_, value) in enumerate(chunk):
            pdf.set_xy(pdf.l_margin + j * col_w, y)
            pdf.set_font("Helvetica", "B", 15)
            pdf.set_text_color(*_INDIGO)
            pdf.cell(col_w, 8, value, align="C", **_NOP)
        for j, (label, _) in enumerate(chunk):
            pdf.set_xy(pdf.l_margin + j * col_w, y + 8)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*_LIGHT)
            pdf.cell(col_w, 5, label, align="C", **_NOP)
        pdf.set_xy(pdf.l_margin, y + 14)


def _bucket_table(pdf: _ActivityPDF, bucket: str, cols: list[str],
                  buckets: list[dict], device: bool, total: int):
    """One row per bucket, one column per connector. Zero rows are kept — an empty
    day is part of what the range says."""
    head = {"day": "Day", "week": "Week", "month": "Month"}.get(bucket, "Period")
    n_num = len(cols) + (1 if device else 0) + 1
    label_w = max(38.0, pdf.epw - n_num * 22.0)
    num_w = (pdf.epw - label_w) / n_num

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_MID)
    pdf.set_fill_color(*_BG)
    pdf.set_x(pdf.l_margin)
    pdf.cell(label_w, 7, f"  {head}", fill=True, **_NOP)
    for c in cols:
        pdf.cell(num_w, 7, _SOURCE_LABELS.get(c, c), align="R", fill=True, **_NOP)
    if device:
        pdf.cell(num_w, 7, "Focus", align="R", fill=True, **_NOP)
    pdf.cell(num_w, 7, "Total", align="R", fill=True, **_NL)

    totals = {c: sum(b["counts"].get(c, 0) for b in buckets) for c in cols}
    total_mins = sum(b.get("device_minutes", 0) for b in buckets)

    pdf.set_font("Helvetica", "", 8)
    for b in buckets:
        pdf.set_x(pdf.l_margin)
        pdf.set_text_color(*_DARK)
        pdf.cell(label_w, 6, f"  {_safe(b['label'], 40)}", **_NOP)
        for c in cols:
            v = b["counts"].get(c, 0)
            pdf.set_text_color(*(_MID if v else _LIGHT))
            pdf.cell(num_w, 6, str(v), align="R", **_NOP)
        if device:
            pdf.set_text_color(*_MID)
            pdf.cell(num_w, 6, _fmt_mins(b.get("device_minutes", 0)), align="R", **_NOP)
        pdf.set_text_color(*_DARK)
        pdf.cell(num_w, 6, str(b["total"]), align="R", **_NL)

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_DARK)
    pdf.set_fill_color(*_BG)
    pdf.set_x(pdf.l_margin)
    pdf.cell(label_w, 7, "  Total", fill=True, **_NOP)
    for c in cols:
        pdf.cell(num_w, 7, str(totals[c]), align="R", fill=True, **_NOP)
    if device:
        pdf.cell(num_w, 7, _fmt_mins(total_mins), align="R", fill=True, **_NOP)
    pdf.cell(num_w, 7, str(total), align="R", fill=True, **_NL)


def generate_consolidated_pdf(*, who: str, start: str, end: str, bucket: str,
                              total: int, by_source: dict, buckets: list,
                              summary: str, detail: bool, device: bool,
                              truncated: bool = False) -> bytes:
    """The consolidated report as a document. `detail=False` (counts-only) prints
    the notice instead of a summary — the same distinction the page draws."""
    pdf = _ActivityPDF(f"Consolidated Report - {start} to {end}")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_DARK)
    pdf.cell(pdf.epw, 9, "Consolidated Activity", **_NL)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_MID)
    pdf.cell(pdf.epw, 5, _safe(who, 80), **_NL)
    pdf.set_text_color(*_LIGHT)
    pdf.cell(pdf.epw, 5, f"{start} to {end}  -  grouped by {bucket}", **_NL)
    pdf.ln(4)

    cols = sorted(by_source)
    _kpi_grid(pdf, [("Total events", str(total))]
              + [(_SOURCE_LABELS.get(c, c), str(by_source[c])) for c in cols])
    pdf.ln(2)

    if detail:
        pdf.section_heading("Summary")
        body = summary or "No summary generated for this range."
        if truncated:
            body += "\n\nBased on the most recent 200 events in this range."
        pdf.body_text(_safe(body, 6000), indent=4)
    else:
        pdf.section_heading("Summary")
        pdf.body_text("Counts only. Event names and the written summary are not "
                      "enabled for the person who ran this report.", indent=4)
    pdf.ln(4)

    if buckets and total:
        pdf.section_heading("Breakdown")
        pdf.ln(2)
        _bucket_table(pdf, bucket, cols, buckets, device, total)

    return bytes(pdf.output())


def generate_daily_pdf(date_str: str, summary_text: str, events: list) -> bytes:
    pdf = _ActivityPDF(f"Daily Report - {date_str}")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_DARK)
    pdf.cell(pdf.epw, 9, f"Daily Activity - {date_str}", **_NL)
    pdf.ln(4)

    counts = {}
    for e in events:
        src = e.get("source", "other")
        counts[src] = counts.get(src, 0) + 1
    pdf.kpi_row(counts)
    pdf.ln(2)

    pdf.section_heading("AI Summary")
    pdf.body_text(_safe(summary_text or "No summary generated for this day.", 4000), indent=4)
    pdf.ln(4)

    if events:
        pdf.section_heading("Event Details")
        pdf.ln(2)
        _write_events(pdf, events)

    return bytes(pdf.output())


def generate_weekly_pdf(week_label: str, summary_text: str, events_by_day: list,
                        total_counts: dict, week_stats: dict | None = None) -> bytes:
    pdf = _ActivityPDF(f"Weekly Report - {week_label}")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_DARK)
    pdf.cell(pdf.epw, 9, f"Weekly Activity - {week_label}", **_NL)
    pdf.ln(4)

    pdf.kpi_row(total_counts)
    pdf.ln(2)

    if week_stats:
        pdf.section_heading("Activity Breakdown")
        pdf.ln(2)
        pdf.stats_table(week_stats)

    pdf.section_heading("Weekly Summary")
    pdf.body_text(_safe(summary_text or "No summary generated for this week.", 4000), indent=4)
    pdf.ln(4)

    if any(evts for _, evts in events_by_day):
        pdf.section_heading("Daily Breakdown")
        pdf.ln(2)
        for day_label, day_events in events_by_day:
            if not day_events:
                continue
            count = len(day_events)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(*_DARK)
            pdf.set_x(pdf.l_margin)
            label_text = f"{day_label}  ({count} event{'s' if count != 1 else ''})"
            pdf.cell(pdf.epw, 7, label_text, **_NL)
            _write_events(pdf, day_events)
            pdf.ln(1)

    return bytes(pdf.output())
