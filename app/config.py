"""Environment-backed settings with safe development defaults."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
except ImportError:  # pragma: no cover - .env loading is a convenience
    pass


class Settings:
    app_name = os.getenv("APP_NAME", "OrderFlow")
    environment = os.getenv("ENVIRONMENT", "development").lower()
    secret_key = os.getenv("SECRET_KEY", "dev-only-change-this-key-before-deployment")
    database_url = os.getenv("DATABASE_URL", "sqlite:///./orderflow.db")
    session_https_only = os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true"
    auto_create_tables = os.getenv("AUTO_CREATE_TABLES", "true" if os.getenv("ENVIRONMENT", "development").lower() != "production" else "false").lower() == "true"
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    worker_poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "2"))
    worker_max_attempts = int(os.getenv("WORKER_MAX_ATTEMPTS", "5"))
    worker_retry_base_seconds = int(os.getenv("WORKER_RETRY_BASE_SECONDS", "3"))


settings = Settings()

if settings.environment == "production" and (
    settings.secret_key == "dev-only-change-this-key-before-deployment"
    or settings.secret_key.lower().startswith("replace-")
    or len(settings.secret_key) < 32
):
    raise RuntimeError("Set a unique SECRET_KEY of at least 32 characters in production before starting OrderFlow.")
