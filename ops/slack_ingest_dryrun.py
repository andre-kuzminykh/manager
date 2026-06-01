"""FR-CR-05-162 — read-only Slack ingest DRY-RUN / channel audit.

Two jobs, both READ-ONLY (no DB writes, no Task/draft creation, no TG
cards, no Slack output):

  --list-channels
      Print every channel the bot is a member of (= exactly what the
      Socket-Mode ingest listens to). Answers «к каким каналам подключён».

  (default) [--channel C] [--limit N]
      Pull the last N messages from the channel(s) the bot is in and run
      each human message through the SAME pipeline the live listener uses
      (ContextRetriever → IntentClassifier → _resolve_owner), then PRINT
      what WOULD be extracted: title, owner, priority, due_date, and the
      strategic `direction` (so you can see whether it'd reach the Sheet).
      Nothing is persisted.

Usage:
    docker exec slack-task-slack-ingest python -m ops.slack_ingest_dryrun \\
        --list-channels
    docker exec slack-task-slack-ingest python -m ops.slack_ingest_dryrun \\
        --channel C0123ABCD --limit 20
"""
from __future__ import annotations

import argparse
import sys

from openai import OpenAI
from slack_sdk import WebClient

from app.config import get_settings
from app.context.retriever import ContextRetriever
from app.db import session_scope
from app.intent import IntentClassifier
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.schemas.intent import IntentType, InvocationType
from app.services.task_direction import DIRECTIONS_IMPORTANT, classify_one_direction
from app.slack_ingest.listener import (
    _SKIPPED_SUBTYPES,
    _author_display_from_registry,
    _known_employees_from_db,
    _mention_uids,
)
from app.telegram_ingest.service import _admin_fallback_owner_id, _resolve_owner

log = get_logger(__name__)


def _list_member_channels(client: WebClient) -> list[dict]:
    """Channels the bot user is a member of — what ingest actually sees."""
    out: list[dict] = []
    cursor = None
    while True:
        resp = client.users_conversations(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        out.extend(resp.get("channels", []))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def _human_messages(
    client: WebClient, channel: str, limit: int, oldest: float | None = None,
) -> list[dict]:
    """Most recent `limit` non-bot, non-service text messages, oldest first.
    When `oldest` (epoch seconds) is given, only messages at/after it are
    fetched (used for the --today window)."""
    kwargs: dict = {"channel": channel, "limit": limit}
    if oldest is not None:
        kwargs["oldest"] = str(oldest)
    resp = client.conversations_history(**kwargs)
    msgs = list(resp.get("messages", []))
    msgs.reverse()  # Slack returns newest-first
    keep = []
    for m in msgs:
        if m.get("bot_id") or m.get("subtype") in _SKIPPED_SUBTYPES:
            continue
        if not (m.get("text") or "").strip():
            continue
        keep.append(m)
    return keep


def _trace_message(
    *, msg, channel_id, retriever, classifier, llm, model,
    known_employees, admin_uid,
) -> None:
    source_message = {
        "ts": msg.get("ts"),
        "thread_ts": msg.get("thread_ts"),
        "user": msg.get("user"),
        "text": msg.get("text") or "",
        "channel": channel_id,
        "subtype": msg.get("subtype"),
    }
    text = source_message["text"]
    author = source_message["user"]
    print("\n" + "─" * 78)
    print(f"ts={msg.get('ts')}  author={author}")
    print(f"  text: {text[:200]}")

    window = retriever.build(conversation_id=channel_id, source_message=source_message)
    n_ctx = len(window.history_before)
    n_thr = len(window.thread_messages)
    print(f"  context: {n_ctx} msgs above" + (f" + {n_thr} thread msgs" if n_thr else ""))

    classification = classifier.classify(
        context=window,
        invocation_type=InvocationType.passive,
        known_employees=known_employees,
    )
    if classification.intent != IntentType.create_task or not classification.tasks:
        print(f"  → intent={classification.intent.value} — no task extracted")
        return

    sender_name = _author_display_from_registry(author, known_employees)
    mention_uids = _mention_uids(text, exclude=author)
    for i, td in enumerate(classification.tasks, 1):
        _resolve_owner(
            td,
            known_employees=known_employees,
            sender_user_id=author,
            sender_user_name=sender_name,
            admin_uid=admin_uid,
            mention_uids=mention_uids,
        )
        try:
            direction = classify_one_direction(
                title=td.title or "", description=td.description or "",
                llm_backend=llm, model=model,
            )
        except Exception:  # noqa: BLE001
            direction = "other"
        to_sheet = "→ В ТАБЛИЦУ" if direction in DIRECTIONS_IMPORTANT else "× не в таблицу (other)"
        print(f"  TASK {i}/{len(classification.tasks)}:")
        print(f"     title:     {td.title}")
        print(f"     owner:     {td.owner_display_name}  (uid={td.owner_user_id})")
        print(f"     priority:  {td.priority}   due: {td.due_date}")
        print(f"     direction: {direction}   {to_sheet}")
        if td.description:
            print(f"     desc:      {td.description[:160]}")


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-channels", action="store_true")
    ap.add_argument("--channel", default=None, help="channel id (default: all member channels)")
    ap.add_argument("--limit", type=int, default=15, help="messages per channel")
    ap.add_argument(
        "--today", action="store_true",
        help="only messages since 00:00 Europe/London today (uses --limit as cap)",
    )
    args = ap.parse_args()

    oldest: float | None = None
    if args.today:
        from datetime import datetime, time as _time
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/London")
        start = datetime.combine(datetime.now(tz).date(), _time(0, 0), tzinfo=tz)
        oldest = start.timestamp()
        if args.limit < 100:
            args.limit = 200  # widen so a busy day isn't truncated

    if not s.slack_bot_token:
        print("ERROR: SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 2
    client = WebClient(token=s.slack_bot_token)

    channels = _list_member_channels(client)
    if args.list_channels:
        print(f"Bot is a member of {len(channels)} channel(s):")
        for c in channels:
            vis = "private" if c.get("is_private") else "public"
            print(f"  {c.get('id')}  #{c.get('name')}  [{vis}]")
        return 0

    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set (needed for classify)", file=sys.stderr)
        return 2
    llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key), s.openai_model)
    classifier = IntentClassifier(backend=llm)
    retriever = ContextRetriever(client, window_before=s.context_window_before)
    admin_uid = _admin_fallback_owner_id()

    targets = [args.channel] if args.channel else [c.get("id") for c in channels]
    name_by_id = {c.get("id"): c.get("name") for c in channels}

    with session_scope() as session:
        known_employees = _known_employees_from_db(session)
        print(f"registry: {len(known_employees)} known employees | "
              f"context_window_before={s.context_window_before} | model={s.openai_model}")
        for ch in targets:
            print("\n" + "═" * 78)
            print(f"CHANNEL {ch}  #{name_by_id.get(ch, '?')}  (last {args.limit} msgs)")
            try:
                msgs = _human_messages(client, ch, args.limit, oldest=oldest)
            except Exception as e:  # noqa: BLE001
                print(f"  ! cannot read history: {e}")
                continue
            if not msgs:
                print("  (no human text messages)")
            for m in msgs:
                try:
                    _trace_message(
                        msg=m, channel_id=ch, retriever=retriever,
                        classifier=classifier, llm=llm, model=s.openai_model,
                        known_employees=known_employees, admin_uid=admin_uid,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  ! trace failed for ts={m.get('ts')}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
