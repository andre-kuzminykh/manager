"""FR-CR-05-192x mock-test — manually push one synthetic Telegram
message through `classify_and_persist` to verify the unified
`tasks` table accepts `source_kind=telegram` end-to-end.

This is a one-shot smoke test that:
  1. Builds a fake TG-flavoured payload + Services bundle.
  2. Calls classify_and_persist → action_draft (state=proposed).
  3. Forces draft confirmation → Task row inserted with
     source_kind=telegram.
  4. Prints the resulting row.
  5. Optionally deletes everything it created (--cleanup).

Usage:
    docker exec manager-bot-1 python -m ops.test_tg_ingest_mock \\
        --text "Алина, подготовь deck для Stellantis к среде" \\
        --tg-chat-id -100123456 --tg-user-id 222968032 \\
        [--cleanup]
"""
from __future__ import annotations

import argparse
import sys

from app.db import session_scope
from app.intent import IntentClassifier
from app.intent.llm_backends import OpenAIBackend
from app.config import get_settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--tg-chat-id", default="-100123456")
    ap.add_argument(
        "--tg-user-id", default="222968032",
        help="Numeric TG user id (used as fake source author)",
    )
    ap.add_argument(
        "--cleanup", action="store_true",
        help="Delete the created draft + task after printing.",
    )
    args = ap.parse_args()

    s = get_settings()
    from openai import OpenAI
    llm = OpenAIBackend(
        OpenAI(api_key=s.openai_api_key), s.openai_model,
    )

    from app.schemas.intent import InvocationType
    from app.slack_bot.handlers.shared import Services, classify_and_persist
    from app.orchestrator.service import Orchestrator
    from app.services import EmployeeDirectory
    from app.context.retriever import ContextRetriever
    from unittest.mock import MagicMock

    fake_slack_client = MagicMock()
    # `ContextRetriever` calls `conversations_history`/`conversations_replies`
    # → return empty so we don't hit Slack.
    fake_slack_client.conversations_history.return_value = {"messages": []}
    fake_slack_client.conversations_replies.return_value = {"messages": []}
    services = Services(
        slack=fake_slack_client,
        context_retriever=ContextRetriever(
            fake_slack_client,
            window_before=s.context_window_before,
        ),
        classifier=IntentClassifier(backend=llm),
        orchestrator=Orchestrator(s),
        employees=EmployeeDirectory(
            client=fake_slack_client, settings=s,
        ),
    )

    # Fake source_message keyed in the TG dialect — `ts` is a
    # synthetic string, `subtype=None` means user-authored.
    fake_ts = f"tg-mock-{args.tg_user_id}"
    source_message = {
        "ts": fake_ts,
        "thread_ts": None,
        "user": args.tg_user_id,
        "subtype": None,
        "text": args.text,
    }
    fake_raw = {
        "type": "message",
        "channel": args.tg_chat_id,
        "user": args.tg_user_id,
        "ts": fake_ts,
        "text": args.text,
        # TG-flavour hint
        "source": "telegram",
    }

    with session_scope() as db:
        print(f"\n[1/3] classify_and_persist on text:\n  '{args.text}'\n")
        classification, draft, snapshot = classify_and_persist(
            db,
            services=services,
            conversation_id=str(args.tg_chat_id),
            kind="telegram",  # NB — informs Conversation.kind
            source_message=source_message,
            invocation_type=InvocationType.passive,
            slack_user_id=args.tg_user_id,
            raw_event=fake_raw,
            transcript=None,
            has_audio=False,
        )
        print(
            f"  intent={classification.intent} "
            f"confidence={classification.confidence:.2f}"
        )
        if draft is None:
            print("  → no draft (intent != actionable)")
            return 0
        print(
            f"  ✓ ActionDraft id={draft.id} state={draft.state} "
            f"intent={draft.intent}"
        )

        # [2] Force-confirm the draft → Task row created
        from app.models import (
            ActionDraft as _AD, ActionDraftState as _ADS,
            Task, TaskSourceKind,
        )
        from app.orchestrator.finalize import FinalizeService

        # Use a minimal FinalizeService — its job is to flip
        # draft.state and INSERT Task.
        print("\n[2/3] Confirming draft → Task row…")
        from datetime import datetime, timezone
        # Direct insert mirroring what /confirm action would do
        # — keeps the mock self-contained without invoking the
        # full finalizer (which depends on more Slack glue).
        payload = draft.payload or {}
        task = Task(
            title=(payload.get("title") or "(no title)")[:512],
            description=payload.get("description"),
            owner_display_name=payload.get("owner_display_name"),
            source_kind=TaskSourceKind.telegram,
            source_conversation_id=str(args.tg_chat_id),
            source_message_ts=fake_ts,
            created_by_slack_user_id=args.tg_user_id,
        )
        db.add(task)
        draft.state = _ADS.confirmed
        db.flush()
        print(
            f"  ✓ Task id={task.id} source_kind={task.source_kind} "
            f"title='{task.title[:60]}' owner='{task.owner_display_name}'"
        )

        # [3] Show the row from tasks
        print("\n[3/3] SELECT * FROM tasks WHERE id = {}".format(task.id))
        row = db.query(Task).filter(Task.id == task.id).first()
        if row:
            print(f"  id            : {row.id}")
            print(f"  source_kind   : {row.source_kind}")
            print(f"  source_conv   : {row.source_conversation_id}")
            print(f"  source_ts     : {row.source_message_ts}")
            print(f"  title         : {row.title[:80]}")
            print(f"  description   : {(row.description or '')[:120]}")
            print(f"  owner         : {row.owner_display_name}")
            print(f"  status        : {row.status}")
            print(f"  priority      : {row.priority}")
            print(f"  created_at    : {row.created_at}")

        if args.cleanup:
            print("\nCleanup — deleting draft + task + snapshot…")
            db.delete(task)
            db.delete(draft)
            if snapshot is not None:
                db.delete(snapshot)
            db.commit()
            print("  ✓ cleaned")
        else:
            db.commit()
            print("\nKept in DB. Run with --cleanup to remove afterwards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
