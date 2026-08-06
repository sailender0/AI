from datetime import date

from app.ai.insights import _shipped_phrase
from app.ai.query import _jira_due_buckets, _keys_phrase


def test_jira_due_buckets():
    today = date(2026, 7, 23)
    assigned = {"issues": [
        {"key": "AI-1", "due_date": "2026-07-20"},
        {"key": "AI-2", "due_date": "2026-07-22"},
        {"key": "AI-3", "due_date": "2026-07-23"},
        {"key": "AI-4", "due_date": "2026-07-25"},
        {"key": "AI-5", "due_date": "2026-07-30"},
        {"due_date": None},
        {},
        {"key": "AI-8", "due_date": "not-a-date"},
    ]}
    assert _jira_due_buckets(assigned, today) == (["AI-1", "AI-2"], ["AI-3", "AI-4"])
    assert _jira_due_buckets({}, today) == ([], [])


def test_keys_phrase_caps_at_three():
    assert _keys_phrase(["AI-1", "AI-2"]) == "AI-1, AI-2"
    assert _keys_phrase(["AI-1", "AI-2", "AI-3", "AI-4", "AI-5"]) == "AI-1, AI-2, AI-3 +2 more"


def test_shipped_phrase():
    assert _shipped_phrase(0, 0, 0) == ""
    assert _shipped_phrase(1, 0, 0) == "1 commit"
    assert _shipped_phrase(4, 2, 1) == "4 commits, 2 PR updates, 1 issue update"
    assert _shipped_phrase(0, 0, 3) == "3 issue updates"
