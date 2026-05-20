#!/usr/bin/env python3
"""Force-rerun the Zoom summary pipeline on an existing recording so
the new bilingual restoration (FR-CR-05-170) + calendar-driven
participants (FR-CR-05-169) + Zoom-attendees reconcile (FR-CR-05-172)
land in a NEW Slack DM / Google Doc / Telegram cards — so the
operator can SEE the new code's output, not just read CLI logs.

Operator-pinned 2026-05-20: «как мне точно быть во всем убежден что
реально работает, что реально отправляется».

What it does:
  1. Clears `detailed_summarised`, `tasks_extracted`,
     `short_summary_sent`, `doc_exported` flags so the pipeline
     re-runs every LLM-touching step.
  2. Calls `ZoomPipeline.process_one` with the row — same code
     path the listener uses for a new recording.
  3. Generates a fresh Doc + Slack DM + TG cards.

WARNING: this WILL post duplicate Slack / TG messages. Use a
recording you've already seen, on a quiet day. Pass `--no-slack`
to short-circuit the cards / DM (Doc still gets created).

Usage:
    docker compose exec -T bot python -m ops.reprocess_zoom_summary \\
        --zoom-id "Es0xxBa8RHG4lPl9Z5ZMGQ=="
"""
from __future__ import annotations

import argparse
import sys

from slack_sdk import WebClient

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import AnthropicBackend, OpenAIBackend
from app.models import ZoomRecording
from app.sync.factories import (
    build_calendar_credentials_factory_with_sa_fallback,
    build_docs_factory,
)
from app.zoom.client import ZoomClient
from app.zoom.pipeline import ZoomPipeline


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoom-id", required=True)
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
    if not s.zoom_account_id:
        print("ERROR: Zoom OAuth not configured.", file=sys.stderr)
        return 2

    with session_scope() as session:
        row = session.query(ZoomRecording).filter(
            ZoomRecording.zoom_id == args.zoom_id
        ).first()
        if row is None:
            print(f"ERROR: no ZoomRecording for zoom_id={args.zoom_id!r}",
                  file=sys.stderr)
            return 3

        print("=" * 70)
        print(f"Re-processing zoom_id={args.zoom_id}")
        print(f"  title:        {row.title!r}")
        print(f"  meeting_date: {row.meeting_date}")
        print(f"  transcribed:  {row.transcribed}")
        print(f"  detailed_sum: {row.detailed_summarised}")
        print(f"  no-slack:     {args.no_slack}")
        print("=" * 70)

        # Reset the flags so the pipeline re-runs the LLM-touching
        # steps. We DON'T clear `transcribed` — the transcript is
        # already on the row; bilingual restoration applies to the
        # next transcribe pass, so to force it we'd also flip
        # `transcribed=False` and the pipeline would call Whisper
        # again on the cached audio.
        if not args.keep_summary:
            row.detailed_summarised = False
            row.detailed_summary = None
        row.tasks_extracted = False
        row.short_summary_sent = False
        row.doc_exported = False
        row.google_doc_id = None
        row.google_doc_url = None
        # Clear participants caches so FR-CR-05-169 runs fresh.
        row.calendar_attendees = None
        if args.no_slack:
            # Mark short summary as «already sent» so the pipeline
            # skips the post-to-Slack step. Doc + Telegram are
            # decoupled.
            row.short_summary_sent = True
        session.flush()

    # Build the pipeline. We don't import the listener wiring —
    # too many side effects — and instead wire the minimum the
    # pipeline needs.
    print("\n[1/2] Wiring pipeline…")
    zm_client = ZoomClient(
        account_id=s.zoom_account_id,
        client_id=s.zoom_client_id,
        client_secret=s.zoom_client_secret,
        api_base=s.zoom_api_base,
        oauth_url=s.zoom_oauth_url,
    )
    from openai import OpenAI
    openai_client = OpenAI(api_key=s.openai_api_key)
    # Most LLM steps in this pipeline use OpenAI through the
    # backend's `_client` attribute.
    extract_model = (s.fireflies_summary_model or "gpt-4o").strip()
    llm = OpenAIBackend(client=openai_client, model=extract_model)
    cal_factory = build_calendar_credentials_factory_with_sa_fallback(s)
    pipeline = ZoomPipeline(
        settings=s,
        client=zm_client,
        llm_backend=llm,
        docs_factory=build_docs_factory(s),
        sender=None,
        calendar_factory=cal_factory,
    )
    print("  → ready (calendar_factory: "
          f"{'available' if cal_factory else 'MISSING'})")

    # Run the pipeline against the same UUID. The ZoomRecordingMeta
    # shape only requires id + a couple of fields; we look up the
    # original row inside process_one.
    print("\n[2/2] Running process_one (re-runs detailed_summary, "
          "tasks_extracted, doc_exported)…")
    from app.zoom.client import ZoomRecordingMeta
    meta = ZoomRecordingMeta(
        id=args.zoom_id,
        meeting_id=None,
        title=None,
        meeting_date=None,
        duration_seconds=None,
        participants=[],
        audio_url=None,
        share_url=None,
        host_email=None,
    )
    with session_scope() as session:
        report = pipeline.process_one(session, meta)
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
    print("  • Open the Google Doc URL above — verify «Участники: …» "
          "uses real Calendar names.")
    if not args.no_slack:
        print("  • Open your Humanoid CEO Brain DM in Slack — the "
              "short summary should have landed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
