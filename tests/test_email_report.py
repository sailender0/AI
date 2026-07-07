"""Email renderers: escaping + kind dispatch (security-relevant — content is
attacker-influenced, so it must be HTML-escaped)."""
from datetime import datetime

import pytest

from app.services.email_report import render


def test_standup_render_escapes_and_labels():
    subject, html_body = render("standup", {"standup": "Did <x> & more", "period": "Friday"})
    assert "Friday" in subject
    assert "&lt;x&gt;" in html_body and "<x>" not in html_body   # escaped


def test_device_activity_render():
    subject, html_body = render("device_activity", {
        "_date": "2026-07-07", "total_focus_min": 95, "active_tools": ["claude-code"],
        "claude_usage": [{"input_tokens": 10, "output_tokens": 5}],
        "commits": [{"repo": "app", "branch": "main", "message": "fix <bug>"}],
    })
    assert "device activity" in subject and "2026-07-07" in subject
    assert "1h 35m" in html_body
    assert "&lt;bug&gt;" in html_body and "<bug>" not in html_body   # escaped


def test_my_day_render_has_summary_kpi_timeline():
    subject, html_body = render("my_day", {
        "date": "2026-07-07",
        "summary": "Shipped <feature> & tests",
        "counts": {"github": 3, "teams": 1},
        "events": [{"occurred_at": datetime(2026, 7, 7, 9, 30), "source": "github",
                    "event_type": "commit", "title": "fix <auth>"}],
    })
    assert "Summary" in html_body and "Totals" in html_body and "Activity timeline" in html_body
    assert "GitHub" in html_body and ">3<" in html_body                 # KPI count
    assert "&lt;feature&gt;" in html_body and "&lt;auth&gt;" in html_body   # escaped
    assert "Your day" in subject


def test_analytics_render_has_summary_and_timeline():
    subject, html_body = render("analytics", {
        "week_start": "2026-07-06",
        "stats": {"github": {"commits": 3, "pull_requests": 1, "issues": 0}},
        "summary": "Busy week",
        "events": [{"occurred_at": datetime(2026, 7, 6, 10, 0), "source": "gitlab",
                    "event_type": "merge_request", "title": "add feature"}],
    })
    assert "2026-07-06" in subject
    assert "GitHub" in html_body and "Summary" in html_body and "Activity timeline" in html_body
    assert "GitLab" in html_body   # from the timeline event


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        render("nope", {})
