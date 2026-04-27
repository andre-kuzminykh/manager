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

    # LLM provider
    # "auto" picks OpenAI if OPENAI_API_KEY is set, else Anthropic if
    # ANTHROPIC_API_KEY is set, else falls back to rules only.
    llm_provider: Literal["auto", "openai", "anthropic", "none"] = Field(
        default="auto", alias="LLM_PROVIDER"
    )

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    # CR-04: stronger model dedicated to the date node, which gpt-4o-mini
    # cannot handle reliably on relative phrases. Defaults to gpt-4o; set
    # to empty string to fall back to openai_model.
    openai_date_model: str = Field(default="gpt-4o", alias="OPENAI_DATE_MODEL")

    # Google
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(
        default="http://localhost:8080/oauth/google/callback",
        alias="GOOGLE_REDIRECT_URI",
    )
    google_sheets_spreadsheet_id: str = Field(default="", alias="GOOGLE_SHEETS_SPREADSHEET_ID")
    # Tab name inside the spreadsheet. Defaults to "Main" so existing
    # `Tasks` spreadsheets with a "Main" tab work out of the box.
    google_sheets_tab_name: str = Field(
        default="Main", alias="GOOGLE_SHEETS_TAB_NAME"
    )
    # Service-Account auth — alternative to OAuth. Provide ONE of:
    # - GOOGLE_SERVICE_ACCOUNT_JSON: full JSON key inline (single line);
    # - GOOGLE_SERVICE_ACCOUNT_JSON_PATH: filesystem path to the key file.
    # Service Account is the recommended path: simpler than OAuth (no
    # browser flow, no token refresh), and the bot acts as a tech account.
    # Share the spreadsheet with the service account's email as Editor.
    google_service_account_json: str = Field(
        default="", alias="GOOGLE_SERVICE_ACCOUNT_JSON"
    )
    google_service_account_json_path: str = Field(
        default="", alias="GOOGLE_SERVICE_ACCOUNT_JSON_PATH"
    )
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

    # CR-03: comma-separated Slack user ids with admin privileges.
    admin_slack_user_ids: str = Field(default="", alias="ADMIN_SLACK_USER_IDS")
    # How often the bot refreshes an employee profile from Slack (default 24h).
    employee_refresh_ttl_seconds: int = Field(
        default=86400, alias="EMPLOYEE_REFRESH_TTL_SECONDS"
    )

    # CR-01: Allowed owners — JSON list of {slack_user_id, display_name}.
    # Example: '[{"slack_user_id":"U123","display_name":"Ivan"},...]'
    allowed_owners_json: str = Field(default="[]", alias="ALLOWED_OWNERS")

    # CR-01: Workload heuristic (minutes per business day for a single owner).
    workload_minutes_per_day: int = Field(default=360, alias="WORKLOAD_MINUTES_PER_DAY")
    workload_default_task_minutes: int = Field(
        default=120, alias="WORKLOAD_DEFAULT_TASK_MINUTES"
    )

    def allowed_owners(self) -> list[dict[str, str]]:
        import json

        try:
            raw = json.loads(self.allowed_owners_json or "[]")
        except json.JSONDecodeError:
            return []
        result = []
        for entry in raw:
            sid = entry.get("slack_user_id")
            name = entry.get("display_name") or sid
            if sid:
                result.append({"slack_user_id": sid, "display_name": name})
        return result


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
