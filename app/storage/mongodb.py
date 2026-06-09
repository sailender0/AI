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
