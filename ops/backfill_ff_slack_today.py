"""FR-CR-05-194 backfill — publish TODAY's already-processed Fireflies
summaries to Slack that never went out because AUTO_SEND_TO_SLACK_ENABLED was
off at the time they were processed.

Uses the SAME path as the live pipeline (`publish_zoom_recording_to_slack`),
so formatting is identical, and it's idempotent (rows that already carry a
`slack_post_ts` are skipped via `already_posted`). Channel + token are resolved
exactly like `maybe_auto_publish` (env `AUTO_SEND_TO_SLACK_CHANNEL` /
`AUTO_SEND_TO_SLACK_TOKEN_KEY`), but the master `AUTO_SEND_TO_SLACK_ENABLED`
gate is intentionally NOT required here — this is an explicit operator backfill.

Scope: FF recordings with `meeting_date >= --since` (default = today UTC),
`tasks_extracted = true`, non-empty `short_summary`, and no `slack_post_ts`.

Run in the FF container (has the CEO-brain token + DB):
    docker exec manager-zoom-ff-1 python -m ops.backfill_ff_slack_today --dry-run
    docker exec manager-zoom-ff-1 python -m ops.backfill_ff_slack_today
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import MeetingRecording
from app.services.slack_publish import (
    _get_channel,
    _get_token_key,
    publish_zoom_recording_to_slack,
)

log = get_logger(__name__)


def main() -> int:
    setup_logging()
    s = get_settings()
    ap = argparse.ArgumentParser(description="Backfill today's FF summaries to Slack")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="meeting_date >= this UTC date (default: today UTC)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list rows that WOULD post; no Slack write")
    args = ap.parse_args()

    if args.since:
        try:
            since = _dt.date.fromisoformat(args.since)
        except ValueError:
            print(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}",
                  file=sys.stderr)
            return 2
    else:
        since = _dt.datetime.now(_dt.timezone.utc).date()
    since_dt = _dt.datetime.combine(since, _dt.time.min, tzinfo=_dt.timezone.utc)

    channel = _get_channel()
    if not channel:
        print("ERROR: AUTO_SEND_TO_SLACK_CHANNEL is empty", file=sys.stderr)
        return 2
    token_key = _get_token_key()
    token = (getattr(s, token_key, "") or "").strip()
    if not token:
        print(f"ERROR: no token for token_key={token_key}", file=sys.stderr)
        return 2

    with session_scope() as session:
        rows = (
            session.query(MeetingRecording)
            .filter(MeetingRecording.meeting_date >= since_dt)
            .filter(MeetingRecording.tasks_extracted.is_(True))
            .filter(MeetingRecording.slack_post_ts.is_(None))
            .order_by(MeetingRecording.meeting_date.asc())
            .all()
        )
        candidates = [r for r in rows if (r.short_summary or "").strip()]

        print(f"{'DRY-RUN: ' if args.dry_run else ''}{len(candidates)} FF "
              f"recording(s) since {since} need Slack (channel={channel}):")
        for r in candidates:
            print(f"  id={r.id} ff={r.fireflies_id} {r.meeting_date} "
                  f"«{(r.title or '')[:40]}» summ={len(r.short_summary or '')}c")
        if args.dry_run or not candidates:
            return 0

        posted = failed = 0
        for r in candidates:
            try:
                res = publish_zoom_recording_to_slack(
                    session, r, channel=channel, token=token)
            except Exception as e:  # noqa: BLE001
                failed += 1
                log.error("ff_backfill_publish_exception",
                          fireflies_id=r.fireflies_id, error=str(e))
                continue
            if res.get("ok"):
                posted += 1
                # commit immediately — Slack post is irreversible (mirror the
                # pipeline's commit-now discipline so a crash can't re-post).
                try:
                    session.commit()
                except Exception as ce:  # noqa: BLE001
                    session.rollback()
                    log.warning("ff_backfill_commit_failed",
                                fireflies_id=r.fireflies_id, error=str(ce))
                log.info("ff_backfill_posted", fireflies_id=r.fireflies_id,
                         parent_ts=res.get("parent_ts"),
                         skipped=res.get("skipped_reason"))
            else:
                failed += 1
                log.warning("ff_backfill_failed", fireflies_id=r.fireflies_id,
                            error=res.get("error"), step=res.get("step"))

    print(f"DONE: posted={posted} failed={failed} (of {len(candidates)})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
