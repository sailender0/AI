"""Backfill orchestration tests — pager, runner counting/gating, and the
ingest() bool + dedup-race hardening. All mocked; no live API or DB.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from pymongo.errors import DuplicateKeyError

from app.backfill import paged
from app.backfill.runner import run_backfill
from app.webhooks.normalizer import ingest

PID = "22222222-2222-2222-2222-222222222222"


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


class _Client:
    """Fake paginated HTTP client keyed on the ?page param."""
    def __init__(self, pages):
        self.pages, self.pages_seen = pages, []

    async def get(self, url, headers=None, params=None):
        p = params.get("page", 1)
        self.pages_seen.append(p)
        return self.pages[p - 1] if p - 1 < len(self.pages) else _Resp(200, [])


async def test_paged_accumulates_until_short_page():
    c = _Client([_Resp(200, [{"i": n} for n in range(100)]),
                 _Resp(200, [{"i": 100}, {"i": 101}])])
    out = await paged(c, "u", {}, {})
    assert len(out) == 102
    assert c.pages_seen == [1, 2]


async def test_paged_stops_on_error():
    c = _Client([_Resp(500, None)])
    assert await paged(c, "u", {}, {}) == []


async def test_paged_respects_cap():
    c = _Client([_Resp(200, [{"i": n} for n in range(100)]) for _ in range(20)])
    out = await paged(c, "u", {}, {}, cap=3)
    assert len(out) == 300
    assert c.pages_seen == [1, 2, 3]


async def _fake_fetch(token, profile_id, since):
    _fake_fetch.since = since
    return [{"_id": "a"}, {"_id": "b"}]


async def test_run_backfill_counts_inserts(monkeypatch):
    monkeypatch.setattr("app.backfill.runner.get_valid_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("app.backfill.runner.ingest", AsyncMock(side_effect=[True, True]))
    res = await run_backfill(PID, "github", 30, fetch=_fake_fetch)
    assert res == {"source": "github", "days": 30, "fetched": 2, "inserted": 2, "deduped": 0}


async def test_run_backfill_counts_dedup(monkeypatch):
    monkeypatch.setattr("app.backfill.runner.get_valid_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("app.backfill.runner.ingest", AsyncMock(side_effect=[True, False]))
    res = await run_backfill(PID, "github", 30, fetch=_fake_fetch)
    assert res["inserted"] == 1 and res["deduped"] == 1


async def test_run_backfill_no_token(monkeypatch):
    monkeypatch.setattr("app.backfill.runner.get_valid_token", AsyncMock(return_value=None))
    res = await run_backfill(PID, "github", 30, fetch=_fake_fetch)
    assert res["error"] == "no_token"


async def test_run_backfill_unsupported_source():
    res = await run_backfill(PID, "bitbucket", 30)
    assert res["error"] == "unsupported_source"


async def test_run_backfill_clamps_days(monkeypatch):
    monkeypatch.setattr("app.backfill.runner.get_valid_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("app.backfill.runner.ingest", AsyncMock(return_value=True))
    res = await run_backfill(PID, "github", 999, fetch=_fake_fetch)
    assert res["days"] == 90
    assert 89 <= (datetime.now(timezone.utc) - _fake_fetch.since).days <= 90


_EV = {"_id": "x", "profile_id": PID, "source": "github",
       "event_type": "commit", "source_event_id": "sha1", "title": "t"}


def _infra(*, redis, col):
    s = ExitStack()
    s.enter_context(patch("app.webhooks.normalizer.get_redis", return_value=redis))
    s.enter_context(patch("app.webhooks.normalizer.activity_events", return_value=col))
    ws = s.enter_context(patch("app.ws_manager.manager"))
    ws.notify = AsyncMock()
    return s


async def test_ingest_returns_true_on_new_insert():
    redis = AsyncMock(exists=AsyncMock(return_value=0), set=AsyncMock())
    col = MagicMock(find_one=AsyncMock(return_value=None), insert_one=AsyncMock())
    with _infra(redis=redis, col=col):
        assert await ingest(dict(_EV)) is True
    col.insert_one.assert_called_once()


async def test_ingest_returns_false_on_dup():
    redis = AsyncMock(exists=AsyncMock(return_value=1), set=AsyncMock())
    col = MagicMock(find_one=AsyncMock(return_value=None), insert_one=AsyncMock())
    with _infra(redis=redis, col=col):
        assert await ingest(dict(_EV)) is False
    col.insert_one.assert_not_called()


async def test_ingest_survives_duplicate_key_race():
    redis = AsyncMock(exists=AsyncMock(return_value=0), set=AsyncMock())
    col = MagicMock(find_one=AsyncMock(return_value=None),
                    insert_one=AsyncMock(side_effect=DuplicateKeyError("dup")))
    with _infra(redis=redis, col=col):
        assert await ingest(dict(_EV)) is False
