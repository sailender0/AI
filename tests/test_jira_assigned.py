"""Pure-mapper checks for the live 'assigned to me' endpoint (_issue_row) and
the read-time Jira field extraction (_jira_extras) — both payload shapes
(webhook-nested vs backfill-flat), offline, no fixtures.
"""
from app.routes.activity import _jira_extras
from app.routes.stats import _issue_row


def test_issue_row_maps_fields():
    issue = {"key": "PROJ-7", "fields": {
        "summary": "Fix login", "duedate": "2026-07-20",
        "created": "2026-07-01T09:00:00.000+0000",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "priority": {"name": "High"}, "issuetype": {"name": "Bug"},
        "customfield_10016": 5,
        "customfield_10020": [{"name": "Sprint 1", "state": "closed"},
                              {"name": "Sprint 2", "state": "active"}],
    }}
    assert _issue_row(issue) == {
        "key": "PROJ-7", "summary": "Fix login",
        "status": "In Progress", "status_category": "indeterminate",
        "priority": "High", "issue_type": "Bug", "due_date": "2026-07-20",
        "created": "2026-07-01T09:00:00.000+0000",
        "story_points": 5, "sprint": "Sprint 2",
    }


def test_issue_row_tolerates_missing_fields():
    row = _issue_row({"key": "PROJ-8", "fields": {"summary": "Bare"}})
    assert row["status"] == "" and row["sprint"] is None and row["story_points"] is None


def test_format_jira_live_block():
    from app.ai.query import _format_jira_live
    block = _format_jira_live({"done_7d": 4, "issues": [
        {"key": "PROJ-7", "status": "In Progress", "priority": "High",
         "due_date": "2026-07-20", "sprint": "Sprint 2", "story_points": 5,
         "summary": "Fix login"},
        {"key": "PROJ-9", "status": "To Do", "priority": "", "summary": "Bare"},
    ]})
    assert "2 open" in block and "Resolved by the user in the last 7 days: 4" in block
    assert "PROJ-7 | In Progress | High | due 2026-07-20 | Sprint 2 | 5 pts — Fix login" in block
    assert "PROJ-9 | To Do — Bare" in block


def test_jira_extras_webhook_and_backfill_shapes():
    fields = {"status": {"name": "Done"}, "priority": {"name": "Low"},
              "assignee": {"displayName": "Sai"}}
    webhook  = {"issue": {"key": "PROJ-9", "fields": fields}}   # receiver stores this shape
    backfill = {"key": "PROJ-9", "fields": fields}              # make_event stores the issue itself
    assert _jira_extras(webhook) == _jira_extras(backfill) == {
        "issue_key": "PROJ-9", "status": "Done", "priority": "Low", "assignee": "Sai",
    }
