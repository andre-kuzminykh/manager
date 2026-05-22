"""FR-CR-05-39 — Fireflies meeting recordings registry.

Each row is one recorded meeting we've ingested from Fireflies.
The pipeline (`app/fireflies/pipeline.py`) writes a row when a
new recording appears in the Fireflies API and updates it as
each downstream step lands (mp3 download, Whisper transcript,
detailed summary, short summary, Google Doc, task extraction).

Two consumers:

- *Idempotency.* `processed_at IS NOT NULL` means «we've at least
  attempted this recording». Re-running the migrator never
  re-processes it. The boolean step flags (`audio_downloaded` /
  `transcribed` / etc.) let us resume mid-pipeline if the
  process crashed during e.g. the LLM call.
- *Audit.* The DB row is the lasting record of what happened —
  the operator can grep by ``fireflies_id`` to find the original
  meeting, jump to the Google Doc, see how many tasks fell out.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MeetingRecording(Base, TimestampMixin):
    """One row per Fireflies meeting we've ingested.

    Identifier fields:

      - ``fireflies_id`` — Fireflies' opaque transcript id.
        Unique. Used as the bookmark key so a re-poll of the
        API can never double-process the same meeting.
      - ``title`` — meeting name from Fireflies; used as the
        Google Doc title.
      - ``meeting_date`` — when the meeting actually happened
        (vs. when we ingested it).
      - ``audio_url`` — Fireflies-supplied URL to the mp3.
        Stored so a re-run can re-download without re-querying
        the API.

    Pipeline state flags — each becomes True as the matching
    step lands. Together they form a resumable progress bar:

        audio_downloaded → transcribed → detailed_summarised
        → doc_exported → tasks_extracted

    Failures store an error blurb on ``last_error`` and bump
    ``attempts``; the next poll retries from the first
    incomplete step.
    """

    __tablename__ = "meeting_recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    fireflies_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meeting_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    participants: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    # FR-CR-05-169 — see same field on `ZoomRecording`.
    calendar_attendees: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True
    )
    audio_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    fireflies_share_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )

    # Local artefacts — paths populated by each step.
    audio_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    detailed_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_doc_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    google_doc_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tasks_extracted_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    # Pipeline progress flags — checked before re-doing work on a
    # retry.
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
    # FR-CR-05-193g-3 — flag для idempotent skip pipeline step
    extracted_via_reasoning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["MeetingRecording"]
