#!/usr/bin/env python3
"""Send ONE Zoom recording's short_summary to a specific Slack channel
using the CEO Brain bot token (which lives in the operator's Humanoid
workspace, unlike the main SLACK_BOT_TOKEN which is in a different
workspace).

Wraps the same flow as `ops.send_summaries_19_21`:
  - reads `row.short_summary`,
  - strips any LLM-emitted To-Do block,
  - posts parent (DD/MM-Title hyperlink + body),
  - extracts tasks EPHEMERALLY via LLM (FR-CR-05-178), filters by
    DIRECTIONS_IMPORTANT (FR-CR-05-163),
  - posts tasks as thread reply.

NO Task rows persisted. The only DB write is `short_summary_sent=True`
on the recording row (suppressible with `--no-mark-sent`).

Usage:
    docker compose exec -T bot python -m ops.send_one_zoom \\
        --zoom-id "02V6MzzUQbGTI2Yy2sEFPQ==" \\
        --channel D0ASY5QF6UX
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.fireflies.pipeline import _strip_llm_todo_block
from app.intent.llm_backends import OpenAIBackend
from app.models import Task, TaskSourceKind, ZoomRecording
from app.services.slack_mirror import (
    SLACK_TEXT_CHUNK_CHARS,
    _compact_for_slack,
    _split_for_slack,
    _to_slack_mrkdwn,
)
from ops.send_summaries_19_21 import (
    _extract_important_tasks_ephemeral,
    _render_tasks_block,
)


def _split_short_summary(text: str) -> tuple[str, str]:
    """FR-CR-05-184 — split a short_summary string at the To-Do
    marker. Returns (body_without_todo, todo_block).

    `_strip_llm_todo_block` already removes the entire trailing
    To-Do section. We compute it as the suffix that the strip
    consumed. The pipeline's `_build_todo_section` appends a
    formatted, already-filtered (DIRECTIONS_IMPORTANT) list — so
    this is a free-of-charge source for the thread reply, no
    re-extraction needed.
    """
    body = _strip_llm_todo_block(text).rstrip()
    if body == text.rstrip():
        return body, ""
    suffix = text[len(body):].lstrip("\n").rstrip()
    return body, suffix


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument(
        "--reuse-todo", action="store_true", default=True,
        help="Reuse the To-Do block already embedded in "
        "row.short_summary (from the pipeline's deterministic "
        "_build_todo_section, FR-CR-05-119+FR-CR-05-163 filtered). "
        "Default. Skips ephemeral LLM re-extraction.",
    )
    ap.add_argument(
        "--ephemeral-tasks", dest="reuse_todo", action="store_false",
        help="Run a FRESH ephemeral LLM task extraction at send "
        "time (slow). Useful when row's To-Do is stale or you want "
        "to verify the FR-CR-05-178 ephemeral path.",
    )
    ap.add_argument(
        "--token-key", default="ceo_brain_slack_bot_token",
        choices=[
            "ceo_brain_slack_bot_token",
            "slack_bot_token",
            "agenda_slack_bot_token",
        ],
        help="Which Settings token field to use. Default ceo_brain_slack_bot_token "
        "(lives in Humanoid workspace).",
    )
    ap.add_argument(
        "--no-mark-sent", action="store_true",
        help="Don't flip row.short_summary_sent=True. Pure dry-of-DB.",
    )
    ap.add_argument(
        "--no-tasks", action="store_true",
        help=(
            "Operator-pinned 2026-05-21 — don't post the task thread "
            "reply and don't append «TODO:» trailer to the parent. "
            "Parent-only mode (summary in Slack, no tasks anywhere)."
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
        row = session.query(ZoomRecording).filter(
            ZoomRecording.zoom_id == args.zoom_id
        ).first()
        if row is None:
            print(f"ERROR: zoom_id not found", file=sys.stderr)
            return 4
        if not (row.short_summary or "").strip():
            print(f"ERROR: row.short_summary is empty — run reprocess first",
                  file=sys.stderr)
            return 5

        body, reused_todo = _split_short_summary(row.short_summary)
        # FR-CR-05-184 — parent ends with «TODO:» so the reader
        # knows tasks landed in the thread below. No emojis.
        # FR-CR-05-189 — `--no-tasks` skips both the trailer and
        # the thread reply (parent-only mode).
        parent_raw = body if args.no_tasks else body + "\n\nTODO:"
        parent_text = _compact_for_slack(_to_slack_mrkdwn(parent_raw))
        chunks = _split_for_slack(parent_text, limit=SLACK_TEXT_CHUNK_CHARS)

        print(f"\n[1/3] Parent body — {len(chunks)} chunk(s), "
              f"first chunk {len(chunks[0])} chars:")
        print("-" * 70)
        print(chunks[0][:400] + ("…" if len(chunks[0]) > 400 else ""))
        print("-" * 70)

        if args.no_tasks:
            print("\n[2/3] --no-tasks — skipping tasks thread.")
            tasks_text = ""
        elif args.reuse_todo and reused_todo:
            print(
                f"\n[2/3] Reusing existing To-Do from row.short_summary "
                f"({len(reused_todo)} chars, FR-CR-05-119/163 "
                "filtered) — no ephemeral LLM call."
            )
            tasks_text = reused_todo
            # Slack-format conversion below.
        else:
            print("\n[2/3] Ephemeral task extraction (LLM)…")
            tasks = _extract_important_tasks_ephemeral(
                row, settings=s, llm_backend=llm,
            )
            print(
                f"      → {len(tasks)} important tasks "
                "(DIRECTIONS_IMPORTANT filter)"
            )
            for i, t in enumerate(tasks, start=1):
                owner = f" — {t['owner']}" if t['owner'] else ""
                print(f"      {i}) [{t['direction']}] "
                      f"{t['title'][:70]}{owner}")
            tasks_text = _render_tasks_block(tasks)
        if tasks_text:
            tasks_text = _compact_for_slack(_to_slack_mrkdwn(tasks_text))

        if args.dry_run:
            print("\n[3/3] --dry-run — no Slack call. Done.")
            return 0

        print(f"\n[3/3] Posting to Slack channel {args.channel} via "
              f"{args.token_key}…")
        client = WebClient(token=token)
        try:
            resp = client.chat_postMessage(
                channel=args.channel,
                text=chunks[0],
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
                print(f"      thread reply posted (tasks block)")
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

        # FR-CR-05-178 — defensive hard-DELETE of any Task rows
        # persisted for this zoom_id. Pipeline's deterministic
        # `_build_todo_section` reads them to render the To-Do, but
        # operator's contract is «никакие задачи не создавать в бд»
        # — keep zero footprint after the Slack post. FKs cascade.
        wiped = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.zoom)
            .filter(Task.source_conversation_id == row.zoom_id)
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
