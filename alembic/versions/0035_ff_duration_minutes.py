"""FR-CR-05-195 — backfill Fireflies meeting_recordings.duration_seconds.

Before this commit, `FirefliesClient.list_transcripts` stored the raw
`duration` value from Fireflies' GraphQL API into `duration_seconds`.
The API actually returns duration in MINUTES; we were treating it as
seconds. As a result, every meeting was stored with a tiny value
(e.g. 58 for a 58-minute interview) which then failed the
`min_meeting_seconds=300` (5-minute) gate in `process_one` and got
skipped as «duration_too_short» without ever being transcribed.

This migration backfills existing rows: any row with `0 < duration_seconds < 300`
is assumed to be a minutes-stored value and is multiplied by 60.

Records with `duration_seconds = 0` are left alone (those came from
audio_*.ogg ingestions with no measurable duration — multiplying 0
by 60 is still 0, but skipping the row makes intent clearer).

Records with `duration_seconds >= 300` are also left alone — those
either (a) were ingested AFTER the fix was deployed, or (b) were
manually corrected, or (c) genuinely represent ≥5-min meetings that
somehow got stored correctly through an earlier code path.

Revision ID: 0035_ff_duration_minutes
Revises: 0034_slack_post_ts
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op

revision = "0035_ff_duration_minutes"
down_revision = "0034_slack_post_ts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill duration_seconds for FF records stored as minutes."""
    op.execute(
        """
        UPDATE meeting_recordings
           SET duration_seconds = duration_seconds * 60
         WHERE duration_seconds > 0
           AND duration_seconds < 300;
        """
    )


def downgrade() -> None:
    """Reverse the backfill — divide back. Only affects rows we touched."""
    op.execute(
        """
        UPDATE meeting_recordings
           SET duration_seconds = duration_seconds / 60
         WHERE duration_seconds >= 60
           AND duration_seconds < 18000;
        """
    )
