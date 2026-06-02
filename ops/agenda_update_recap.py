"""FR-CR-05-167 follow-up 2026-06-02 — patch a posted agenda's recap.

Problem: the agenda's «На прошлой встрече: …» recap is built from
`prior_recordings[0].short_summary` (the NEWEST prior recording).  When the
day's meeting was already recorded but not yet summarised, that newest prior
is empty (transcript/summary len 0), so the recap renders blank even though an
older prior (e.g. yesterday's) has a perfectly good summary.  The open-tasks
path already skips thin priors (FR-CR-05-235); the recap path does not.

This one-shot:
  1. loads the `meeting_agendas` row (--agenda-id),
  2. picks the NEWEST prior in `prior_meeting_zoom_ids` that actually has a
     usable `short_summary` (skips the empty/in-progress ones),
  3. fetches the already-posted Slack message (channel + ts on the row),
  4. injects / replaces the «На прошлой встрече: …» line — keeping the rest
     of the message (header, Участники, «Статус задач к обсуждению:») intact,
  5. `chat.update`s the message in place.

Run in the container that posted it (has AGENDA_SLACK_BOT_TOKEN + the DB):
    docker exec manager-bot-1 python -m ops.agenda_update_recap --agenda-id 45 --dry-run
    docker exec manager-bot-1 python -m ops.agenda_update_recap --agenda-id 45
"""
from __future__ import annotations

import argparse
import sys

from app.agenda.compose import _strip_prior_short_summary_body
from app.agenda.slack_format import _slack_safe, _strip_recap_label
from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import MeetingAgenda, ZoomRecording

log = get_logger(__name__)

_RECAP_LABEL = "На прошлой встрече:"
_STATUS_LABEL = "Статус задач к обсуждению:"
_PARTICIPANTS_LABEL = "Участники:"


def _best_recap(session, zoom_ids: list[str]) -> tuple[str | None, str | None]:
    """Return (recap_text, source_zoom_id) for the NEWEST prior with a usable
    `short_summary`. `zoom_ids` is newest-first (as stored). Skips empties."""
    for zid in zoom_ids:
        rec = (
            session.query(ZoomRecording)
            .filter(ZoomRecording.zoom_id == zid)
            .one_or_none()
        )
        if rec is None:
            continue
        recap = _strip_prior_short_summary_body(rec.short_summary or "").strip()
        if recap:
            return recap, zid
    return None, None


def _render_recap_line(recap: str) -> str:
    """Match render_agenda_text's recap formatting exactly."""
    blob = recap.strip().rstrip(".") + "." if recap.strip() else ""
    blob = _slack_safe(_strip_recap_label(blob).strip())
    return f"{_RECAP_LABEL} {blob}" if blob else ""


def _inject_recap(text: str, recap_line: str) -> str:
    """Insert/replace the recap line, preserving the canonical agenda layout:
    header / (blank+Участники) / (blank+recap) / (blank+Статус)."""
    lines = text.split("\n")
    header = lines[0] if lines else ""
    participants = next(
        (ln for ln in lines if ln.startswith(_PARTICIPANTS_LABEL)), None)
    has_status = any(ln.startswith(_STATUS_LABEL) for ln in lines)

    out: list[str] = [header]
    if participants:
        out += ["", participants]
    if recap_line:
        out += ["", recap_line]
    if has_status:
        out += ["", _STATUS_LABEL]
    return "\n".join(out)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description="Patch a posted agenda's recap")
    ap.add_argument("--agenda-id", type=int, required=True,
                    help="meeting_agendas.id of the posted agenda")
    ap.add_argument("--dry-run", action="store_true",
                    help="print old + new body, do NOT call Slack")
    args = ap.parse_args()

    s = get_settings()

    with session_scope() as session:
        row = session.get(MeetingAgenda, args.agenda_id)
        if row is None:
            print(f"ERROR: meeting_agendas id={args.agenda_id} not found",
                  file=sys.stderr)
            return 2
        if not row.slack_ts or not row.slack_channel:
            print(f"ERROR: agenda id={args.agenda_id} has no slack_ts/"
                  f"slack_channel (never posted?)", file=sys.stderr)
            return 2
        zoom_ids = list(row.prior_meeting_zoom_ids or [])
        recap, src = _best_recap(session, zoom_ids)
        channel, ts, title = row.slack_channel, row.slack_ts, row.title

    if not recap:
        print(f"ERROR: no prior with a usable short_summary among "
              f"{len(zoom_ids)} ids — nothing to fill in", file=sys.stderr)
        return 1
    recap_line = _render_recap_line(recap)
    log.info("agenda_update_recap_source", agenda_id=args.agenda_id,
             source_zoom_id=src, recap_chars=len(recap), title=title)

    token = (s.agenda_slack_bot_token or s.slack_bot_token or "").strip()
    if not token:
        print("ERROR: no AGENDA_SLACK_BOT_TOKEN / SLACK_BOT_TOKEN", file=sys.stderr)
        return 2

    try:
        from slack_sdk import WebClient
    except ImportError as e:
        print(f"ERROR: slack_sdk missing: {e}", file=sys.stderr)
        return 2
    client = WebClient(token=token)

    # Fetch the exact message currently in Slack so we preserve header +
    # Участники verbatim and only touch the recap.
    try:
        resp = client.conversations_history(
            channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
        msgs = resp.get("messages") or []
        if not msgs:
            print(f"ERROR: message ts={ts} not found in {channel}", file=sys.stderr)
            return 1
        old_text = msgs[0].get("text") or ""
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: conversations_history failed: {e}", file=sys.stderr)
        return 1

    if _RECAP_LABEL in old_text:
        log.info("agenda_update_recap_already_present", agenda_id=args.agenda_id,
                 hint="recap line exists — will be replaced")
    new_text = _inject_recap(old_text, recap_line)

    if new_text == old_text:
        print("No change — recap already matches; nothing to update.")
        return 0

    print("=" * 72)
    print(f"AGENDA id={args.agenda_id}  «{title}»  channel={channel} ts={ts}")
    print(f"recap source zoom_id={src} ({len(recap)} chars)")
    print("-" * 72 + "\n--- OLD ---\n" + old_text)
    print("-" * 72 + "\n--- NEW ---\n" + new_text)
    print("=" * 72)

    if args.dry_run:
        print("DRY-RUN — Slack not touched. Re-run without --dry-run to apply.")
        return 0

    try:
        client.chat_update(channel=channel, ts=ts, text=new_text)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: chat_update failed: {e}", file=sys.stderr)
        log.error("agenda_update_recap_failed", agenda_id=args.agenda_id, error=str(e))
        return 1
    log.info("agenda_update_recap_done", agenda_id=args.agenda_id,
             source_zoom_id=src, channel=channel, ts=ts)
    print("DONE — message updated in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
