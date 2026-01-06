from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    # Telegram (user session)
    API_ID: int
    API_HASH: str
    STRING_SESSION: str

    # Mongo
    MONGO_URI: str
    MONGODB_NAME: str

    # Collections
    CHATS_COLLECTION: str
    SCHEDULED_MESSAGES_COLLECTION: str
    DELIVERIES_COLLECTION: str

    # Timezone
    TZ_NAME: str

    # Control
    MIN_DELAY_SECONDS: float = 0.35
    SCHEDULER_POLL_SECONDS: float = 5.0
    DIALOG_SYNC_EVERY_MINUTES: int = 30

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TZ_NAME)


def load_settings() -> Settings:
    def req(name: str) -> str:
        v = os.getenv(name)
        if not v:
            raise RuntimeError(f"Missing env var: {name}")
        return v

    return Settings(
        API_ID=int(req("API_ID")),
        API_HASH=req("API_HASH"),
        STRING_SESSION=req("STRING_SESSION"),

        MONGO_URI=req("MONGO_URI"),
        MONGODB_NAME=os.getenv("MONGODB_NAME", "TelegramBot"),

        CHATS_COLLECTION=os.getenv("CHATS_COLLECTION", "chats"),
        SCHEDULED_MESSAGES_COLLECTION=os.getenv("SCHEDULED_MESSAGES_COLLECTION", "scheduled_messages"),
        DELIVERIES_COLLECTION=os.getenv("DELIVERIES_COLLECTION", "deliveries"),

        TZ_NAME=os.getenv("TZ_NAME", "America/Los_Angeles"),
    )
