"""FR-CR-05-165 — Pre-meeting agenda.

Workflow:
  1. Every ``AGENDA_TICK_INTERVAL_SECONDS`` (default 60s) the
     runner asks Google Calendar for events starting in
     ``[now + lead - window, now + lead + window]``.
  2. For each event, look up zoom_recordings with a matching
     normalised title in the last ``AGENDA_LOOKBACK_DAYS`` days.
     Skip if fewer than ``AGENDA_MIN_PRIOR_MEETINGS`` matches —
     event isn't «recurring enough» yet.
  3. Build an agenda via LLM: summarise the last meeting + list
     open tasks tied to those zoom_ids + propose discussion points
     + render a Slack-mrkdwn checklist.
  4. DM ``AGENDA_SLACK_TARGET_CHANNEL_ID`` with the short version
     + hyperlink to a Google Doc with the long version.
  5. Persist a MeetingAgenda row keyed on calendar_event_id so a
     duplicate tick can't re-send.

Feature flag: ``AGENDA_ENABLED`` (default False) — runner is a
no-op when off, no API calls.
"""
from app.agenda.service import (
    AgendaCandidate,
    AgendaService,
    normalise_title,
)

__all__ = [
    "AgendaCandidate",
    "AgendaService",
    "normalise_title",
]
