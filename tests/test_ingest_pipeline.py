"""
Integration-style tests: normalize() + ingest() full pipeline.

Mocks Redis, MongoDB, and the WebSocket manager — no infrastructure required.
Tests the full data path: raw webhook payload → stored MongoDB document shape.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.webhooks.normalizer import ingest, normalize

PROFILE = "profile-pipeline-123"


def _redis_miss():
    """Redis mock that reports no cached duplicate."""
    m = AsyncMock()
    m.exists = AsyncMock(return_value=0)
    m.set = AsyncMock()
    return m


def _mongo_collection(existing_doc=None):
    """MongoDB collection mock. existing_doc simulates a duplicate already in the DB."""
    col = MagicMock()
    col.find_one = AsyncMock(return_value=existing_doc)
    col.insert_one = AsyncMock()
    return col


def _patch_infra(redis=None, col=None):
    """Context manager that patches Redis, MongoDB, and the WebSocket manager."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("app.webhooks.normalizer.get_redis", return_value=redis or _redis_miss()))
    stack.enter_context(patch("app.webhooks.normalizer.activity_events", return_value=col or _mongo_collection()))
    mock_ws = stack.enter_context(patch("app.ws_manager.manager"))
    mock_ws.notify = AsyncMock()
    return stack


# ── Write path ────────────────────────────────────────────────────────────────

async def test_github_push_stored_document_shape():
    """Full pipeline for a GitHub push: verify every required field in the stored doc."""
    raw = {
        "after": "abc123def456",
        "head_commit": {"message": "Fix auth bug\n\nExtended detail"},
        "repository": {"full_name": "org/my-repo"},
        "created_at": "2026-06-23T10:00:00Z",
    }
    event = normalize(raw, source="github", profile_id=PROFILE, event_type="commit")
    col = _mongo_collection()

    with _patch_infra(col=col):
        await ingest(event)

    col.insert_one.assert_called_once()
    doc = col.insert_one.call_args[0][0]

    assert doc["profile_id"] == PROFILE
    assert doc["source"] == "github"
    assert doc["event_type"] == "commit"
    assert doc["workspace"] == "org/my-repo"
    assert "Fix auth bug" in doc["title"]
    assert doc["_id"]                          # non-empty UUID string
    assert doc["occurred_at"]                  # datetime object
    assert doc["raw_payload"] == raw


async def test_jira_stored_document_shape():
    """Full pipeline for a Jira webhook: workspace from project key, title from summary."""
    raw = {
        "webhookEvent": "jira:issue_updated",
        "user": {"accountId": "acc-1"},
        "issue": {
            "id": "10042",
            "fields": {
                "summary": "Login page broken on mobile",
                "project": {"key": "PROJ"},
                "updated": "2026-06-23T12:00:00.000+0000",
            },
        },
    }
    event = normalize(raw, source="jira", profile_id=PROFILE, event_type="jira:issue_updated")
    col = _mongo_collection()

    with _patch_infra(col=col):
        await ingest(event)

    doc = col.insert_one.call_args[0][0]
    assert doc["workspace"] == "PROJ"
    assert doc["title"] == "Login page broken on mobile"
    assert doc["source_event_id"] == "10042"
    assert doc["source"] == "jira"


async def test_gitlab_commits_produce_independent_documents():
    """Two GitLab commits produce two separate documents with distinct _id values."""
    base = {"object_kind": "push", "project": {"path_with_namespace": "group/project"}}
    col = _mongo_collection()

    with _patch_infra(col=col):
        for sha in ("sha-1", "sha-2"):
            enriched = {
                **base,
                "_commit": {"id": sha, "message": f"Commit {sha}", "timestamp": "2026-06-23T10:00:00Z"},
            }
            event = normalize(enriched, source="gitlab", profile_id=PROFILE, event_type="commit")
            await ingest(event)

    assert col.insert_one.call_count == 2
    docs = [c[0][0] for c in col.insert_one.call_args_list]
    assert docs[0]["_id"] != docs[1]["_id"]
    assert {d["source_event_id"] for d in docs} == {"sha-1", "sha-2"}


# ── Dedup path ────────────────────────────────────────────────────────────────

async def test_redis_cache_hit_blocks_insert():
    """If Redis already has the dedup key, insert_one must not be called."""
    redis_hit = AsyncMock()
    redis_hit.exists = AsyncMock(return_value=1)
    redis_hit.set = AsyncMock()

    col = _mongo_collection()
    event = normalize(
        {"after": "abc", "head_commit": {"message": "x"}, "repository": {"full_name": "org/r"}},
        source="github", profile_id=PROFILE, event_type="commit",
    )

    with _patch_infra(redis=redis_hit, col=col):
        await ingest(event)

    col.insert_one.assert_not_called()


async def test_mongodb_fallback_dedup_blocks_insert():
    """Redis miss but MongoDB finds an existing doc — insert_one must not be called."""
    existing = {"_id": "already-in-db"}
    col = _mongo_collection(existing_doc=existing)
    event = normalize(
        {"after": "abc", "head_commit": {"message": "x"}, "repository": {"full_name": "org/r"}},
        source="github", profile_id=PROFILE, event_type="commit",
    )

    with _patch_infra(col=col):
        await ingest(event)

    col.insert_one.assert_not_called()


async def test_unique_events_each_get_inserted():
    """Two events with different source_event_ids are both inserted."""
    col = _mongo_collection()

    with _patch_infra(col=col):
        for sha in ("aaa111", "bbb222"):
            event = normalize(
                {"after": sha, "head_commit": {"message": "msg"}, "repository": {"full_name": "org/r"}},
                source="github", profile_id=PROFILE, event_type="commit",
            )
            await ingest(event)

    assert col.insert_one.call_count == 2
