"""
Shared fixtures. All app settings have defaults, so no env patching is needed.
This file exists to keep any future shared fixtures in one place.
"""
import asyncio
import os
import shutil
from pathlib import Path

import pytest

NODE = shutil.which("node")
TAILWIND_CLI = Path("node_modules/tailwindcss/lib/cli.js")


def _skip_or_fail(what: str, exc: Exception):
    """Skip locally when the store is unreachable, but FAIL when REQUIRE_DB is set
    (CI) — so a misconfigured integration job can't go green without running."""
    if os.environ.get("REQUIRE_DB"):
        raise RuntimeError(f"{what} required (REQUIRE_DB set) but unreachable: {exc!r}") from exc
    pytest.skip(f"{what} not reachable ({type(exc).__name__}) — integration test skipped")


def _need(missing: str | None):
    """Gate for the frontend guards — same spirit as _skip_or_fail above.

    A dev without node just skips. In CI it FAILS, because these guards exist to
    catch exactly the breakages that are otherwise silent (stale app.css; a
    global-scope clash that blanks a whole page). A guard that quietly skips in
    CI is worse than no guard — it reports green.

    GitHub Actions sets CI=true automatically.
    """
    if not missing:
        return
    if os.environ.get("CI"):
        pytest.fail(f"frontend guard cannot run in CI: {missing}")
    pytest.skip(missing)


@pytest.fixture
def node() -> str:
    """Path to the node binary, for the JS syntax/scope guards."""
    _need(None if NODE else "node is not installed")
    return NODE


@pytest.fixture
def tailwind(node) -> tuple[str, Path]:
    """(node, tailwind cli path) for the CSS build guard."""
    _need(None if TAILWIND_CLI.exists() else f"{TAILWIND_CLI} is missing (run: npm ci)")
    return node, TAILWIND_CLI


@pytest.fixture
async def pg_session():
    """Yields the real Postgres sessionmaker (AsyncSessionLocal), or skips/fails
    when the DB isn't reachable (see _skip_or_fail). On the host the compose
    'postgres' hostname doesn't resolve, so run these in-container or with a
    Postgres service. Points at whatever POSTGRES_URL resolves to — a full suite
    should target a dedicated *_test database, not dev.
    """
    from sqlalchemy import text

    from app.storage.postgres import AsyncSessionLocal, engine

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        _skip_or_fail("Postgres", exc)
    yield AsyncSessionLocal


@pytest.fixture
async def mongo_db():
    """Yields the real Mongo database with indexes ensured, or skips/fails per
    REQUIRE_DB. Points at whatever MONGODB_URL resolves to."""
    from app.storage.mongodb import get_db, init_indexes

    try:
        db = get_db()
        await asyncio.wait_for(db.command("ping"), timeout=3)   # bound the probe so host skips fast
        await init_indexes()
    except Exception as exc:
        _skip_or_fail("MongoDB", exc)
    yield db
