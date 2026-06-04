"""Dry-run ONE meeting through detailed_summary → tasks → short_summary with the
FR resolver applied, and PRINT the result — with ZERO side effects:

  * runs inside a transaction that is ROLLED BACK (no DB writes — tasks,
    entity_fr_decisions, counterparty mentions are all discarded);
  * docs_factory=None / sender=None → NO Google Doc, NO Telegram;
  * the publish steps (Slack mirror, n8n webhook, task cards, send_short_summary)
    are simply NOT called.

Only external calls are the resolver's MCP fetch + the LLM (read-only).

Usage (on the host, inside the container):
    docker exec -i manager-zoom-ff-1 python -m ops.dry_run_meeting --ff-id <id>
    docker exec -i manager-zoom-ff-1 python -m ops.dry_run_meeting --zoom-id <id>
    docker exec -i manager-zoom-ff-1 python -m ops.dry_run_meeting   # latest real FF
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope


def _latest_real_ff(sess) -> str | None:
    from app.models import MeetingRecording
    row = (sess.query(MeetingRecording.fireflies_id)
           .filter(MeetingRecording.detailed_summary.isnot(None))
           .filter(MeetingRecording.tasks_extracted_count > 0)
           .order_by(MeetingRecording.meeting_date.desc())
           .first())
    return row[0] if row else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--ff-id", default=None)
    ap.add_argument("--zoom-id", default=None)
    a = ap.parse_args()

    s = get_settings()
    from openai import OpenAI

    from app.intent.llm_backends import OpenAIBackend
    llm = OpenAIBackend(OpenAI(api_key=s.openai_api_key), s.openai_model)

    from app.models import Task, TaskSourceKind

    with session_scope() as sess:
        if a.zoom_id:
            from app.zoom.client import ZoomClient
            from app.zoom.pipeline import ZoomPipeline
            from app.models import ZoomRecording
            row = sess.query(ZoomRecording).filter(
                ZoomRecording.zoom_id == a.zoom_id).one()
            pipe = ZoomPipeline(
                settings=s,
                client=ZoomClient(account_id=s.zoom_account_id, client_id=s.zoom_client_id,
                                  client_secret=s.zoom_client_secret, api_base=s.zoom_api_base,
                                  oauth_url=s.zoom_oauth_url),
                llm_backend=llm, docs_factory=None, sender=None, calendar_factory=None)
            src_kind, src_id = TaskSourceKind.zoom, row.zoom_id
        else:
            from app.fireflies.client import FirefliesClient
            from app.fireflies.pipeline import FirefliesPipeline
            from app.models import MeetingRecording
            ffid = a.ff_id or _latest_real_ff(sess)
            if not ffid:
                print("no FF meeting found"); return 1
            row = sess.query(MeetingRecording).filter(
                MeetingRecording.fireflies_id == ffid).one()
            pipe = FirefliesPipeline(
                settings=s,
                client=FirefliesClient(token=s.fireflies_api_token, endpoint=s.fireflies_api_url),
                llm_backend=llm, docs_factory=None, sender=None, calendar_factory=None)
            src_kind, src_id = TaskSourceKind.fireflies, row.fireflies_id

        print(f"meeting: {row.title!r}  source_id={src_id}")
        print(f"transcript_chars={len(row.transcript_text or '')}  "
              f"(DRY-RUN — rolled back, nothing sent)\n")

        # Force a fresh re-run in memory + clear existing tasks IN THE SESSION
        # (rolled back) so we see only this run's output.
        row.detailed_summarised = False
        row.detailed_summary = None
        row.tasks_extracted = False
        row.tasks_extracted_count = 0
        sess.query(Task).filter(Task.source_kind == src_kind,
                                Task.source_conversation_id == src_id
                                ).delete(synchronize_session=False)

        def _try(label, fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"[{label} failed: {type(e).__name__}: {str(e)[:160]}]")

        _try("detailed", lambda: pipe._step_detailed_summary(row, session=sess))
        _try("extract_tasks", lambda: pipe._step_extract_tasks(sess, row))
        _try("verify_tasks", lambda: pipe._step_verify_tasks(sess, row))
        _try("canonicalize_tasks", lambda: pipe._step_canonicalize_task_names(sess, row))
        _try("consolidate_tasks", lambda: pipe._step_consolidate_tasks(sess, row))
        _try("short_summary", lambda: pipe._step_short_summary(sess, row))

        print("=" * 70)
        print("DETAILED SUMMARY (FR resolver applied):\n")
        print(row.detailed_summary or "(none)")
        print("\n" + "=" * 70)
        tasks = (sess.query(Task)
                 .filter(Task.source_kind == src_kind,
                         Task.source_conversation_id == src_id).all())
        print(f"TASKS ({len(tasks)}):\n")
        for t in tasks:
            print(f"  • {t.title}   [owner: {t.owner_display_name}]")
        print("\n" + "=" * 70)
        print("SHORT SUMMARY:\n")
        print(row.short_summary or "(none / suppressed by content gate)")

        sess.rollback()
        print("\n" + "=" * 70)
        print("(rolled back — NO DB changes, NO doc, NO Slack/TG/webhook, NO task cards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
