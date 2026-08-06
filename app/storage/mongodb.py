from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGODB_URL)
    return _client


def get_db():
    return get_client()[settings.MONGODB_DB]


def activity_events():
    return get_db()["activity_events"]


def device_heartbeats():
    return get_db()["device_heartbeats"]


def local_commits():
    return get_db()["local_commits"]


def ai_tool_events():
    return get_db()["ai_tool_events"]


def claude_usage():
    return get_db()["claude_usage"]


def vscode_extensions():
    return get_db()["vscode_extensions"]


def tool_preferences():
    return get_db()["tool_preferences"]


def week_summaries():
    return get_db()["week_summaries"]


def standups():
    return get_db()["standups"]


def email_sends():
    return get_db()["email_sends"]


def access_log():
    """Audit trail for cross-user report access (supervisor/admin)."""
    return get_db()["access_log"]


async def purge_profile(profile_id: str) -> dict[str, int]:
    """Delete every per-profile document for a removed user. Postgres rows go via
    the ORM cascade; this is the Mongo half, which has no foreign keys.

    access_log is deliberately NOT purged — it is the audit trail of who accessed
    what, and must outlive the account it refers to.
    """
    collections = [
        activity_events(), device_heartbeats(), local_commits(), ai_tool_events(),
        claude_usage(), vscode_extensions(), tool_preferences(), week_summaries(),
        standups(), email_sends(),
    ]
    deleted = {}
    for col in collections:
        result = await col.delete_many({"profile_id": profile_id})
        if result.deleted_count:
            deleted[col.name] = result.deleted_count
    return deleted


async def init_indexes():
    col = activity_events()
    await col.create_index([("profile_id", 1), ("occurred_at", -1)])
    await col.create_index([("profile_id", 1), ("source", 1), ("event_type", 1)])
    await col.create_index([("profile_id", 1), ("due_date", 1)])
    await col.create_index(
        [("profile_id", 1), ("source", 1), ("source_event_id", 1)],
        unique=True,
        sparse=True,
    )

    await device_heartbeats().create_index([("profile_id", 1), ("timestamp", -1)])
    await device_heartbeats().create_index([("profile_id", 1), ("device_id", 1), ("timestamp", -1)])

    await local_commits().create_index([("profile_id", 1), ("timestamp", -1)])
    await local_commits().create_index(
        [("profile_id", 1), ("sha", 1)], unique=True, sparse=True
    )

    await ai_tool_events().create_index([("profile_id", 1), ("timestamp", -1)])
    await claude_usage().create_index([("profile_id", 1), ("date", 1), ("model", 1), ("repo", 1)], unique=True)
    await week_summaries().create_index(
        [("profile_id", 1), ("week_start", 1)], unique=True
    )
    await email_sends().create_index(
        [("profile_id", 1), ("kind", 1), ("date", 1)], unique=True
    )

    await access_log().create_index([("target_profile_id", 1), ("at", -1)])
    await access_log().create_index([("actor_profile_id", 1), ("at", -1)])
