"""ADR-0003 correctness anchor: a backfilled event must dedup against its live
webhook twin. For each object, assert the REST->event mapper yields the SAME
(source_event_id, event_type, title) as normalizer.normalize() does for the
equivalent webhook payload. If this drifts, backfill creates duplicates.

Pure — runs offline (no live API, no DB), unlike the fetch layer.
"""
from app.backfill import github, gitlab
from app.webhooks.normalizer import normalize

PID = "11111111-1111-1111-1111-111111111111"


def _key(ev: dict) -> tuple:
    # The dedup index is (profile_id, source, source_event_id, event_type);
    # title is asserted too so display stays consistent across both paths.
    return (ev["source_event_id"], ev["event_type"], ev["title"])


# ── GitHub ──────────────────────────────────────────────────────────────────

def test_github_commit_dedup_parity():
    sha, msg = "abc123deadbeef", "Fix the auth bug\n\nlong body here"
    webhook = {
        "repository": {"full_name": "org/repo"},
        "after": sha,                                  # webhook keys on head sha
        "head_commit": {"message": msg},
        "commits": [{"id": sha, "message": msg}],
    }
    live = normalize(webhook, "github", PID, event_type="commit")  # receiver maps push->commit
    rest = {"sha": sha, "commit": {"message": msg, "author": {"date": "2026-07-01T10:00:00Z"}}}
    assert _key(github.commit_to_event(rest, PID, "org/repo")) == _key(live)


def test_github_pr_opened_parity():
    webhook = {"action": "opened", "repository": {"full_name": "org/repo"},
               "pull_request": {"id": 555, "title": "Add feature", "merged": False}}
    live = normalize(webhook, "github", PID, event_type=None)      # normalizer derives pr_opened
    rest = {"id": 555, "title": "Add feature", "state": "open", "merged_at": None}
    back = github.pull_to_event(rest, PID, "org/repo")
    assert back["event_type"] == "pr_opened"
    assert _key(back) == _key(live)


def test_github_pr_merged_parity():
    webhook = {"action": "closed", "repository": {"full_name": "org/repo"},
               "pull_request": {"id": 555, "title": "Add feature", "merged": True}}
    live = normalize(webhook, "github", PID, event_type=None)
    rest = {"id": 555, "title": "Add feature", "state": "closed", "merged_at": "2026-07-02T09:00:00Z"}
    back = github.pull_to_event(rest, PID, "org/repo")
    assert back["event_type"] == "pr_merged"
    assert _key(back) == _key(live)


def test_github_issue_parity():
    webhook = {"repository": {"full_name": "org/repo"},
               "issue": {"id": 777, "title": "Broken link"}}
    live = normalize(webhook, "github", PID, event_type="issue_updated")  # receiver maps issues->issue_updated
    rest = {"id": 777, "title": "Broken link", "state": "open"}
    assert not github.is_pull_request(rest)
    assert _key(github.issue_to_event(rest, PID, "org/repo")) == _key(live)


def test_github_issues_list_flags_prs():
    # /issues returns PRs too; they must route to pull_to_event, not issue.
    assert github.is_pull_request({"id": 1, "pull_request": {"url": "..."}})


# ── GitLab ──────────────────────────────────────────────────────────────────

def test_gitlab_commit_parity():
    sha, msg = "deadbeefcafe", "Initial commit\n\ndetails"
    enriched = {"object_kind": "push", "project": {"path_with_namespace": "grp/proj"},
                "_commit": {"id": sha, "message": msg}}
    live = normalize(enriched, "gitlab", PID, event_type="commit")
    rest = {"id": sha, "title": "Initial commit", "message": msg, "created_at": "2026-07-01T10:00:00Z"}
    assert _key(gitlab.commit_to_event(rest, PID, "grp/proj")) == _key(live)


def test_gitlab_mr_parity():
    webhook = {"object_kind": "merge_request", "project": {"path_with_namespace": "grp/proj"},
               "object_attributes": {"id": 900, "iid": 3, "title": "My MR"}}
    live = normalize(webhook, "gitlab", PID, event_type=None)
    rest = {"id": 900, "iid": 3, "title": "My MR", "updated_at": "2026-07-01T11:00:00Z"}
    back = gitlab.mr_to_event(rest, PID, "grp/proj")
    assert back["event_type"] == "merge_request"
    assert _key(back) == _key(live)          # global id, not iid


def test_gitlab_issue_parity():
    webhook = {"object_kind": "issue", "project": {"path_with_namespace": "grp/proj"},
               "object_attributes": {"id": 950, "iid": 7, "title": "A bug"}}
    live = normalize(webhook, "gitlab", PID, event_type=None)
    rest = {"id": 950, "iid": 7, "title": "A bug", "updated_at": "2026-07-01T11:00:00Z"}
    assert _key(gitlab.issue_to_event(rest, PID, "grp/proj")) == _key(live)
