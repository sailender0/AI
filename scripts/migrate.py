"""
Container startup script: runs database migrations safely for all DB states.

  Fresh DB (no tables)      → create_all builds the full schema, then stamp to head
  Existing without Alembic  → upgrade head applies diff migrations to existing tables
  Existing with Alembic     → upgrade head applies any new migrations (normal path)
"""
import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def _db_state() -> tuple[bool, bool]:
    """Return (has_alembic_version_table, has_profiles_table)."""
    from app.config import settings
    engine = create_async_engine(settings.POSTGRES_URL)
    try:
        async with engine.connect() as conn:
            has_alembic = bool((await conn.execute(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            )).scalar())
            has_profiles = bool((await conn.execute(
                text("SELECT to_regclass('public.profiles') IS NOT NULL")
            )).scalar())
            return has_alembic, has_profiles
    finally:
        await engine.dispose()


async def _create_all() -> None:
    from app.config import settings
    from app.storage.models import Base
    engine = create_async_engine(settings.POSTGRES_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    has_alembic, has_profiles = asyncio.run(_db_state())

    if not has_alembic and not has_profiles:
        print("[migrate] Fresh database — creating schema and stamping to head")
        asyncio.run(_create_all())
        _run(["alembic", "stamp", "head"])
    else:
        print("[migrate] Running alembic upgrade head")
        _run(["alembic", "upgrade", "head"])


if __name__ == "__main__":
    main()
