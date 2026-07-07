"""Render an activity artifact as (subject, html_body) for email.

All dynamic content is HTML-escaped — commit messages, event titles and
AI-generated text are attacker-influenced. Add a new kind by writing a render_*
function and registering it in _RENDERERS (keep the route's _SUPPORTED in sync).
"""
import html
from datetime import datetime

_SOURCE_NAMES = {
    "github": "GitHub", "gitlab": "GitLab", "jira": "Jira",
    "teams": "Teams", "teams_subscription": "Teams",
}
_KPI_ORDER = [("github", "GitHub"), ("gitlab", "GitLab"), ("jira", "Jira"), ("teams", "Teams")]


def _wrap(title: str, body_html: str) -> str:
    return (
        '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;'
        'margin:0 auto;color:#1e1e1e">'
        f'<h2 style="color:#4f46e5;margin:0 0 12px">{html.escape(title)}</h2>'
        f'<div style="font-size:14px;line-height:1.6">{body_html}</div>'
        '<hr style="border:none;border-top:1px solid #eee;margin:20px 0">'
        '<p style="font-size:12px;color:#888">Developer Activity Tracker</p>'
        '</div>'
    )


def _nl2br(text: str) -> str:
    return html.escape(text or "").replace("\n", "<br>")


def _summary_html(text: str) -> str:
    if not text:
        return "<p style='color:#888'>No AI summary generated for this period.</p>"
    return f"<div>{_nl2br(text)}</div>"


def _kpi_html(counts: dict) -> str:
    cells = []
    for key, name in _KPI_ORDER:
        n = int(counts.get(key, 0))
        cells.append(
            '<span style="display:inline-block;margin-right:18px">'
            f'<b style="font-size:18px;color:#4f46e5">{n}</b> '
            f'<span style="color:#666">{name}</span></span>'
        )
    return f'<p style="margin:6px 0">{"".join(cells)}</p>'


def _timeline_html(events: list, limit: int = 100) -> str:
    if not events:
        return "<p style='color:#888'>No activity events.</p>"
    rows = []
    for e in events[:limit]:
        ts = e.get("occurred_at")
        tss = ts.strftime("%H:%M") if isinstance(ts, datetime) else ""
        src = _SOURCE_NAMES.get(e.get("source", ""), e.get("source", "") or "?")
        etype = (e.get("event_type") or "").replace("_", " ")
        title = (e.get("title") or "")[:120]
        rows.append(
            f"<li>[{html.escape(tss)}] <b>{html.escape(src)}</b> "
            f"{html.escape(etype)}: {html.escape(title)}</li>"
        )
    return f'<ul style="padding-left:18px;margin:4px 0">{"".join(rows)}</ul>'


def _commits_html(commits: list) -> str:
    if not commits:
        return ""
    items = "".join(
        f"<li>{html.escape((c.get('repo') or '?'))}"
        f"{('/' + html.escape(c['branch'])) if c.get('branch') else ''}: "
        f"{html.escape((c.get('message') or '')[:120])}</li>"
        for c in commits
    )
    return f"<p><b>Commits ({len(commits)}):</b></p><ul>{items}</ul>"


def _device_body(data: dict) -> str:
    fm = data.get("total_focus_min", 0)
    h, m = divmod(fm, 60)
    tools = data.get("active_tools", [])
    claude_tok = sum(
        c.get("input_tokens", 0) + c.get("output_tokens", 0)
        for c in data.get("claude_usage", [])
    )
    parts = [f"<p><b>Focus/coding time:</b> {h}h {m}m</p>"]
    if tools:
        parts.append(f"<p><b>AI tools:</b> {html.escape(', '.join(tools))}</p>")
    if claude_tok:
        parts.append(f"<p><b>Claude Code tokens:</b> {claude_tok:,}</p>")
    parts.append(_commits_html(data.get("commits", [])))
    return "".join(p for p in parts if p)


def render_standup(data: dict) -> tuple[str, str]:
    period = data.get("period") or data.get("date") or ""
    body = _nl2br(data.get("standup") or "No standup available.")
    subject = f"Your standup — {period}" if period else "Your standup"
    return subject, _wrap(f"Standup · {period}".strip(" ·"), body)


def render_device_activity(data: dict) -> tuple[str, str]:
    date = data.get("_date", "")
    body = _device_body(data) or "<p>No activity recorded.</p>"
    subject = f"Your device activity — {date}" if date else "Your device activity"
    return subject, _wrap(f"Device Activity · {date}".strip(" ·"), body)


def render_my_day(data: dict) -> tuple[str, str]:
    date = data.get("date", "")
    body = "".join([
        "<h3 style='margin:0 0 4px'>Summary</h3>", _summary_html(data.get("summary", "")),
        "<h3 style='margin:16px 0 4px'>Totals</h3>", _kpi_html(data.get("counts", {})),
        "<h3 style='margin:16px 0 4px'>Activity timeline</h3>", _timeline_html(data.get("events", [])),
    ])
    subject = f"Your day — {date}" if date else "Your day"
    return subject, _wrap(f"My Day · {date}".strip(" ·"), body)


def render_analytics(data: dict) -> tuple[str, str]:
    stats = data.get("stats", {})
    ws = data.get("week_start", "")
    layout = {
        "github": ("GitHub", [("Commits", "commits"), ("PRs", "pull_requests"), ("Issues", "issues")]),
        "gitlab": ("GitLab", [("Commits", "commits"), ("MRs", "merge_requests"), ("Issues", "issues")]),
        "jira":   ("Jira",   [("Created", "created"), ("Updated", "updated"), ("Comments", "comments")]),
        "teams":  ("Teams",  [("Messages", "messages")]),
    }
    rows = []
    for src, (name, fields) in layout.items():
        d = stats.get(src) or {}
        if not any(d.get(k, 0) for _, k in fields):
            continue
        cells = " · ".join(f"{lbl}: <b>{int(d.get(k, 0))}</b>" for lbl, k in fields)
        rows.append(f"<p>{name} — {cells}</p>")
    kpi = "".join(rows) or "<p style='color:#888'>No activity this week.</p>"

    body = "".join([
        "<h3 style='margin:0 0 4px'>Summary</h3>", _summary_html(data.get("summary", "")),
        "<h3 style='margin:16px 0 4px'>Totals</h3>", kpi,
        "<h3 style='margin:16px 0 4px'>Activity timeline</h3>", _timeline_html(data.get("events", [])),
    ])
    subject = f"Your analytics — week of {ws}" if ws else "Your analytics"
    return subject, _wrap(f"Analytics · week of {ws}".strip(" ·"), body)


_RENDERERS = {
    "standup":         render_standup,
    "device_activity": render_device_activity,
    "analytics":       render_analytics,
    "my_day":          render_my_day,
}


def render(kind: str, data: dict) -> tuple[str, str]:
    fn = _RENDERERS.get(kind)
    if not fn:
        raise ValueError(f"unknown email kind: {kind}")
    return fn(data)
