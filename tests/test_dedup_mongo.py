"""email_sends unique-index dedup — the REAL idempotency guarantee (integration).

The mocked digest-job test proves the claim-then-send *branch*; this proves the
thing that actually prevents double-sends in prod: the unique index on
(profile_id, kind, date). Skips when Mongo is unreachable (host); runs in-container
or with a Mongo service.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pymongo.errors import DuplicateKeyError

from app.storage.mongodb import email_sends


async def test_email_sends_unique_index_blocks_duplicate(mongo_db):
    pid = f"test-{uuid4()}"
    doc = {"profile_id": pid, "kind": "my_day", "date": "2026-07-08",
           "sent_at": datetime.now(timezone.utc)}
    col = email_sends()
    try:
        await col.insert_one(dict(doc))                     # first claim succeeds
        with pytest.raises(DuplicateKeyError):
            await col.insert_one(dict(doc))                 # same (profile,kind,date) rejected
        await col.insert_one({**doc, "date": "2026-07-09"})  # a different day is allowed
    finally:
        await col.delete_many({"profile_id": pid})
