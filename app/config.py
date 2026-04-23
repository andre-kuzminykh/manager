from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # Slack
    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_app_token: str = Field(default="", alias="SLACK_APP_TOKEN")
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")

    # DB
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/slack_tasks",
        alias="DATABASE_URL",
    )

    # LLM
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")

    # Google
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(
        default="http://localhost:8080/oauth/google/callback",
        alias="GOOGLE_REDIRECT_URI",
    )
    google_sheets_spreadsheet_id: str = Field(default="", alias="GOOGLE_SHEETS_SPREADSHEET_ID")
    google_tasks_default_tasklist_id: str = Field(
        default="@default", alias="GOOGLE_TASKS_DEFAULT_TASKLIST_ID"
    )

    # Intent policy
    intent_confidence_high: float = Field(default=0.75, alias="INTENT_CONFIDENCE_HIGH")
    intent_confidence_low: float = Field(default=0.40, alias="INTENT_CONFIDENCE_LOW")

    # Context window
    context_window_before: int = Field(default=10, alias="CONTEXT_WINDOW_BEFORE")

    # Secrets
    secrets_encryption_key: str = Field(default="", alias="SECRETS_ENCRYPTION_KEY")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
