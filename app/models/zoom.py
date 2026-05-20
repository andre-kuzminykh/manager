"""FR-CR-05-116 — Zoom cloud recordings registry.

Mirror of `MeetingRecording` (FR-CR-05-39) for Zoom Cloud
Recordings as a second meeting source. Each row is one
recorded meeting we've ingested from Zoom. The pipeline
(`app/zoom/pipeline.py`) writes a row when a new recording
appears in the Zoom API and updates it as each downstream
step lands (mp3 download, Whisper transcript, detailed
summary, short summary, Google Doc, task extraction).

Two consumers:

- *Idempotency.* `processed_at IS NOT NULL` means «we've at
  least attempted this recording». Re-running the migrator
  never re-processes it. The boolean step flags
  (`audio_downloaded` / `transcribed` / etc.) let us resume
  mid-pipeline if the process crashed during e.g. the LLM call.
- *Audit.* The DB row is the lasting record of what happened —
  the operator can grep by ``zoom_id`` to find the original
  meeting, jump to the Google Doc, see how many tasks fell out.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ZoomRecording(Base, TimestampMixin):
    """One row per Zoom cloud meeting we've ingested.

    Identifier fields:
      - ``zoom_id`` — Zoom's stable meeting/recording id (uuid).
        Unique. Bookmark key so a re-poll never double-
        processes the same meeting.
      - ``zoom_meeting_id`` — Zoom's numeric meeting_id (the
        one shown in the join URL). Same meeting can have
        multiple `uuid` recordings (rejoin / re-record), so
        the `zoom_id` (uuid) is the dedup key.
      - ``title`` — meeting topic from Zoom; used as the
        Google Doc title.
      - ``meeting_date`` — start_time from Zoom.
      - ``audio_url`` — Zoom's recording-file `download_url`
        (audio-only mp3 if available, else mp4 fallback).

    Pipeline state mirrors `MeetingRecording`:

        audio_downloaded → transcribed → detailed_summarised
        → doc_exported → tasks_extracted

    Failures store an error blurb on ``last_error`` and bump
    ``attempts``; the next poll retries from the first
    incomplete step.
    """

    __tablename__ = "zoom_recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    zoom_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    zoom_meeting_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meeting_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    participants: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # FR-CR-05-169 — attendees resolved from the matching Google
    # Calendar event (when found). Authoritative when non-empty;
    # falls back to `participants` (LLM-from-transcript) otherwise.
    # See `app.services.calendar_attendees.resolve_calendar_attendees_for_zoom`
    # for the shape.
    calendar_attendees: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True
    )
    audio_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    zoom_share_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    # FR-CR-05-166 — host email from `ZoomRecordingMeta`. Filled
    # at ingest time so the agenda pattern-detector can exclude
    # teammates' recurring series (operator-pinned: only post
    # agendas for meetings hosted by `1@thehumanoid.ai`).
    host_email: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )

    audio_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    detailed_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_doc_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    google_doc_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tasks_extracted_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    audio_downloaded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    transcribed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    detailed_summarised: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    short_summary_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    doc_exported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    tasks_extracted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["ZoomRecording"]
