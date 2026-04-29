"""FR-CR-05-10 — Team registry ↔ Google Sheet sync CLI.

Usage::

    # First run — seed the table from existing chat-members + Slack
    # employees, then push to the sheet so the operator can polish.
    python -m ops.sync_team --seed --push

    # Pull the operator's edits back into the DB.
    python -m ops.sync_team --pull

    # Round-trip in one shot — pull operator edits, then push the
    # canonical state back so any new auto-seeded rows surface.
    python -m ops.sync_team --pull --push

Exits 2 when the sheet isn't configured, 0 otherwise (per-row
errors get logged but don't abort the run).
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.services.team_members import (
    backfill_team_members_from_chat_members,
    enrich_team_members_from_bot_api,
    seed_from_chat_members,
    seed_from_slack_employees,
    seed_from_telegram_source,
)
from app.sync.factories import build_team_sheet_factory
from app.telegram_ingest import TelegramSourceReader

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Team registry ↔ Google Sheet sync.")
    p.add_argument(
        "--seed",
        action="store_true",
        help=(
            "Auto-import rows from `telegram_chat_members`, the "
            "Supabase `humanoid_tg_chats_readonly` view (every "
            "distinct sender we can see), and Slack `employees` "
            "into `team_members` before any sync. Skips rows that "
            "already exist (matched by tg_user_id / slack_user_id). "
            "Use once at first deploy."
        ),
    )
    p.add_argument(
        "--pull",
        action="store_true",
        help="Read the sheet, upsert into the DB.",
    )
    p.add_argument(
        "--push",
        action="store_true",
        help="Write the DB state to the sheet (clears + rewrites).",
    )
    p.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "FR-CR-05-23 — one-shot backfill: fill BLANK "
            "`telegram_username` / `real_name` on existing "
            "`team_members` rows from whatever the listener has "
            "captured in `telegram_chat_members`. Operator-edited "
            "values are never overwritten. Useful after deploying "
            "FR-CR-05-21 to catch up rows seeded before the "
            "auto-enrich landed."
        ),
    )
    p.add_argument(
        "--enrich-bot-api",
        action="store_true",
        help=(
            "FR-CR-05-24 — for every `team_members` row with a "
            "blank `telegram_username`, call Telegram Bot API "
            "`getChat(<user_id>)` and adopt the returned profile "
            "fields. Works only for users the bot has interacted "
            "with (they /started the bot, sent it a DM, or are a "
            "member of a chat the bot is in). Operator-edited "
            "values are never overwritten. Slower than --backfill "
            "(one HTTP call per sparse row), but reaches users "
            "the listener hasn't observed since FR-CR-05-21."
        ),
    )
    args = p.parse_args()
    if not (
        args.seed or args.pull or args.push or args.backfill
        or args.enrich_bot_api
    ):
        p.error(
            "specify at least one of --seed / --backfill / "
            "--enrich-bot-api / --pull / --push"
        )
    return args


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()

    factory = build_team_sheet_factory(settings)
    if factory is None and (args.pull or args.push):
        log.error(
            "team_sheet_not_configured",
            hint=(
                "Set GOOGLE_TEAM_SHEETS_SPREADSHEET_ID (or "
                "GOOGLE_SHEETS_SPREADSHEET_ID) and ensure the "
                "service account has Editor access to the sheet."
            ),
        )
        return 2

    backfilled = 0
    if args.backfill:
        with session_scope() as session:
            backfilled = backfill_team_members_from_chat_members(session)
        log.info("team_sync_backfilled", rows=backfilled)

    enriched_bot_api = 0
    if args.enrich_bot_api:
        if not settings.telegram_bot_token:
            log.error(
                "team_sync_enrich_bot_api_needs_token",
                hint="TELEGRAM_BOT_TOKEN is required for getChat calls.",
            )
            return 2
        from app.telegram_bot.sender import TelegramSender

        sender = TelegramSender(token=settings.telegram_bot_token)
        with session_scope() as session:
            enriched_bot_api = enrich_team_members_from_bot_api(session, sender)
        log.info("team_sync_enriched_bot_api", rows=enriched_bot_api)

    seeded_chat = seeded_slack = seeded_tg_view = 0
    if args.seed:
        with session_scope() as session:
            seeded_chat = seed_from_chat_members(session)
            seeded_slack = seed_from_slack_employees(session)
            # FR-CR-05-10 — also pull from the read-only Supabase
            # view so we don't depend on the live listener having
            # observed every teammate.
            tg_reader = None
            if settings.telegram_source_database_url:
                try:
                    tg_reader = TelegramSourceReader(
                        database_url=settings.telegram_source_database_url,
                        view_name=settings.telegram_source_view,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "team_sync_telegram_reader_failed", error=str(e)
                    )
            if tg_reader is not None:
                seeded_tg_view = seed_from_telegram_source(session, tg_reader)
        log.info(
            "team_sync_seeded",
            from_chat_members=seeded_chat,
            from_slack_employees=seeded_slack,
            from_telegram_source=seeded_tg_view,
        )

    pulled_updated = pulled_inserted = 0
    if args.pull:
        sync = factory()
        if sync is None:
            log.error("team_sheet_no_credentials")
            return 2
        with session_scope() as session:
            pulled_updated, pulled_inserted = sync.pull(session)
        log.info(
            "team_sync_pulled",
            updated=pulled_updated,
            inserted=pulled_inserted,
        )

    pushed = 0
    if args.push:
        sync = factory()
        if sync is None:
            log.error("team_sheet_no_credentials")
            return 2
        with session_scope() as session:
            pushed = sync.push(session)
        log.info("team_sync_pushed", rows=pushed)

    log.info(
        "team_sync_done",
        seeded_chat=seeded_chat,
        seeded_slack=seeded_slack,
        seeded_tg_view=seeded_tg_view,
        backfilled=backfilled,
        enriched_bot_api=enriched_bot_api,
        pulled_updated=pulled_updated,
        pulled_inserted=pulled_inserted,
        pushed=pushed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
