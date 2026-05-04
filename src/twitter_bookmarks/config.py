"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the application.

    All values come from environment variables (see `.env.example`). The
    settings instance is cached via `get_settings()` so callers can rely on
    a single, immutable view of configuration for the lifetime of the
    process.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Database
    DATABASE_URL: str

    # X API
    X_CLIENT_ID: str
    X_CLIENT_SECRET: str
    X_USER_NUMERIC_ID: str = ""
    X_USERNAME: str = ""
    X_REDIRECT_URI: str = "http://localhost:8765/callback"
    X_REFRESH_TOKEN: str = ""

    # Anthropic
    ANTHROPIC_API_KEY: str

    # Resend
    RESEND_API_KEY: str = ""
    DIGEST_FROM_EMAIL: str = ""
    DIGEST_TO_EMAIL: str = ""

    # Dashboard
    DASHBOARD_USERNAME: str = "admin"
    DASHBOARD_PASSWORD: str = "admin"

    # App
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    TIMEZONE: str = "America/New_York"

    # Scheduler
    BOOKMARK_PULL_INTERVAL: int = 86400
    DIGEST_DAY_OF_WEEK: str = "mon"
    DIGEST_HOUR: int = 8

    # Classifier
    FEEDBACK_FEW_SHOT_COUNT: int = Field(default=12, ge=0)

    # Models — pinned so behavior is reproducible. To upgrade, change here.
    SONNET_MODEL: str = "claude-sonnet-4-6"
    HAIKU_MODEL: str = "claude-haiku-4-5"
    OPUS_MODEL: str = "claude-opus-4-7"

    # Classifier version string — bumped when the static prompt changes so
    # all bookmarks become eligible for reclassification.
    CLASSIFIER_VERSION: str = "v1"

    # Two-tier digest knobs.
    # A bookmark is part of a category's "baseline" view once it's older
    # than this many days; bookmarks newer than this are the rolling
    # interpretation window the model-update tier reflects on.
    BASELINE_AGE_DAYS: int = 30
    # Minimum number of older bookmarks required before we'll generate a
    # baseline view for a category. Below this, we mark the baseline thin
    # and skip the model-update tier for that category.
    MIN_BASELINE_BOOKMARKS: int = 5
    # How often to refresh each category's baseline view. Set lower for
    # fresher baselines at higher Opus cost.
    BASELINE_REFRESH_DAYS: int = 7

    # Path to refresh token file (rotated on every refresh).
    X_REFRESH_TOKEN_FILE: str = "secrets/x_refresh_token"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached `Settings` instance."""
    return Settings()  # type: ignore[call-arg]
