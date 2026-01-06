from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Always resolve .env next to THIS file (backend/.env), no matter where uvicorn is run from
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Admin auth for the web UI + API clients
    ADMIN_TOKEN: str

    # Mongo connection
    MONGO_URI: str
    MONGODB_NAME: str = "TelegramBot"

    # Collection names
    CHATS_COLLECTION: str = "chats"
    ANNOUNCEMENTS_COLLECTION: str = "Announcements"
    SAVED_CAMPAIGNS_COLLECTION: str = "saved_campaigns"
    SCHEDULED_MESSAGES_COLLECTION: str = "scheduled_messages"
    DELIVERIES_COLLECTION: str = "deliveries"

    # Scheduling timezone (used for cron parsing / display)
    TZ_NAME: str = "America/Los_Angeles"

    # CORS for the Vue admin site
    CORS_ORIGINS: str = "http://localhost:5173"


settings = Settings()
