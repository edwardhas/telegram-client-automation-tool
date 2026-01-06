from __future__ import annotations

import logging
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING
from pymongo.errors import OperationFailure

from .settings import settings

logger = logging.getLogger("tg_automation.db")

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


def get_client() -> AsyncIOMotorClient:
    """Singleton motor client."""
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGO_URI)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    """Singleton db handle."""
    global _db
    if _db is None:
        _db = get_client()[settings.MONGODB_NAME]
    return _db


async def _drop_index_if_exists(collection, name: str) -> None:
    try:
        await collection.drop_index(name)
    except Exception:
        pass


def _same_index_spec(existing: dict, *, key: list[tuple[str, int]], unique: bool, pfe: dict | None) -> bool:
    """
    Compare index_information() output with our desired spec.
    index_information() returns dicts like:
      {"key": [("a", 1), ("b", 1)], "unique": True, "partialFilterExpression": {...}}
    """
    if existing.get("key") != key:
        return False
    if bool(existing.get("unique", False)) != bool(unique):
        return False

    existing_pfe = existing.get("partialFilterExpression")
    # Normalize None vs missing
    if pfe is None:
        return existing_pfe is None

    return existing_pfe == pfe


async def ensure_indexes() -> None:
    db = get_db()

    chats = db[settings.CHATS_COLLECTION]
    scheduled = db[settings.SCHEDULED_MESSAGES_COLLECTION]
    deliveries = db[settings.DELIVERIES_COLLECTION]
    saved = db[settings.SAVED_CAMPAIGNS_COLLECTION]

    # ---- Chats ----
    await chats.create_index([("chatId", ASCENDING)], unique=True, name="chatId_1")
    await chats.create_index(
        [("normalizedTitle", ASCENDING)],
        unique=True,
        sparse=True,
        name="normalizedTitle_1",
    )

    # ---- Scheduled messages ----
    await scheduled.create_index([("enabled", ASCENDING)], name="enabled_1")
    await scheduled.create_index([("status", ASCENDING), ("nextRunAt", ASCENDING)], name="due_1")

    # ---- Deliveries (cron-safe idempotency) ----
    # We want uniqueness per run: (scheduledId, chatId, runAt)
    desired_name = "scheduledId_chatId_runAt_uniq"
    desired_key = [("scheduledId", 1), ("chatId", 1), ("runAt", 1)]
    desired_unique = True

    # Prefer type-based partial filter (excludes nulls cleanly)
    desired_pfe = {
        "scheduledId": {"$type": "objectId"},
        "runAt": {"$type": "date"},
    }

    # Drop legacy/old index names if present
    await _drop_index_if_exists(deliveries, "scheduledId_chatId_uniq")
    await _drop_index_if_exists(deliveries, "scheduledId_chatId_runAt_uniq_sparse")

    info = await deliveries.index_information()

    # If an index with our name exists but spec differs, drop it so we can recreate cleanly
    if desired_name in info and not _same_index_spec(
        info[desired_name], key=desired_key, unique=desired_unique, pfe=desired_pfe
    ):
        logger.warning("Dropping conflicting index %s (definition mismatch).", desired_name)
        await _drop_index_if_exists(deliveries, desired_name)

    # Create the desired index (with fallbacks if provider doesn't support our partial expression)
    try:
        await deliveries.create_index(
            desired_key,
            unique=True,
            name=desired_name,
            partialFilterExpression=desired_pfe,
        )
    except OperationFailure as e:
        code = getattr(e, "code", None)

        # If there's still a name/spec conflict, drop and retry once
        if code == 86:  # IndexKeySpecsConflict
            await _drop_index_if_exists(deliveries, desired_name)
            await deliveries.create_index(
                desired_key,
                unique=True,
                name=desired_name,
                partialFilterExpression=desired_pfe,
            )
        else:
            # Fallback 1: very compatible partial ($exists only)
            logger.warning("Type-based partial index failed (%s). Falling back to $exists partial.", e)
            await _drop_index_if_exists(deliveries, desired_name)
            try:
                await deliveries.create_index(
                    desired_key,
                    unique=True,
                    name=desired_name,
                    partialFilterExpression={
                        "scheduledId": {"$exists": True},
                        "runAt": {"$exists": True},
                    },
                )
            except OperationFailure as e2:
                # Fallback 2: sparse unique index (works on many Mongo-compatible services)
                logger.warning("Partial index fallback failed (%s). Falling back to sparse unique.", e2)
                await _drop_index_if_exists(deliveries, desired_name)
                await deliveries.create_index(
                    desired_key,
                    unique=True,
                    sparse=True,
                    name="scheduledId_chatId_runAt_uniq_sparse",
                )

    # Helpful secondary indexes
    await deliveries.create_index([("scheduledId", ASCENDING), ("runAt", ASCENDING)], name="scheduledId_runAt_1")
    await deliveries.create_index([("chatId", ASCENDING), ("runAt", ASCENDING)], name="chatId_runAt_1")

    # ---- Saved campaigns ----
    await saved.create_index([("code", ASCENDING)], unique=True, name="code_1")
