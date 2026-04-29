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
    # FR-CR-05-10 — separate spreadsheet for the Team registry
    # (cross-channel directory of who's assignable). Falls back to
    # the tasks spreadsheet when this is empty so a single-sheet
    # deploy still works (just add a `Team` tab).
    google_team_sheets_spreadsheet_id: str = Field(
        default="", alias="GOOGLE_TEAM_SHEETS_SPREADSHEET_ID"
    )
    google_team_sheets_tab_name: str = Field(
        default="Team", alias="GOOGLE_TEAM_SHEETS_TAB_NAME"
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
    # FR-CR-05-28 — listener polls both Sheets (Tasks + Team)
    # every N seconds and applies operator edits to the DB. 60s
    # default keeps the operator-edit-to-DB delay below a minute
    # without burning Sheets API quota. Set to 0 to disable
    # in-listener polling (e.g. when running an external cron
    # instead).
    sheet_poll_interval_seconds: int = Field(
        default=60, alias="SHEET_POLL_INTERVAL_SECONDS"
    )
    # FR-CR-05-35 — real-time poll of the Supabase TG message
    # view from inside the listener. When enabled, every
    # ``view_poll_interval_seconds`` the listener pulls the
    # latest ``view_poll_batch_size`` messages from the
    # ``humanoid_tg_chats_readonly`` view and runs them through
    # `prepare_drafts` + `post_draft_confirmation`, same path the
    # historical migrator uses. Already-processed messages
    # short-circuit on the per-message bookmark, so re-pulling
    # the same 50 newest each tick is cheap.
    view_realtime_enabled: bool = Field(
        default=False, alias="VIEW_REALTIME_ENABLED"
    )
    view_poll_interval_seconds: int = Field(
        default=30, alias="VIEW_POLL_INTERVAL_SECONDS"
    )
    # FR-CR-05-36 — large batch by default so a single 30-second
    # poll covers any realistic burst of new messages without
    # needing pagination logic. The view itself caps at the
    # batch size; already-processed messages short-circuit on
    # the bookmark so the work is bounded by «new since last
    # poll», not by `batch_size`.
    view_poll_batch_size: int = Field(
        default=500, alias="VIEW_POLL_BATCH_SIZE"
    )

    # FR-CR-05-39..47 — Fireflies pipeline.
    fireflies_api_token: str = Field(
        default="", alias="FIREFLIES_API_TOKEN"
    )
    fireflies_api_url: str = Field(
        default="https://api.fireflies.ai/graphql",
        alias="FIREFLIES_API_URL",
    )
    # Local volume for downloaded mp3 files. Mount this from the
    # host so audio survives container restarts.
    fireflies_audio_dir: str = Field(
        default="/app/fireflies", alias="FIREFLIES_AUDIO_DIR"
    )
    # Optional Drive folder for the detailed-summary docs. Empty
    # string ⇒ docs land in the service-account's My Drive root.
    fireflies_docs_folder_id: str = Field(
        default="", alias="FIREFLIES_DOCS_FOLDER_ID"
    )
    # Models — the user can override per cost / quality.
    fireflies_summary_model: str = Field(
        default="gpt-4o", alias="FIREFLIES_SUMMARY_MODEL"
    )
    fireflies_short_summary_model: str = Field(
        default="gpt-4o-mini", alias="FIREFLIES_SHORT_SUMMARY_MODEL"
    )
    fireflies_tasks_model: str = Field(
        default="gpt-4o-mini", alias="FIREFLIES_TASKS_MODEL"
    )
    fireflies_whisper_model: str = Field(
        default="whisper-1", alias="FIREFLIES_WHISPER_MODEL"
    )
    # Listener-side periodic poll (mirrors VIEW_REALTIME_ENABLED
    # for TG view).
    fireflies_realtime_enabled: bool = Field(
        default=False, alias="FIREFLIES_REALTIME_ENABLED"
    )
    fireflies_poll_interval_seconds: int = Field(
        default=30, alias="FIREFLIES_POLL_INTERVAL_SECONDS"
    )
    fireflies_poll_batch_size: int = Field(
        default=20, alias="FIREFLIES_POLL_BATCH_SIZE"
    )
    # Hard cap on the audio file size we'll download + Whisper
    # (Whisper API has a 25 MB request cap; meetings can run
    # longer than that as a single mp3 — we'd need to chunk in
    # that case, which is not yet implemented).
    fireflies_audio_max_bytes: int = Field(
        default=25 * 1024 * 1024,
        alias="FIREFLIES_AUDIO_MAX_BYTES",
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

    # FR-CR-04-26 — Telegram channel.
    # Token of the bot used to push messages back into Telegram.
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    # Read-only DSN for the Supabase view that holds the team's Telegram
    # message archive. The bot reads from `humanoid_tg_chats_readonly`
    # via this URL — never writes. Empty disables the Telegram channel.
    telegram_source_database_url: str = Field(
        default="", alias="TELEGRAM_SOURCE_DATABASE_URL"
    )
    # Name of the read-only Supabase view we ingest from (override only
    # if the operator has renamed it).
    telegram_source_view: str = Field(
        default="humanoid_tg_chats_readonly", alias="TELEGRAM_SOURCE_VIEW"
    )
    # Default page size for ingest reads.
    telegram_ingest_batch_size: int = Field(
        default=200, alias="TELEGRAM_INGEST_BATCH_SIZE"
    )
    # FR-CR-04-29 — comma-separated Telegram user ids with admin
    # privileges (Edit / Cancel / Delete on any task, plus admin
    # watch-list digest delivery).
    telegram_admin_user_ids: str = Field(
        default="", alias="TELEGRAM_ADMIN_USER_IDS"
    )

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
