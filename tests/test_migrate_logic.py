"""
Tests for scripts/migrate.py branching logic.

These are sync tests (not async) because main() uses asyncio.run() internally.
_db_state, _create_all, and _run are patched so no real DB connection is made.
The three branches tested:
  1. Fresh DB (no alembic_version, no profiles) → create_all + stamp head
  2. Existing DB without Alembic             → upgrade head
  3. Managed DB (has alembic_version)         → upgrade head
"""
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import migrate  # noqa: E402 — must come after sys.path insert


def test_fresh_db_calls_create_all_then_stamp():
    """Fresh DB: no tables at all → create_all builds schema, then stamp to head."""
    run_calls = []

    with patch.object(migrate, "_db_state", new=AsyncMock(return_value=(False, False))), \
         patch.object(migrate, "_create_all", new=AsyncMock()) as mock_create_all, \
         patch.object(migrate, "_run", side_effect=lambda cmd: run_calls.append(cmd)):
        migrate.main()

    mock_create_all.assert_called_once()
    assert run_calls == [["alembic", "stamp", "head"]]


def test_existing_db_without_alembic_runs_upgrade():
    """Existing DB (profiles table exists, no alembic_version) → upgrade head."""
    run_calls = []

    with patch.object(migrate, "_db_state", new=AsyncMock(return_value=(False, True))), \
         patch.object(migrate, "_create_all", new=AsyncMock()) as mock_create_all, \
         patch.object(migrate, "_run", side_effect=lambda cmd: run_calls.append(cmd)):
        migrate.main()

    mock_create_all.assert_not_called()
    assert run_calls == [["alembic", "upgrade", "head"]]


def test_managed_db_runs_upgrade():
    """Managed DB (alembic_version present) → upgrade head."""
    run_calls = []

    with patch.object(migrate, "_db_state", new=AsyncMock(return_value=(True, True))), \
         patch.object(migrate, "_create_all", new=AsyncMock()) as mock_create_all, \
         patch.object(migrate, "_run", side_effect=lambda cmd: run_calls.append(cmd)):
        migrate.main()

    mock_create_all.assert_not_called()
    assert run_calls == [["alembic", "upgrade", "head"]]


def test_create_all_never_called_for_existing_db():
    """create_all must ONLY run for a truly fresh DB — never for any existing state."""
    for has_alembic, has_profiles in [(True, False), (True, True), (False, True)]:
        with patch.object(migrate, "_db_state", new=AsyncMock(return_value=(has_alembic, has_profiles))), \
             patch.object(migrate, "_create_all", new=AsyncMock()) as mock_create_all, \
             patch.object(migrate, "_run"):
            migrate.main()

        assert not mock_create_all.called, \
            f"create_all was called for state ({has_alembic=}, {has_profiles=})"


def test_upgrade_not_called_for_fresh_db():
    """For a fresh DB, upgrade head must NOT run — only stamp after create_all."""
    run_calls = []

    with patch.object(migrate, "_db_state", new=AsyncMock(return_value=(False, False))), \
         patch.object(migrate, "_create_all", new=AsyncMock()), \
         patch.object(migrate, "_run", side_effect=lambda cmd: run_calls.append(cmd)):
        migrate.main()

    assert ["alembic", "upgrade", "head"] not in run_calls
