"""
Shared fixtures. All app settings have defaults, so no env patching is needed.
This file exists to keep any future shared fixtures in one place.
"""
import pytest


@pytest.fixture
async def pg_session():
    """Yields the real Postgres sessionmaker (AsyncSessionLocal), or SKIPS when the
    DB isn't reachable — e.g. on the host, where the compose 'postgres' hostname
    doesn't resolve. Run integration tests inside the app container or with a
    Postgres service available.

    NOTE: this points at whatever POSTGRES_URL resolves to. For a full integration
    suite, target a dedicated *_test database rather than dev.
    """
    from sqlalchemy import text

    from app.storage.postgres import AsyncSessionLocal, engine

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Postgres not reachable ({type(exc).__name__}) — integration test skipped")
    yield AsyncSessionLocal
