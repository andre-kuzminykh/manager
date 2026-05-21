#!/usr/bin/env python3
"""FR-CR-05-176 — force-rerun the Fireflies summary pipeline on an
existing recording so the new calendar-driven attendees enrichment
(FR-CR-05-176) shows up in a fresh Doc / Slack DM / Telegram cards.

Pair-script of `ops/reprocess_zoom_summary.py` — operator wants
parity: same canary verification workflow for внешние meetings.

What it does:
  1. Clears ``detailed_summarised``, ``tasks_extracted``,
     ``short_summary_sent``, ``doc_exported``, ``calendar_attendees``
     flags so the pipeline re-runs every LLM-touching step.
  2. Calls ``FirefliesPipeline.process_one`` with the row.
  3. Generates a fresh Doc + Slack DM + TG cards.

WARNING: this WILL post duplicate Slack / TG messages. Use a
recording you've already seen, on a quiet day. Pass ``--no-slack``
to short-circuit the cards / DM (Doc still gets created).

Usage:
    docker compose exec -T bot python -m ops.reprocess_fireflies_summary \\
        --fireflies-id "01KRZMTRA0EMWPTZVEPNXNN7J2"
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.fireflies.client import FirefliesClient, FirefliesTranscript
from app.fireflies.pipeline import FirefliesPipeline
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
    build_docs_factory,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fireflies-id", required=True)
    ap.add_argument(
        "--no-slack", action="store_true",
        help="Don't post the short summary or per-task cards.",
    )
    ap.add_argument(
        "--keep-summary", action="store_true",
        help="Don't clear `detailed_summarised`; just refresh "
        "downstream (short_summary, Doc, tasks).",
    )
    args = ap.parse_args()

    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        return 2
    if not s.fireflies_api_token:
        print("ERROR: FIREFLIES_API_TOKEN not set.", file=sys.stderr)
        return 2

    with session_scope() as session:
        row = session.query(MeetingRecording).filter(
            MeetingRecording.fireflies_id == args.fireflies_id
        ).first()
        if row is None:
            print(
                f"ERROR: no MeetingRecording for fireflies_id="
                f"{args.fireflies_id!r}",
                file=sys.stderr,
            )
            return 3

        print("=" * 70)
        print(f"Re-processing fireflies_id={args.fireflies_id}")
        print(f"  title:        {row.title!r}")
        print(f"  meeting_date: {row.meeting_date}")
        print(f"  transcribed:  {row.transcribed}")
        print(f"  detailed_sum: {row.detailed_summarised}")
        print(f"  cal_attendees:{bool(row.calendar_attendees)}")
        print(f"  no-slack:     {args.no_slack}")
        print("=" * 70)

        if not args.keep_summary:
            row.detailed_summarised = False
            row.detailed_summary = None
        row.tasks_extracted = False
        row.short_summary_sent = False
        row.doc_exported = False
        row.google_doc_id = None
        row.google_doc_url = None
        # FR-CR-05-176 — clear cached attendees so the new resolve
        # path runs fresh from Calendar.
        row.calendar_attendees = None
        if args.no_slack:
            row.short_summary_sent = True
        session.flush()

    # Build the pipeline.
    print("\n[1/2] Wiring pipeline…")
    ff_client = FirefliesClient(
        token=s.fireflies_api_token,
        endpoint=s.fireflies_api_url,
    )
    from openai import OpenAI
    openai_client = OpenAI(api_key=s.openai_api_key)
    extract_model = (s.fireflies_summary_model or "gpt-4o").strip()
    llm = OpenAIBackend(client=openai_client, model=extract_model)
    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    pipeline = FirefliesPipeline(
        settings=s,
        client=ff_client,
        llm_backend=llm,
        docs_factory=build_docs_factory(s),
        sender=None,
        calendar_factory=cal_factory,
    )
    print(
        "  → ready (calendar_factory: "
        f"{'available' if cal_factory else 'MISSING'})"
    )

    # Run process_one. We need a FirefliesTranscript shape with id +
    # a couple of fields; pipeline looks up the row via fireflies_id.
    print(
        "\n[2/2] Running process_one (re-runs detailed_summary, "
        "tasks_extracted, doc_exported)…"
    )
    transcript = FirefliesTranscript(
        id=args.fireflies_id,
        title=None,
        meeting_date=None,
        duration_seconds=None,
        participants=[],
        audio_url=None,
        share_url=None,
        raw={},
    )
    with session_scope() as session:
        report = pipeline.process_one(session, transcript)
        session.commit()

    print("\n" + "=" * 70)
    print("Report:")
    print(f"  tasks_created:    {report.tasks_created}")
    print(f"  transcript_chars: {report.transcript_chars}")
    print(f"  detailed_chars:   {report.detailed_chars}")
    print(f"  short_chars:      {report.short_chars}")
    print(f"  google_doc_url:   {report.google_doc_url}")
    print(f"  errors:           {report.errors}")
    print("=" * 70)

    print("\nNext check:")
    print(
        "  • Open the Google Doc URL above — verify «Участники: …» "
        "uses real Calendar names."
    )
    if not args.no_slack:
        print(
            "  • Open your Humanoid CEO Brain DM in Slack — the "
            "short summary should have landed."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
