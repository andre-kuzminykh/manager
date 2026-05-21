"""Send ONE Fireflies recording's short_summary to a Slack channel
using the CEO Brain bot token. Mirror of `ops.send_one_zoom`. Reuses
the To-Do block already embedded in `row.short_summary` (faster, no
ephemeral LLM call). Defensive hard-DELETE of any Task rows for this
fireflies_id after the post.

Usage:
    docker compose exec -T bot python -m ops.send_one_fireflies \\
        --fireflies-id "..." --channel D0ASY5QF6UX
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.fireflies.pipeline import _strip_llm_todo_block
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, Task, TaskSourceKind
from app.services.slack_mirror import (
    SLACK_TEXT_CHUNK_CHARS,
    _compact_for_slack,
    _split_for_slack,
    _to_slack_mrkdwn,
)
from ops.send_one_zoom import _split_short_summary
from ops.send_summaries_19_21 import (
    _extract_important_tasks_ephemeral,
    _render_tasks_block,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fireflies-id", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument(
        "--reuse-todo", action="store_true", default=True,
        help="Reuse To-Do already in row.short_summary (default).",
    )
    ap.add_argument(
        "--ephemeral-tasks", dest="reuse_todo", action="store_false",
        help="Run fresh ephemeral LLM extraction at send time.",
    )
    ap.add_argument(
        "--token-key", default="ceo_brain_slack_bot_token",
        choices=[
            "ceo_brain_slack_bot_token",
            "slack_bot_token",
            "agenda_slack_bot_token",
        ],
    )
    ap.add_argument("--no-mark-sent", action="store_true")
    ap.add_argument(
        "--no-tasks", action="store_true",
        help=(
            "Operator-pinned 2026-05-21 — skip tasks thread reply "
            "AND «TODO:» trailer on parent. Parent-only mode."
        ),
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    token = getattr(s, args.token_key, "") or ""
    if not token and not args.dry_run:
        print(f"ERROR: settings.{args.token_key} is empty.", file=sys.stderr)
        return 2

    try:
        from openai import OpenAI
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)

    with session_scope() as session:
        row = session.query(MeetingRecording).filter(
            MeetingRecording.fireflies_id == args.fireflies_id
        ).first()
        if row is None:
            print(f"ERROR: fireflies_id not found", file=sys.stderr)
            return 4
        if not (row.short_summary or "").strip():
            print("ERROR: row.short_summary empty — run reprocess first",
                  file=sys.stderr)
            return 5

        body, reused_todo = _split_short_summary(row.short_summary)
        # FR-CR-05-189 — parent-only mode skips «TODO:» trailer.
        parent_raw = body if args.no_tasks else body + "\n\nTODO:"
        parent_text = _compact_for_slack(_to_slack_mrkdwn(parent_raw))
        chunks = _split_for_slack(parent_text, limit=SLACK_TEXT_CHUNK_CHARS)

        print(f"\n[1/3] Parent body — {len(chunks)} chunk(s):")
        print("-" * 70)
        print(chunks[0][:300] + ("…" if len(chunks[0]) > 300 else ""))
        print("-" * 70)

        if args.no_tasks:
            print("\n[2/3] --no-tasks — skipping tasks thread.")
            tasks_text = ""
        elif args.reuse_todo and reused_todo:
            print(
                f"\n[2/3] Reusing existing To-Do "
                f"({len(reused_todo)} chars)."
            )
            tasks_text = reused_todo
        else:
            print("\n[2/3] Ephemeral task extraction…")
            tasks = _extract_important_tasks_ephemeral(
                row, settings=s, llm_backend=llm,
            )
            print(f"      → {len(tasks)} important tasks")
            tasks_text = _render_tasks_block(tasks)
        if tasks_text:
            tasks_text = _compact_for_slack(_to_slack_mrkdwn(tasks_text))

        if args.dry_run:
            print("\n[3/3] --dry-run — no Slack call. Done.")
            return 0

        print(f"\n[3/3] Posting to {args.channel} via {args.token_key}…")
        client = WebClient(token=token)
        try:
            resp = client.chat_postMessage(
                channel=args.channel, text=chunks[0],
                unfurl_links=False, unfurl_media=False,
            )
            parent_ts = (resp.data or {}).get("ts")
            print(f"      parent ts={parent_ts}")
            for c in chunks[1:]:
                client.chat_postMessage(
                    channel=args.channel, text=c,
                    thread_ts=parent_ts,
                    unfurl_links=False, unfurl_media=False,
                )
            if tasks_text:
                client.chat_postMessage(
                    channel=args.channel, text=tasks_text,
                    thread_ts=parent_ts,
                    unfurl_links=False, unfurl_media=False,
                )
                print("      thread reply (tasks) posted")
        except SlackApiError as e:
            err = (
                e.response.data.get("error")
                if e.response is not None
                and isinstance(e.response.data, dict)
                else str(e)
            )
            print(f"      ERROR: {err}", file=sys.stderr)
            return 6

        if not args.no_mark_sent:
            row.short_summary_sent = True
            session.flush()

        # FR-CR-05-178 — defensive cleanup of any stale Task rows
        # for this recording, mirroring send_one_zoom.
        wiped = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.fireflies)
            .filter(Task.source_conversation_id == row.fireflies_id)
            .delete(synchronize_session=False)
        )
        session.flush()
        session.commit()
        if wiped:
            print(f"      defensive-deleted {wiped} stale Task rows")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
