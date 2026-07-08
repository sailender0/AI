"""Idempotency LOGIC of the scheduled digest job (mocked — no Mongo).

Asserts the claim-first behavior: a duplicate email_sends claim short-circuits
the send, and a fresh claim proceeds and is kept on success. The actual dedup
*guarantee* (the unique index) is an integration test for the real-DB harness.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from app.routes import email


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tzinfo=None):
        base = datetime(2026, 7, 8, 16, 0, tzinfo=timezone.utc)   # Wed 16:00 UTC
        return base.astimezone(tzinfo) if tzinfo else base


class _FakeSession:
    """Async-context session whose execute().all() yields the seeded rows."""
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return SimpleNamespace(all=lambda: self._rows)


def _due_pref_and_profile():
    pref = SimpleNamespace(frequency="daily", hour=16, weekday=0,
                           profile_id="pid", kind="my_day")
    profile = SimpleNamespace(email="u@t", timezone="UTC")   # local 16:00 == pref.hour → due
    return pref, profile


async def test_digest_job_skips_when_already_sent(monkeypatch):
    monkeypatch.setattr(email, "datetime", _FrozenDT)
    pref, profile = _due_pref_and_profile()
    sends = MagicMock()
    sends.insert_one = AsyncMock(side_effect=DuplicateKeyError("already sent today"))
    sends.delete_one = AsyncMock()
    run_mock = AsyncMock(return_value=True)

    with patch.object(email, "AsyncSessionLocal", lambda: _FakeSession([(pref, profile)])), \
         patch.object(email, "email_sends", return_value=sends), \
         patch.object(email, "_run", run_mock):
        await email.run_email_digest_job()

    run_mock.assert_not_called()          # dedup: claim collided → no second send


async def test_digest_job_sends_when_due_and_unclaimed(monkeypatch):
    monkeypatch.setattr(email, "datetime", _FrozenDT)
    pref, profile = _due_pref_and_profile()
    sends = MagicMock()
    sends.insert_one = AsyncMock()        # claim succeeds
    sends.delete_one = AsyncMock()
    run_mock = AsyncMock(return_value=True)

    with patch.object(email, "AsyncSessionLocal", lambda: _FakeSession([(pref, profile)])), \
         patch.object(email, "email_sends", return_value=sends), \
         patch.object(email, "_run", run_mock):
        await email.run_email_digest_job()

    run_mock.assert_awaited_once()
    sends.delete_one.assert_not_called()  # sent OK → claim is kept


async def test_digest_job_releases_claim_on_send_failure(monkeypatch):
    monkeypatch.setattr(email, "datetime", _FrozenDT)
    pref, profile = _due_pref_and_profile()
    sends = MagicMock()
    sends.insert_one = AsyncMock()
    sends.delete_one = AsyncMock()
    run_mock = AsyncMock(return_value=False)   # send failed

    with patch.object(email, "AsyncSessionLocal", lambda: _FakeSession([(pref, profile)])), \
         patch.object(email, "email_sends", return_value=sends), \
         patch.object(email, "_run", run_mock):
        await email.run_email_digest_job()

    sends.delete_one.assert_awaited_once()  # failure → claim released so it can retry
