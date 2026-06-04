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
    # FR-CR-05-137 — when set, the meeting pipeline mirrors the
    # short-summary DM to this Slack channel (e.g. Artem AI's
    # bot-DM `D0AUXKND35Y`). Empty disables the mirror.
    slack_meeting_channel_id: str = Field(
        default="", alias="SLACK_MEETING_CHANNEL_ID"
    )
    # FR-CR-05-160 — n8n webhook for meeting summaries. Same body
    # that lands in Slack (short_summary + metadata) is POST'ed
    # JSON to this URL right after slack_mirror succeeds. Empty
    # URL → webhook disabled (no-op).
    meeting_webhook_url: str = Field(
        default="", alias="MEETING_WEBHOOK_URL"
    )
    # FR-CR-05-161 — watchdog + hard timeout, safety net против
    # stuck-socket hang. Если listener tick не отрабатывает дольше
    # этого окна — daemon thread убивает процесс, Docker revives.
    watchdog_max_silence_seconds: int = Field(
        default=600, alias="WATCHDOG_MAX_SILENCE_SECONDS"
    )
    # Hard timeout на _step_post_task_cards (ThreadPool). pool.map()
    # без timeout зависает навсегда если хотя бы один worker на
    # dead TCP socket; concurrent.futures.wait(timeout=N) выходит
    # через N seconds, cancel'ит stuck workers.
    zoom_post_task_cards_timeout_seconds: int = Field(
        default=120, alias="ZOOM_POST_TASK_CARDS_TIMEOUT_SECONDS"
    )
    # FR-CR-05-162 — Slack Socket-Mode ingest. По умолчанию OFF —
    # включается через env когда оператор готов и Slack App scopes
    # перенастроены (channels:history, groups:history, etc).
    slack_ingest_enabled: bool = Field(
        default=False, alias="SLACK_INGEST_ENABLED"
    )

    # FR-CR-05-168 — Pre-meeting counterparty briefs. Event-trigger:
    # when a new Calendar event appears with an external
    # counterparty, the runner generates a brief Google Doc (org +
    # per-beneficiary person Docs) and posts ONE grouped DM to Slack.
    # Default OFF. See SPEC_COUNTERPARTY_BRIEFS_v0.1.md.
    counterparty_briefs_enabled: bool = Field(
        default=False, alias="COUNTERPARTY_BRIEFS_ENABLED"
    )
    counterparty_briefs_slack_target_channel_id: str = Field(
        default="", alias="COUNTERPARTY_BRIEFS_SLACK_TARGET_CHANNEL_ID"
    )
    counterparty_briefs_lookahead_days: int = Field(
        default=7, alias="COUNTERPARTY_BRIEFS_LOOKAHEAD_DAYS"
    )
    counterparty_briefs_tick_interval_seconds: int = Field(
        default=1800, alias="COUNTERPARTY_BRIEFS_TICK_INTERVAL_SECONDS"
    )
    counterparty_briefs_llm_budget_usd: float = Field(
        default=2.0, alias="COUNTERPARTY_BRIEFS_LLM_BUDGET_USD"
    )
    counterparty_briefs_cache_ttl_days: int = Field(
        default=180, alias="COUNTERPARTY_BRIEFS_CACHE_TTL_DAYS"
    )
    counterparty_briefs_max_beneficiaries: int = Field(
        default=5, alias="COUNTERPARTY_BRIEFS_MAX_BENEFICIARIES"
    )
    counterparty_briefs_research_model: str = Field(
        default="o4-mini-deep-research",
        alias="COUNTERPARTY_BRIEFS_RESEARCH_MODEL",
    )
    counterparty_briefs_extract_model: str = Field(
        default="", alias="COUNTERPARTY_BRIEFS_EXTRACT_MODEL"
    )

    # FR-CB2-200 — CEO Brain Bot. Slack-app that (a) archives every
    # message in channels where it's a member into
    # `slack-archive/<channel>/YYYY-MM-DD.jsonl` + PG mirror, and
    # (b) responds to @mention / DM via Anthropic Claude API with
    # access to the operator's claude.ai MCP connectors.
    #
    # Default OFF. Turning on without `CEO_BRAIN_ANTHROPIC_API_KEY`
    # starts archive only (responder refuses to start).
    ceo_brain_enabled: bool = Field(
        default=False, alias="CEO_BRAIN_ENABLED",
    )
    ceo_brain_archive_only: bool = Field(
        default=False, alias="CEO_BRAIN_ARCHIVE_ONLY",
    )
    ceo_brain_anthropic_api_key: str = Field(
        default="", alias="CEO_BRAIN_ANTHROPIC_API_KEY",
    )
    ceo_brain_model: str = Field(
        default="claude-sonnet-4-6", alias="CEO_BRAIN_MODEL",
    )
    ceo_brain_slack_app_token: str = Field(
        default="", alias="CEO_BRAIN_SLACK_APP_TOKEN",
    )
    ceo_brain_slack_bot_token: str = Field(
        default="", alias="CEO_BRAIN_SLACK_BOT_TOKEN",
    )
    ceo_brain_signing_secret: str = Field(
        default="", alias="CEO_BRAIN_SIGNING_SECRET",
    )
    ceo_brain_archive_dir: str = Field(
        default="/var/lib/manager/slack-archive",
        alias="CEO_BRAIN_ARCHIVE_DIR",
    )
    ceo_brain_archive_channels: str = Field(
        default="", alias="CEO_BRAIN_ARCHIVE_CHANNELS",
    )
    ceo_brain_thread_context_msgs: int = Field(
        default=10, alias="CEO_BRAIN_THREAD_CONTEXT_MSGS",
    )
    ceo_brain_max_run_cost_usd: float = Field(
        default=1.0, alias="CEO_BRAIN_MAX_RUN_COST_USD",
    )
    ceo_brain_jsonl_retention_days: int = Field(
        default=365, alias="CEO_BRAIN_JSONL_RETENTION_DAYS",
    )
    ceo_brain_mcp_servers: str = Field(
        default="", alias="MCP_SERVERS",
    )
    # User OAuth token (xoxp-…) — required for `search.messages`
    # since bot tokens cannot search. Optional; if absent the
    # `slack_search` local tool returns a "search disabled" error
    # but other Slack tools still work via the bot token.
    ceo_brain_slack_user_token: str = Field(
        default="", alias="CEO_BRAIN_SLACK_USER_TOKEN",
    )
    # FR-CB2-3.36 — comma-separated list of Slack user IDs that
    # ARE allowed to talk to the CEO Brain bot (DMs, @mentions).
    # Empty (default) = no restriction, everyone can DM. When set,
    # messages from non-listed users are silently ignored (no
    # «access denied» reply — operator-pinned to avoid noise).
    ceo_brain_allowed_users: str = Field(
        default="", alias="CEO_BRAIN_ALLOWED_USERS",
    )
    # FR-CR-05-170 — bilingual transcript restoration applied during
    # Zoom ingestion (in `_step_transcribe`, right after Whisper).
    # When enabled, every fresh transcript goes through:
    #   (a) detector — does it look like a bilingual call with garbled
    #       English segments?
    #   (b) second Whisper pass with `language="en"` on the SAME audio;
    #   (c) reconciler LLM merges Russian + English passes into ONE
    #       canonical transcript stored on `row.transcript_text`.
    # All downstream steps (detailed_summary, short_summary, tasks,
    # Doc export, CEO Brain MCP queries) consume the restored text.
    # Default OFF — operator first verifies via `ops/bilingual_smoke`
    # then flips this on.
    zoom_bilingual_restoration_enabled: bool = Field(
        default=False,
        alias="ZOOM_BILINGUAL_RESTORATION_ENABLED",
    )
    # Model id for the bilingual detector (fast/cheap binary call).
    zoom_bilingual_detector_model: str = Field(
        default="gpt-4o-mini",
        alias="ZOOM_BILINGUAL_DETECTOR_MODEL",
    )
    # Model id for the bilingual reconciler (merges two transcripts).
    zoom_bilingual_reconciler_model: str = Field(
        default="gpt-4o",
        alias="ZOOM_BILINGUAL_RECONCILER_MODEL",
    )
    # Whisper model id for the second STT pass (operator-pinned:
    # «тот же STT что и брал, но язык англ» — reuses
    # `app.services.transcription.transcribe_bytes` with
    # `language="en"`, same OpenAI key as the primary Whisper pass).
    zoom_bilingual_whisper_model: str = Field(
        default="whisper-1",
        alias="ZOOM_BILINGUAL_WHISPER_MODEL",
    )
    # FR-CR-05-186 — operator-pinned 2026-05-21: when the primary
    # STT model output trips the hallucination heuristic AND the
    # Zoom-side VTT fallback is empty/missing, retry the
    # transcribe with `ZOOM_FALLBACK_WHISPER_MODEL` (default
    # `whisper-1` — legacy model with different silent-section
    # behaviour). Keep the fallback transcript ONLY if it doesn't
    # itself trip the heuristic.
    zoom_fallback_whisper_enabled: bool = Field(
        default=True,
        alias="ZOOM_FALLBACK_WHISPER_ENABLED",
    )
    zoom_fallback_whisper_model: str = Field(
        default="whisper-1",
        alias="ZOOM_FALLBACK_WHISPER_MODEL",
    )
    # Chunked reconciler — split both transcripts into batches of this
    # many input chars so the LLM always covers the full primary
    # regardless of length. Smaller = more batches, more cost /
    # latency, but each batch fits comfortably in the context window.
    # 30k chars ≈ 7.5K tokens of input — leaves room for system prompt
    # and a full 16K-token output.
    zoom_bilingual_reconcile_batch_input_chars: int = Field(
        default=30_000,
        alias="ZOOM_BILINGUAL_RECONCILE_BATCH_INPUT_CHARS",
    )
    # Max output tokens per reconciler batch. 16,384 = gpt-4o's
    # single-response cap; lower it only when you intentionally want
    # a shorter, summarised output per batch.
    zoom_bilingual_reconcile_max_tokens_per_batch: int = Field(
        default=16_384,
        alias="ZOOM_BILINGUAL_RECONCILE_MAX_TOKENS_PER_BATCH",
    )

    # FR-CR-05-165 — Pre-meeting agenda. За N минут до повторяющейся
    # встречи в Google Calendar (определяется по совпадению title с
    # ≥1 прошлой записанной встречи из zoom_recordings) бот собирает
    # повестку: что обсуждали прошлый раз + открытые задачи + открытые
    # вопросы → отправляет в Slack DM оператору с гиперссылкой на
    # подробный Google Doc.
    #
    # Default OFF. Включается только когда:
    #   - GOOGLE_CALENDAR_ID настроен (multi-calendar OK)
    #   - GOOGLE_SERVICE_ACCOUNT_JSON_PATH доступен (Calendar API)
    #   - AGENDA_SLACK_TARGET_CHANNEL_ID непустой (куда слать DM)
    agenda_enabled: bool = Field(
        default=False, alias="AGENDA_ENABLED"
    )
    # Сколько минут до начала встречи отправлять повестку.
    agenda_lead_time_minutes: int = Field(
        default=10, alias="AGENDA_LEAD_TIME_MINUTES"
    )
    # Окно ±N минут вокруг target-time, чтобы не пропустить event если
    # tick опоздал. Default 1 — runner тикает раз в 60s, плюс это окно
    # = no-miss даже при минутной задержке.
    agenda_window_minutes: int = Field(
        default=1, alias="AGENDA_WINDOW_MINUTES"
    )
    # Сколько прошлых встреч искать для контекста (минимум 1 чтобы
    # считать встречу «повторяющейся»).
    agenda_lookback_days: int = Field(
        default=90, alias="AGENDA_LOOKBACK_DAYS"
    )
    # Минимум прошлых recordings с тем же title чтобы считать встречу
    # повторяющейся. Default 2 (operator-pinned 2026-05-14: «EQT
    # Group <> Humanoid - почему вообще выводится? разве такое было
    # регулярно?») — intro/one-off встречи с min=1 попадали в окно
    # с одной prior записью и слали повестку. 2 = ждать пока серия
    # реально подтвердится.
    agenda_min_prior_meetings: int = Field(
        default=2, alias="AGENDA_MIN_PRIOR_MEETINGS"
    )
    # Slack channel / DM куда отправлять повестку. Это conversation id
    # типа D0ASY5QF6UX (DM с оператором) или C0... (channel).
    agenda_slack_target_channel_id: str = Field(
        default="", alias="AGENDA_SLACK_TARGET_CHANNEL_ID"
    )
    # Интервал тика runner'a в секундах. Меньше = больше шанс попасть
    # точно в lead-time, но больше calendar API quota. Default 60s.
    agenda_tick_interval_seconds: int = Field(
        default=60, alias="AGENDA_TICK_INTERVAL_SECONDS"
    )
    # OpenAI model для compose шага. Дефолт — текущий settings.openai_model.
    # Можно переопределить если хочется дешевле/умнее.
    agenda_compose_model: str = Field(
        default="", alias="AGENDA_COMPOSE_MODEL"
    )
    # FR-CR-05-167 — Slack bot token для отправки повесток.
    # Optional override: если задан, runner шлёт `chat.postMessage`
    # через этот токен, а не через основной `SLACK_BOT_TOKEN`. Полезно
    # когда:
    #   - В manager/.env основной токен от старого Slack App,
    #     а DM (`AGENDA_SLACK_TARGET_CHANNEL_ID`) открыт с
    #     новым App (FR-CR-05-162 slack ingest token).
    #   - Хочется отделить identity бота, который шлёт повестки,
    #     от identity того, что обрабатывает task-команды.
    # Пусто (default) → fallback на settings.slack_bot_token.
    agenda_slack_bot_token: str = Field(
        default="", alias="AGENDA_SLACK_BOT_TOKEN"
    )
    # FR-CR-05-166 — Источник «upcoming meetings».
    #
    #   - "calendar"      — Google Calendar API (FR-CR-05-165) либо OAuth,
    #                       либо Service Account (read access на календарь).
    #                       Требует enable Calendar API + share с SA.
    #                       Самый точный (видит ad-hoc сдвиги/cancel).
    #   - "zoom_pattern"  — heuristic: парсим zoom_recordings, ищем
    #                       weekly-pattern (одинаковый title + delta 7 ± 1
    #                       день между instance'ами + один weekday+time),
    #                       предсказываем next = last + 7 days.
    #                       Не требует Calendar API. Слабость: если ты
    #                       сдвинул встречу в Calendar — heuristic не
    #                       знает.
    agenda_source: str = Field(
        default="calendar", alias="AGENDA_SOURCE"
    )

    # FR-CR-05-136 — Calendar-match for meeting titles. The
    # Apps Script Web App is the proxy that does the Calendar
    # read with operator-level OAuth (no service account /
    # billing required). Python pipeline GETs events in a
    # ±N-minute window, runs an LLM-pass to pick the best match,
    # rewrites the meeting title to «DD/MM - <calendar title>».
    calendar_match_enabled: bool = Field(
        default=False, alias="CALENDAR_MATCH_ENABLED"
    )
    calendar_match_window_minutes: int = Field(
        default=30, alias="CALENDAR_MATCH_WINDOW_MINUTES"
    )
    calendar_apps_script_url: str = Field(
        default="", alias="CALENDAR_APPS_SCRIPT_URL"
    )
    calendar_apps_script_shared_token: str = Field(
        default="", alias="CALENDAR_APPS_SCRIPT_SHARED_TOKEN"
    )

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
    # FR-CR-05-104 — operator chose gpt-5.5 (released
    # 2026-Q1, https://openai.com/index/introducing-gpt-5-5/).
    # Override via OPENAI_MODEL=gpt-4o if the key doesn't
    # have access yet.
    openai_model: str = Field(default="gpt-5.5", alias="OPENAI_MODEL")
    # FR-CR-05-204 — cost: date parse doesn't need gpt-5.5 reasoning → gpt-4o.
    openai_date_model: str = Field(default="gpt-4o", alias="OPENAI_DATE_MODEL")
    # FR-CR-05-214 — title/description need a stronger model than the
    # gpt-4o-mini intent stage: mini hallucinated names («Игорь попросил
    # Ирину» where the source only said «Ир …»). gpt-4o for clean,
    # faithful title+description; intent detection stays on openai_model.
    openai_title_model: str = Field(default="gpt-4o", alias="OPENAI_TITLE_MODEL")
    # FR-CR-05-204 — cost: dedup is semantic; gpt-4o-mini misses near-dups
    # (FR-CR-05-102), so gpt-4o (NOT mini) — still much cheaper than gpt-5.5.
    openai_dedup_model: str = Field(default="gpt-4o", alias="OPENAI_DEDUP_MODEL")
    # FR-CR-05-220/221 — directory entity embeddings (pgvector
    # entity-matching epic). 3072-dim model; see entity_embeddings.
    embedding_model: str = Field(
        default="text-embedding-3-large", alias="EMBEDDING_MODEL"
    )
    # FR-CR-05-225 — entity-match critic. Per-mention LLM that picks the
    # right directory entity from the pgvector top-K. gpt-4o (NOT gpt-5.5):
    # choosing among ~10 candidates doesn't need gpt-5.5 reasoning, and
    # per-mention fan-out makes a cheaper/faster model matter.
    entity_match_critic_model: str = Field(
        default="gpt-4o", alias="ENTITY_MATCH_CRITIC_MODEL"
    )
    # FR-CR-05-230 — auto-enrollment kill-switch. The 3 enrollment paths
    # (summary canonicalize + 2 enrollment services) used to MINT a new
    # Counterparty row whenever a mention didn't match an existing
    # name_normalised exactly. Garbled Whisper variants («Тезером»,
    # «миры») never match → the directory accumulated alias/dup cards
    # (9 «Tether» rows!). Default OFF: unknown mentions are NOT auto-
    # created; the directory changes ONLY via the Google-Sheet sync.
    counterparty_autoenroll_enabled: bool = Field(
        default=False, alias="COUNTERPARTY_AUTOENROLL_ENABLED"
    )
    # FR-CR-05-241 — vector counterparty resolution (v2) rollout switch.
    # v2 = extract (gpt-5.5 ×1) → pgvector top-K over the NEW 4000-entity
    # catalog (entity_embeddings kind='catalog') → gpt-4o critic → match |
    # review-queue. Modes:
    #   "off"    — v1 whole-directory resolver only (current prod behaviour).
    #   "shadow" — v1 stays canonical; ADDITIONALLY run v2 and LOG how it
    #              would differ (counts + samples). Zero writes, zero risk.
    #   "on"     — RESERVED for the cutover (needs prod pgvector + migrations
    #              0037/0038 + catalog load + id-space mapping; do NOT set
    #              until that maintenance window).
    # shadow/on require prod-side pgvector, which is NOT yet deployed — keep
    # "off" until the infra runbook (FR-CR-05-241) is done.
    counterparty_match_v2_mode: str = Field(
        default="off", alias="COUNTERPARTY_MATCH_V2_MODE"
    )
    # Retrieval width for v2 (operator 2026-06-01: «дефолт k 12→20»).
    counterparty_match_v2_k: int = Field(
        default=20, alias="COUNTERPARTY_MATCH_V2_K"
    )
    # FR-CR-05-241 — the vector catalog (entity_catalog_staging +
    # entity_embeddings, kind='catalog') lives in a SEPARATE pgvector DB so
    # the prod transactional DB is never touched (operator decision
    # 2026-06-01: «отдельный pgvector-инстанс»). When set, the shadow hook /
    # v2 read the catalog via this DSN; when None they fall back to the
    # primary DB (the ops sidecar case, where catalog + app share one DB).
    catalog_database_url: str | None = Field(
        default=None, alias="CATALOG_DATABASE_URL"
    )
    # FR-TV — Task Vector layer (semantic task search / NL field updates via
    # the CEO-brain agent + external MCP server). All default-OFF; the enable
    # flag is flipped LAST, after indexing + synthetic calibration (operator
    # decision 2026-06-01 «фича флаг в конце»). See docs/SPEC_TASK_VECTOR_v0.1.md.
    task_vector_enabled: bool = Field(
        default=False, alias="TASK_VECTOR_ENABLED"
    )
    # FR-TV — second gate, INSIDE task_vector_enabled. When False (default) only
    # the read tools (search_tasks/get_task/resolve_person) are exposed; the
    # three update_* writers are omitted entirely. Stays off until the τ/δ
    # confidence gate is calibrated, so search/Q&A can go live first.
    task_vector_writes_enabled: bool = Field(
        default=False, alias="TASK_VECTOR_WRITES_ENABLED"
    )
    # Tasks + team are indexed (kind='task'/'team_member'/'employee') in a
    # SEPARATE pgvector DB so the prod transactional DB is never touched. When
    # None, falls back to CATALOG_DATABASE_URL, then the primary DB.
    task_vector_database_url: str | None = Field(
        default=None, alias="TASK_VECTOR_DATABASE_URL"
    )
    task_vector_model: str = Field(
        default="text-embedding-3-large", alias="TASK_VECTOR_MODEL"
    )
    task_vector_k: int = Field(default=10, alias="TASK_VECTOR_K")
    # Confidence gate for auto-applied field updates (FR-TV-043): a single
    # match with score >= tau_high and no rival within delta auto-applies;
    # [tau_low, tau_high) or >=2 within delta → the agent asks; < tau_low → none.
    # Calibrated on a synthetic eval set before any auto-write is enabled.
    task_vector_tau_high: float = Field(default=0.45, alias="TASK_VECTOR_TAU_HIGH")
    task_vector_tau_low: float = Field(default=0.30, alias="TASK_VECTOR_TAU_LOW")
    task_vector_delta: float = Field(default=0.05, alias="TASK_VECTOR_DELTA")
    # Best-effort immediate re-embed on task create/edit (FR-TV-013); the cron
    # is the backstop either way.
    task_vector_immediate_upsert: bool = Field(
        default=False, alias="TASK_VECTOR_IMMEDIATE_UPSERT"
    )
    # External MCP server exposing the task tools (FR-TV-090). Off by default.
    task_mcp_server_enabled: bool = Field(
        default=False, alias="TASK_MCP_SERVER_ENABLED"
    )
    # FR-CR-05-110 — narrow Python safety net under the LLM
    # dedup gate: when candidate's normalized title +
    # owner-key set overlaps an existing item, mark
    # duplicate without an LLM call. Empirical: LLM-only
    # dedup (even gpt-5.5) missed identical-title +
    # same-owner cases through 4 rounds of operator
    # regressions. Set to 0 to disable.
    dedup_fast_path: bool = Field(default=True, alias="DEDUP_FAST_PATH")

    # Google
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(
        default="http://localhost:8080/oauth/google/callback",
        alias="GOOGLE_REDIRECT_URI",
    )
    # FR-CR-05-144 — separate OAuth client for read-only Google
    # Calendar access. Operator-pinned: Calendar runs on its own
    # OAuth app so adding/removing the Calendar scope doesn't
    # disrupt the existing Sheets/Docs/Tasks consent. Refresh
    # token stored under DB user_key=`_calendar` (vs
    # `_service_account` for Sheets/Docs/Tasks).
    google_calendar_client_id: str = Field(
        default="", alias="GOOGLE_CALENDAR_CLIENT_ID"
    )
    google_calendar_client_secret: str = Field(
        default="", alias="GOOGLE_CALENDAR_CLIENT_SECRET"
    )
    # `primary` = the operator's own primary calendar. Override
    # to a specific calendar id (e.g. `team@thehumanoid.ai`) to
    # match against a shared team calendar instead.
    google_calendar_id: str = Field(
        default="primary", alias="GOOGLE_CALENDAR_ID"
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
    # FR-GS-* / FR-TASK-* — Google Sheets Versioned Sync (ISOLATED feature).
    # Off by default; runs in its own container. Never touches the existing
    # `tasks` sync (GoogleSheetsSync) — a SEPARATE spreadsheet + gs_* tables.
    sheet_sync_enabled: bool = Field(
        default=False, alias="SHEET_SYNC_ENABLED"
    )
    sheet_sync_spreadsheet_id: str = Field(
        default="", alias="SHEET_SYNC_SPREADSHEET_ID"
    )
    # FR-CR-05-233 — operator 2026-06-01: «надо все» — push EVERY titled
    # task to the Sheet, not only the strategic directions. Set False to
    # restore the strategic-only filter.
    sheet_sync_all_directions: bool = Field(
        default=True, alias="SHEET_SYNC_ALL_DIRECTIONS"
    )
    sheet_sync_tab_title: str = Field(
        default="ceo_brain_tasks", alias="SHEET_SYNC_TAB_TITLE"
    )
    sheet_sync_interval_seconds: int = Field(
        default=300, alias="SHEET_SYNC_INTERVAL_SECONDS"
    )
    sheet_sync_timezone: str = Field(
        default="Europe/London", alias="SHEET_SYNC_TIMEZONE"
    )
    sheet_sync_max_rows_per_tab: int = Field(
        default=5000, alias="SHEET_SYNC_MAX_ROWS_PER_TAB"
    )
    # FR-SS — bidirectional Sheet<->DB bridge (SPEC_SHEET_SYNC_v0.1).
    # OFF by default, fully decoupled from sheet_sync_enabled (System B
    # feeder) and the legacy app/sync/sheets.py writer. Activate ONLY after
    # `--migrate` has stamped DeveloperMetadata and a shadow-tab smoke pass.
    sheet_sync_bridge_enabled: bool = Field(
        default=False, alias="SHEET_SYNC_BRIDGE_ENABLED"
    )
    sheet_sync_bridge_max_delete_pct: float = Field(
        default=0.2, alias="SHEET_SYNC_BRIDGE_MAX_DELETE_PCT"
    )
    # FR-CR-05-124 — counterparties directory pulled from two
    # Google Sheets, wipe-and-reload semantics. Source A is the
    # «Status outreach» tab on the investor master sheet (name
    # in column B, type in column A, all columns captured as
    # JSON). Source B has three tabs each with names in
    # column A — the tab name itself becomes the type.
    counterparties_status_sheet_id: str = Field(
        default="", alias="COUNTERPARTIES_STATUS_SHEET_ID"
    )
    counterparties_status_tab_name: str = Field(
        default="Status outreach",
        alias="COUNTERPARTIES_STATUS_TAB_NAME",
    )
    counterparties_outreach_sheet_id: str = Field(
        default="", alias="COUNTERPARTIES_OUTREACH_SHEET_ID"
    )
    # Comma-separated tab names on the «outreach» sheet, each
    # becomes a `type` value on the hub.
    counterparties_outreach_tab_names: str = Field(
        default="Outreach,Rejections,Looking for intros",
        alias="COUNTERPARTIES_OUTREACH_TAB_NAMES",
    )
    # FR-CR-05-124 follow-up — third source: investor-targets
    # sheet with «Investor Targets» + «rejected» tabs. Name-first
    # pattern (column A = name, tab name = type).
    counterparties_targets_sheet_id: str = Field(
        default="", alias="COUNTERPARTIES_TARGETS_SHEET_ID"
    )
    counterparties_targets_tab_names: str = Field(
        default="Investor Targets,rejected",
        alias="COUNTERPARTIES_TARGETS_TAB_NAMES",
    )
    counterparties_poll_interval_seconds: int = Field(
        default=300, alias="COUNTERPARTIES_POLL_INTERVAL_SECONDS"
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
    # FR-CR-05-61 — listener pulls Google Tasks (edits + deletes
    # made in the Google Tasks UI) every N seconds and writes
    # them back to DB + refreshes the TG card. Default 60s.
    google_tasks_pull_interval_seconds: int = Field(
        default=60, alias="GOOGLE_TASKS_PULL_INTERVAL_SECONDS"
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
    # FR-CR-05-192t — operator-pinned 2026-05-22: «не обрабатывать
    # встречи меньше 5 мин». Both Fireflies + Zoom pipelines skip
    # `process_one` early when `row.duration_seconds < this`, with
    # `report.skipped_reason="duration_too_short"`. 300s = 5 minutes.
    min_meeting_seconds: int = Field(
        default=300, alias="MIN_MEETING_SECONDS",
    )

    # Models — the user can override per cost / quality.
    fireflies_summary_model: str = Field(
        default="gpt-5.5", alias="FIREFLIES_SUMMARY_MODEL"
    )
    # FR-CR-05-104 — bumped to gpt-5.5 alongside the main
    # model for accuracy on Russian transcript summarisation
    # + task extraction.
    # FR-CR-05-204 — short summary остаётся gpt-5.5 (качество RU-фразы), но
    # БЕЗ reasoning: call-site `complete_text` НЕ передаёт reasoning_effort,
    # поэтому reasoning-токены не тратятся.
    fireflies_short_summary_model: str = Field(
        default="gpt-5.5", alias="FIREFLIES_SHORT_SUMMARY_MODEL"
    )
    # FR-CR-05-120 — task extraction stays on plain gpt-5.5
    # (the operator's key doesn't currently expose a separate
    # `*-thinking` SKU). Override to a reasoning model via env
    # if one becomes available: `FIREFLIES_TASKS_MODEL=…`.
    fireflies_tasks_model: str = Field(
        default="gpt-5.5", alias="FIREFLIES_TASKS_MODEL"
    )
    # FR-CR-05-120 — operator pinned `reasoning.effort=high` for
    # task extraction so gpt-5.5 spends more think-budget per
    # call (catches more actionable items, applies rule-7
    # named-assignee routing more consistently). Only sent to
    # gpt-5.x / o-series; 4o-family ignores the kwarg.
    fireflies_tasks_reasoning_effort: str = Field(
        default="high", alias="FIREFLIES_TASKS_REASONING_EFFORT"
    )
    fireflies_whisper_model: str = Field(
        default="gpt-4o-transcribe-diarize", alias="FIREFLIES_WHISPER_MODEL"
    )
    # Listener-side periodic poll (mirrors VIEW_REALTIME_ENABLED
    # for TG view).
    fireflies_realtime_enabled: bool = Field(
        default=False, alias="FIREFLIES_REALTIME_ENABLED"
    )
    fireflies_poll_interval_seconds: int = Field(
        default=60, alias="FIREFLIES_POLL_INTERVAL_SECONDS"
    )
    fireflies_poll_batch_size: int = Field(
        default=20, alias="FIREFLIES_POLL_BATCH_SIZE"
    )
    # Hard cap on the audio file size we'll download + Whisper
    # (Whisper API has a 25 MB request cap; meetings can run
    # longer than that as a single mp3 — we'd need to chunk in
    # that case, which is not yet implemented).
    # FR-CR-05-115 — operator: «а если 100 mb или больше,
    # поставь 200». Whisper itself still hard-limits at 25 MB
    # but FirefliesPipeline now falls back to Fireflies'
    # GraphQL `sentences` transcript when Whisper can't take
    # the file, so the cap here just bounds the on-disk
    # download attempt.
    fireflies_audio_max_bytes: int = Field(
        default=200 * 1024 * 1024,
        alias="FIREFLIES_AUDIO_MAX_BYTES",
    )

    # FR-CR-05-116 — Zoom Cloud Recordings as a second meeting
    # source, mirroring the Fireflies pipeline.
    # Server-to-Server OAuth: account_id + client_id +
    # client_secret → access_token. `secret_token` is the
    # webhook verification secret (used only when we add
    # webhook ingestion later).
    zoom_account_id: str = Field(default="", alias="ZOOM_ACCOUNT_ID")
    zoom_client_id: str = Field(default="", alias="ZOOM_CLIENT_ID")
    zoom_client_secret: str = Field(default="", alias="ZOOM_CLIENT_SECRET")
    zoom_secret_token: str = Field(default="", alias="ZOOM_SECRET_TOKEN")
    zoom_api_base: str = Field(
        default="https://api.zoom.us/v2", alias="ZOOM_API_BASE"
    )
    zoom_oauth_url: str = Field(
        default="https://zoom.us/oauth/token", alias="ZOOM_OAUTH_URL"
    )
    zoom_audio_dir: str = Field(default="/app/zoom", alias="ZOOM_AUDIO_DIR")
    zoom_docs_folder_id: str = Field(
        default="", alias="ZOOM_DOCS_FOLDER_ID"
    )
    zoom_audio_max_bytes: int = Field(
        default=200 * 1024 * 1024, alias="ZOOM_AUDIO_MAX_BYTES"
    )
    # FR-CR-05-146b — batch size for the counterparty-resolve
    # LLM pass. When > 0 AND mentions list exceeds it, split
    # mentions into batches and run them in parallel via thread-
    # pool. Each batch sees the FULL directory so per-batch
    # disambiguation against the universe is unchanged.
    # Default 20 → ~3-4 parallel calls instead of one giant
    # one (8x faster on a 70-mention meeting). 0 disables.
    counterparty_resolve_batch_size: int = Field(
        default=20, alias="COUNTERPARTY_RESOLVE_BATCH_SIZE"
    )
    counterparty_resolve_max_workers: int = Field(
        default=5, alias="COUNTERPARTY_RESOLVE_MAX_WORKERS"
    )

    # FR-CR-05-143 — operator-pinned «мне надо проверять что там
    # есть 1@thehumanoid.ai и только если да, то обрабатывать».
    # `/accounts/me/recordings` returns ALL recordings under the
    # Humanoid.AI Zoom account (incl. ones hosted by other
    # teammates). When ZOOM_REQUIRED_EMAIL is set, a recording is
    # kept iff `host_email` matches OR the email appears in the
    # `/past_meetings/{uuid}/participants` list (i.e. the email
    # is the host OR a confirmed attendee). Empty disables the
    # filter (legacy behaviour — accept all).
    zoom_required_email: str = Field(
        default="", alias="ZOOM_REQUIRED_EMAIL"
    )
    # FR-CR-05-167 — operator-pinned 2026-05-14: «14/05 - Летучка
    # СЕО Office c Ириной — почему это выводится вообще в слак,
    # если там не хост 1@thehumanoid.ai». Default behaviour
    # (FR-CR-05-143) kept any recording where Artem was in the
    # participants list — Иринины встречи c Артемом-гостем
    # просачивались. Strict mode: only keep when host_email ==
    # ZOOM_REQUIRED_EMAIL exactly, no participants fallback.
    zoom_required_email_strict_host: bool = Field(
        default=False, alias="ZOOM_REQUIRED_EMAIL_STRICT_HOST"
    )
    # FR-CR-05-118 — listener-side periodic poll for Zoom Cloud
    # Recordings, mirrors `FIREFLIES_REALTIME_ENABLED`. Idempotent:
    # ZoomPipeline.process_one short-circuits already-processed
    # rows on the per-step bookmark flags.
    zoom_realtime_enabled: bool = Field(
        default=False, alias="ZOOM_REALTIME_ENABLED"
    )
    zoom_poll_interval_seconds: int = Field(
        default=60, alias="ZOOM_POLL_INTERVAL_SECONDS"
    )
    zoom_poll_batch_size: int = Field(
        default=10, alias="ZOOM_POLL_BATCH_SIZE"
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
