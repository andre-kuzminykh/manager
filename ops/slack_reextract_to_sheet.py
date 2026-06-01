"""FR-CR-05-234 — re-extract Slack tasks for a window through the NEW
pipeline and (re)write the Slack section of the Google Sheet.

operator 2026-06-01: «у них должны быть имена / прогон ллм по новому».
The Slack rows already in the sheet came from drafts created BEFORE the
owner/title fixes (FR-CR-05-232) — raw titles, blank owners, some noise.
This tool re-runs each Slack message in the window through the SAME live
pipeline (ContextRetriever 10-above + thread → IntentClassifier → owner =
addressee with clean names → direction) and writes FRESH rows.

SAFE BY DESIGN:
  * READ-ONLY on the Postgres DB — it does NOT touch action_drafts / tasks.
    (The live listener keeps owning prod state; we only rebuild the SHEET.)
  * Writes ONLY the Slack section: deletes existing Источник=Slack rows
    then appends the freshly-extracted ones. Zoom/Fireflies/Telegram rows
    and manual edits are left untouched.
  * DRY-RUN by default. Nothing is written without --apply.
  * 'Added at' = the real Slack message time (Europe/London).

Usage:
    docker exec manager-zoom-ff-1 python -m ops.slack_reextract_to_sheet \\
        --spreadsheet-id 1h1wCHmrmPm5iJl-5oxbZ3HMtwoRWkAO81BzVk4SJLOc \\
        --tab main --since 2026-05-25                # dry-run preview
    # ...then add --apply to actually rewrite the Slack section.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

from openai import OpenAI
from slack_sdk import WebClient

from app.config import get_settings
from app.context.retriever import ContextRetriever
from app.db import session_scope
from app.intent import IntentClassifier
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.schemas.intent import IntentType, InvocationType
from app.services.task_direction import classify_one_direction
from app.sheet_sync.config import TASK_HEADERS
from app.sheet_sync.feeder import _added_at, _ts_to_dt
from app.sheet_sync.sheets_client import SheetTabNotFound, TasksSheetClient
from app.slack_ingest.listener import (
    _SKIPPED_SUBTYPES,
    _author_display_from_registry,
    _known_employees_from_db,
    _mention_uids,
)
from app.telegram_ingest.service import _admin_fallback_owner_id, _resolve_owner

log = get_logger(__name__)

_LONDON = ZoneInfo("Europe/London")
_PRIORITY_DISPLAY = {"low": "Low", "medium": "Medium", "high": "High", "urgent": "High"}
_SLACK_SRC = "Slack"


def _list_member_channels(client: WebClient) -> list[dict]:
    out, cursor = [], None
    while True:
        resp = client.users_conversations(
            types="public_channel,private_channel", exclude_archived=True,
            limit=200, cursor=cursor,
        )
        out.extend(resp.get("channels", []))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def _history_window(client: WebClient, channel: str, oldest: float, latest: float) -> list[dict]:
    """All human text messages in [oldest, latest], oldest-first, paginated."""
    msgs: list[dict] = []
    cursor = None
    while True:
        resp = client.conversations_history(
            channel=channel, oldest=str(oldest), latest=str(latest),
            inclusive=True, limit=200, cursor=cursor,
        )
        msgs.extend(resp.get("messages", []))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    msgs.reverse()
    keep = []
    for m in msgs:
        if m.get("bot_id") or m.get("subtype") in _SKIPPED_SUBTYPES:
            continue
        if not (m.get("text") or "").strip():
            continue
        keep.append(m)
    return keep


def _deadline_time(due: str) -> str:
    return "23:59" if due else ""


def _row(*, title, description, owner, priority_key, direction, due, added_at, link):
    return [
        title,
        description or "",
        owner or "",
        "To Do",
        _PRIORITY_DISPLAY.get((priority_key or "medium").lower(), "Medium"),
        (direction or "other").capitalize(),
        "", "",                       # start date/time
        due or "", _deadline_time(due),  # deadline date/time
        "", "",                       # completion date/time
        "",                           # comments
        added_at,                     # Added at (real message time)
        _SLACK_SRC,                   # Источник
        link or "",                   # Ссылка
    ]


def _permalink(client: WebClient, channel: str, ts: str) -> str:
    try:
        r = client.chat_getPermalink(channel=channel, message_ts=ts)
        return r.get("permalink", "") if r.get("ok") else ""
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreadsheet-id", default=getattr(s, "sheet_sync_spreadsheet_id", "") or "")
    ap.add_argument("--tab", default=getattr(s, "sheet_sync_tab_title", "") or "main")
    ap.add_argument("--since", default="2026-05-25", help="window start (YYYY-MM-DD, Europe/London)")
    ap.add_argument("--until", default=None, help="window end (YYYY-MM-DD); default = now")
    ap.add_argument("--channel", default=None, help="single channel id (default: all member channels)")
    ap.add_argument("--model", default=s.openai_model)
    ap.add_argument("--apply", action="store_true", help="WRITE to the sheet (default: dry-run)")
    args = ap.parse_args()

    if not args.spreadsheet_id:
        print("ERROR: spreadsheet id not set", file=sys.stderr)
        return 2
    if not (s.slack_bot_token and s.openai_api_key):
        print("ERROR: need SLACK_BOT_TOKEN + OPENAI_API_KEY", file=sys.stderr)
        return 2

    since_dt = datetime.combine(datetime.fromisoformat(args.since).date(), dt_time.min, _LONDON)
    until_dt = (
        datetime.combine(datetime.fromisoformat(args.until).date(), dt_time.min, _LONDON)
        if args.until else datetime.now(_LONDON)
    )
    oldest, latest = since_dt.timestamp(), until_dt.timestamp()

    client = WebClient(token=s.slack_bot_token)
    llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key), args.model)
    classifier = IntentClassifier(backend=llm)
    retriever = ContextRetriever(client, window_before=s.context_window_before)
    admin_uid = _admin_fallback_owner_id()

    channels = (
        [{"id": args.channel, "name": args.channel}] if args.channel
        else _list_member_channels(client)
    )

    rows: list[list[str]] = []
    with session_scope() as session:
        known = _known_employees_from_db(session)
        for ch in channels:
            cid = ch["id"]
            try:
                msgs = _history_window(client, cid, oldest, latest)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {cid}: history failed: {e}")
                continue
            print(f"CHANNEL {cid} #{ch.get('name','?')}: {len(msgs)} msgs in window")
            for m in msgs:
                sm = {
                    "ts": m.get("ts"), "thread_ts": m.get("thread_ts"),
                    "user": m.get("user"), "text": m.get("text") or "",
                    "channel": cid, "subtype": m.get("subtype"),
                }
                try:
                    window = retriever.build(conversation_id=cid, source_message=sm)
                    cls = classifier.classify(
                        context=window, invocation_type=InvocationType.passive,
                        known_employees=known,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  ! classify failed ts={m.get('ts')}: {e}")
                    continue
                if cls.intent != IntentType.create_task or not cls.tasks:
                    continue
                sender_name = _author_display_from_registry(sm["user"], known)
                mentions = _mention_uids(sm["text"], exclude=sm["user"])
                added_dt = _ts_to_dt(m.get("ts"))
                added = _added_at(added_dt) if added_dt else ""
                link = _permalink(client, cid, m.get("ts"))
                for td in cls.tasks:
                    _resolve_owner(
                        td, known_employees=known, sender_user_id=sm["user"],
                        sender_user_name=sender_name, admin_uid=admin_uid,
                        mention_uids=mentions,
                    )
                    try:
                        direction = classify_one_direction(
                            title=td.title or "", description=td.description or "",
                            llm_backend=llm, model=args.model,
                        )
                    except Exception:  # noqa: BLE001
                        direction = "other"
                    rows.append(_row(
                        title=(td.title or "").strip(),
                        description=(td.description or "").strip(),
                        owner=(td.owner_display_name or "").strip(),
                        priority_key=getattr(td, "priority", None),
                        direction=direction,
                        due=td.due_date.isoformat() if getattr(td, "due_date", None) else "",
                        added_at=added, link=link,
                    ))
        session.rollback()  # READ-ONLY on the DB

    print(f"\n=== извлечено Slack-задач: {len(rows)} (окно {args.since}..{args.until or 'now'}) ===")
    assert len(TASK_HEADERS) == 16
    for i, r in enumerate(rows[:60], 1):
        print(f"  {i:3d} | {r[5]:<12} | {r[4]:<6} | {(r[2] or '—'):<22} | {r[13]:<16} | {r[0][:48]}")
    if len(rows) > 60:
        print(f"  … +{len(rows)-60} ещё")

    if not args.apply:
        print("\n(--dry-run — в лист НЕ пишу; добавь --apply для записи)")
        return 0
    if not rows:
        print("нет задач — лист не трогаю.")
        return 0

    sheet = TasksSheetClient(spreadsheet_id=args.spreadsheet_id, tab_title=args.tab)
    try:
        removed = sheet.delete_rows_where_source(_SLACK_SRC)
        print(f"удалено старых Slack-строк: {removed}")
        n = sheet.append_rows(rows)
        sheet.ensure_structure()
        print(f"залито свежих Slack-строк: {n} в '{sheet.spreadsheet_title}' / '{args.tab}'")
    except SheetTabNotFound as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
