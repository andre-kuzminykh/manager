"""Operator-pinned 2026-05-21 — post per-meeting summaries 19-21 May
to Slack в новом формате:

  - Parent message: HEADER (DD/MM - Title, гиперссылка на Google Doc)
    + короткое саммери body (БЕЗ To-Do).
  - Thread reply под parent: To-Do list (по одному паграфу — Slack
    отдельно рендерит).

Это отличается от текущего пайплайн-пути (где To-Do приклеивается
к телу parent-сообщения). Operator-pinned: «гиперссылка, короткое
саммери, задачи в треде».

Использует ту же Slack-конвертацию (mrkdwn) что и
`app.services.slack_mirror.post_meeting_summary_to_slack`, но шлёт
parent + thread напрямую через slack_sdk WebClient.

Безопасность:
  - SKIP, если `row.short_summary_sent` == True (уже отправлено)
  - SKIP, если нет short_summary / google_doc_url / detailed_summary
  - --dry-run: ничего не шлёт, печатает что бы отправилось
  - --yes: skip interactive confirm

Usage:
    docker compose exec -T bot python -m ops.send_summaries_19_21 \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10 --dry-run

    # реально шлём:
    docker compose exec -T bot python -m ops.send_summaries_19_21 \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10 --yes
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.fireflies.pipeline import (
    _build_todo_section,
    _strip_llm_todo_block,
)
from app.models import MeetingRecording, TaskSourceKind, ZoomRecording
from app.services.slack_mirror import (
    SLACK_TEXT_CHUNK_CHARS,
    _compact_for_slack,
    _split_for_slack,
    _to_slack_mrkdwn,
)


def _is_ready(row) -> bool:
    if row.short_summary_sent:
        return False
    if not (row.transcript_text or "").strip():
        return False
    if not (row.detailed_summary or "").strip():
        return False
    if not (row.short_summary or "").strip():
        return False
    if not (row.google_doc_url or "").strip():
        return False
    return True


def _gather_candidates(
    session, *, start: datetime, end: datetime, min_secs: int,
) -> list[tuple[datetime, str, object]]:
    out: list[tuple[datetime, str, object]] = []
    for r in session.query(ZoomRecording).filter(
        ZoomRecording.meeting_date >= start,
        ZoomRecording.meeting_date < end,
    ).all():
        if (r.duration_seconds or 0) < min_secs:
            continue
        if not _is_ready(r):
            continue
        out.append((r.meeting_date, "zoom", r))
    for r in session.query(MeetingRecording).filter(
        MeetingRecording.meeting_date >= start,
        MeetingRecording.meeting_date < end,
    ).all():
        if (r.duration_seconds or 0) < min_secs:
            continue
        if not _is_ready(r):
            continue
        out.append((r.meeting_date, "fireflies", r))
    out.sort(key=lambda x: x[0])
    return out


def _split_parent_and_tasks(short_summary: str) -> tuple[str, str]:
    """Returns (parent_body, tasks_block).
    `_strip_llm_todo_block` removes the deterministic / LLM To-Do
    section; whatever it removed is the tasks block.
    """
    parent = _strip_llm_todo_block(short_summary).rstrip()
    # `_TODO_SECTION_HEADERS_RE` matched what's between parent and
    # end-of-body; recover it by diffing.
    if parent == short_summary.rstrip():
        return parent, ""
    # The block we stripped is the suffix after `parent` (with the
    # leading «\n\n» eaten by the strip).
    suffix = short_summary[len(parent):].lstrip("\n").rstrip()
    return parent, suffix


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--min-minutes", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument(
        "--sleep", type=float, default=1.5,
        help="Seconds between Slack posts (rate-limit cushion).",
    )
    args = ap.parse_args()

    s = get_settings()
    token = s.slack_bot_token
    channel = s.slack_meeting_channel_id
    if not args.dry_run and (not token or not channel):
        print(
            "ERROR: SLACK_BOT_TOKEN and SLACK_MEETING_CHANNEL_ID "
            "must be set unless --dry-run.",
            file=sys.stderr,
        )
        return 2

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    min_secs = args.min_minutes * 60

    with session_scope() as session:
        cands = _gather_candidates(
            session, start=start, end=end, min_secs=min_secs,
        )
        if not cands:
            print("Nothing to send (no READY rows in range).")
            return 0

        print(f"\n# Sending {len(cands)} meetings to Slack channel "
              f"{channel} (chronological order):\n")
        for i, (dt, src, r) in enumerate(cands, start=1):
            print(
                f"  {i:>2}. {dt.strftime('%m-%d %H:%M')}  {src:<10} "
                f"{(r.title or '')[:60]}"
            )
        if args.dry_run:
            print("\n--dry-run set, exiting before any Slack call.\n")
        elif not args.yes:
            ans = input("\nProceed and post to Slack? [yes/N]: ").strip().lower()
            if ans != "yes":
                print("Aborted.")
                return 1

        if not args.dry_run:
            try:
                from slack_sdk import WebClient
                from slack_sdk.errors import SlackApiError
            except ImportError:
                print("ERROR: slack_sdk not installed.", file=sys.stderr)
                return 3
            client = WebClient(token=token)
        else:
            client = None
            SlackApiError = Exception  # noqa: N806

        sent = 0
        for i, (dt, src, r) in enumerate(cands, start=1):
            parent_text, tasks_text = _split_parent_and_tasks(
                r.short_summary or ""
            )
            parent_text = _compact_for_slack(_to_slack_mrkdwn(parent_text))
            if tasks_text:
                tasks_text = _compact_for_slack(_to_slack_mrkdwn(tasks_text))
            # Parent чанки на случай если body > 35K (редко, но
            # «Weekly Top Management» 2ч транскрипт мог разнести
            # короткое до сотни Kчар если LLM не сжал).
            parent_chunks = _split_for_slack(
                parent_text, limit=SLACK_TEXT_CHUNK_CHARS,
            )
            print(
                f"\n=== [{i}/{len(cands)}] {dt.strftime('%m-%d %H:%M')} "
                f"{src} | {(r.title or '')[:50]}"
            )
            print(
                f"    parent chunks: {len(parent_chunks)}   "
                f"tasks block chars: {len(tasks_text)}"
            )
            if args.dry_run:
                print(f"    parent[0] preview: "
                      f"{parent_chunks[0][:160].replace(chr(10), ' / ')}…")
                if tasks_text:
                    print(f"    tasks preview: "
                          f"{tasks_text[:160].replace(chr(10), ' / ')}…")
                continue

            parent_ts = None
            try:
                resp = client.chat_postMessage(
                    channel=channel,
                    text=parent_chunks[0],
                    unfurl_links=False,
                    unfurl_media=False,
                )
                data = resp.data if hasattr(resp, "data") else dict(resp)
                parent_ts = data.get("ts")
                # Any continuation chunks of parent body → thread.
                for c in parent_chunks[1:]:
                    client.chat_postMessage(
                        channel=channel, text=c,
                        thread_ts=parent_ts,
                        unfurl_links=False, unfurl_media=False,
                    )
                    time.sleep(args.sleep)
                # Tasks in thread (always a separate reply, never
                # appended to parent).
                if tasks_text:
                    client.chat_postMessage(
                        channel=channel, text=tasks_text,
                        thread_ts=parent_ts,
                        unfurl_links=False, unfurl_media=False,
                    )
            except SlackApiError as e:  # noqa: BLE001
                err = (
                    e.response.data.get("error")
                    if e.response is not None
                    and isinstance(e.response.data, dict)
                    else str(e)
                )
                print(f"    ERROR: Slack rejected: {err}")
                continue

            r.short_summary_sent = True
            session.flush()
            sent += 1
            print(f"    posted ts={parent_ts}")
            time.sleep(args.sleep)

        session.commit()
        print(f"\nDone. Sent: {sent}/{len(cands)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
