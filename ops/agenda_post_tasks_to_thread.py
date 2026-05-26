"""One-off: post an agenda's TASK thread-replies under an EXISTING Slack
parent message — WITHOUT creating a new agenda.

Use case: the «Статус задач к обсуждению» thread replies under an already-posted
agenda were deleted; we want to restore them under the SAME parent, reusing the
real compose + render logic (correct task statuses / formatting).

It rebuilds the candidate for the given calendar event, composes the agenda,
renders ONLY the task thread-replies, posts them to ``--thread-ts``, and
re-points the meeting_agendas dedup row at that existing parent so the
scheduled runner does NOT post a duplicate.

Usage:
    python -m ops.agenda_post_tasks_to_thread \\
        --thread-ts 1779781186.549319 \\
        --calendar-event-id <EV_ID> \\
        --lookahead-minutes 2880 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys

from app.agenda.compose import compose_agenda
from app.agenda.service import build_candidates
from app.agenda.slack_format import render_agenda_task_thread_replies
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models import MeetingAgenda
from ops.agenda_run_once import _build_runner_for_oneshot, _fetch_events_wide

log = get_logger(__name__)


def _prior_zoom_ids(candidate) -> list[str]:
    return [r.get("zoom_id") for r in (candidate.prior_recordings or []) if r.get("zoom_id")]


def _restore_dedup(runner, candidate, thread_ts: str) -> None:
    """Re-insert the dedup row pointing at the EXISTING parent so the
    scheduled runner treats the event as already-posted."""
    with session_scope() as session:
        runner._svc.record_post(  # noqa: SLF001
            session,
            candidate=candidate,
            slack_channel=runner._settings.agenda_slack_target_channel_id,  # noqa: SLF001
            slack_ts=thread_ts,
            google_doc_id=None,
            google_doc_url=None,
            prior_zoom_ids=_prior_zoom_ids(candidate),
        )
        session.commit()


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--thread-ts", required=True, help="ts существующего parent-сообщения агенды")
    ap.add_argument("--calendar-event-id", required=True)
    ap.add_argument("--lookahead-minutes", type=int, default=2880)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    runner = _build_runner_for_oneshot()
    if runner is None:
        print("ERROR: не удалось собрать runner (нет calendar/llm креды?)", file=sys.stderr)
        return 2
    s = runner._settings  # noqa: SLF001

    events = [e for e in _fetch_events_wide(runner, args.lookahead_minutes) if e]
    events = [e for e in events if e.get("id") == args.calendar_event_id]
    if not events:
        print(
            f"событие {args.calendar_event_id!r} не найдено в окне "
            f"{args.lookahead_minutes} мин — встреча уже прошла; через раннер не собрать.",
            file=sys.stderr,
        )
        return 1

    # Temp-drop the dedup row so build_candidates includes this (already-posted) event.
    with session_scope() as session:
        session.query(MeetingAgenda).filter(
            MeetingAgenda.calendar_event_id == args.calendar_event_id
        ).delete()
        session.commit()

    with session_scope() as session:
        cands = build_candidates(
            session,
            events=events,
            lookback_days=s.agenda_lookback_days,
            min_prior_meetings=s.agenda_min_prior_meetings,
            organizer_email=s.zoom_required_email or None,
        )
    if not cands:
        print(
            "нет кандидата (нет прошлых записей с тем же title / не recurring / "
            "min_prior_meetings не достигнут).",
            file=sys.stderr,
        )
        return 1

    c = cands[0]
    model = s.agenda_compose_model or s.openai_model
    output = compose_agenda(c, llm_backend=runner._llm, model=model)  # noqa: SLF001
    if output is None:
        print("compose_agenda вернул None (LLM не отработал).", file=sys.stderr)
        return 1

    replies = list(render_agenda_task_thread_replies(output=output))
    print(f"кандидат={c.title!r} | open_tasks={len(c.open_tasks)} | reply_chunks={len(replies)}")

    if args.dry_run:
        for i, r in enumerate(replies, 1):
            print(f"\n----- REPLY {i}/{len(replies)} -----\n{r}")
        print("\n--dry-run: в Slack НЕ постил.")
        _restore_dedup(runner, c, args.thread_ts)  # leave no side effect
        print("dedup-строка восстановлена (без изменений в Slack).")
        return 0

    if not replies:
        print("у агенды нет задач к обсуждению — постить нечего.")
        _restore_dedup(runner, c, args.thread_ts)
        return 0

    posted = 0
    for r in replies:
        ts = runner._send_slack_dm(r, thread_ts=args.thread_ts)  # noqa: SLF001
        if ts:
            posted += 1
        else:
            print("  предупреждение: реплай не отправился", file=sys.stderr)
    print(f"запостил {posted}/{len(replies)} реплаев в тред {args.thread_ts}")

    _restore_dedup(runner, c, args.thread_ts)
    print(f"dedup-строка восстановлена (slack_ts={args.thread_ts}) — раннер дубль не запостит.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
