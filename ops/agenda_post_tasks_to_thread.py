"""One-off: post an agenda's TASK thread-replies under an EXISTING Slack
parent message — WITHOUT creating a new agenda.

Use case: the «Статус задач к обсуждению» thread replies under an already-posted
agenda were deleted; restore them under the SAME parent, reusing the real
compose + render logic (correct task statuses / formatting).

Works even when the meeting is already PAST: instead of fetching the calendar
(future-only), it synthesises the event from the stored ``meeting_agendas`` row
(title + start + recurring id) and lets ``build_candidates`` enrich it from the
DB (prior recordings + open tasks). The dedup row is temporarily dropped so the
candidate is built, then re-inserted unchanged so the scheduled runner does NOT
post a duplicate.

Usage:
    python -m ops.agenda_post_tasks_to_thread \\
        --thread-ts 1779781186.549319 \\
        --calendar-event-id <EV_ID> [--dry-run]
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
from ops.agenda_run_once import _build_runner_for_oneshot

log = get_logger(__name__)

_SNAPSHOT_COLS = (
    "calendar_event_id", "recurring_event_id", "title", "title_normalised",
    "scheduled_start_at", "posted_at", "slack_channel", "slack_ts",
    "google_doc_id", "google_doc_url", "prior_meeting_zoom_ids",
)


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--thread-ts", required=True, help="ts существующего parent-сообщения агенды")
    ap.add_argument("--calendar-event-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ev_id = args.calendar_event_id

    runner = _build_runner_for_oneshot()
    if runner is None:
        print("ERROR: не удалось собрать runner (нет llm креды?)", file=sys.stderr)
        return 2
    s = runner._settings  # noqa: SLF001

    # 1) Load the existing dedup row → синтетическое событие + снимок для восстановления.
    with session_scope() as session:
        row = session.query(MeetingAgenda).filter(
            MeetingAgenda.calendar_event_id == ev_id
        ).one_or_none()
        if row is None:
            print(f"нет meeting_agendas строки для event-id {ev_id!r}", file=sys.stderr)
            return 1
        snapshot = {col: getattr(row, col) for col in _SNAPSHOT_COLS}

    synth_event = {
        "id": ev_id,
        "title": snapshot["title"],
        "start": snapshot["scheduled_start_at"],
        "end": None,
        "description": "",
        "attendees": [],
        "organizer": {},
        "creator": {},
        "recurring_event_id": snapshot["recurring_event_id"],
    }

    def _restore() -> None:
        with session_scope() as session:
            exists = session.query(MeetingAgenda).filter(
                MeetingAgenda.calendar_event_id == ev_id
            ).first()
            if exists is None:
                session.add(MeetingAgenda(**snapshot))
                session.commit()

    # 2) Drop dedup so build_candidates includes this (already-posted) event.
    with session_scope() as session:
        session.query(MeetingAgenda).filter(
            MeetingAgenda.calendar_event_id == ev_id
        ).delete()
        session.commit()

    try:
        with session_scope() as session:
            cands = build_candidates(
                session,
                events=[synth_event],
                lookback_days=s.agenda_lookback_days,
                min_prior_meetings=s.agenda_min_prior_meetings,
                organizer_email=None,  # таргетим конкретное событие — org-фильтр не нужен
                # FR — «последняя prior» должна быть как НА МОМЕНТ постинга агенды
                # (до самой встречи), иначе после прошедшей встречи last-prior
                # смещается на неё (0 задач). Берём момент создания агенды.
                now=snapshot["posted_at"] or snapshot["scheduled_start_at"],
            )
        if not cands:
            print(
                "build_candidates пусто — нет prior-записей с тем же title "
                "или не достигнут min_prior_meetings.",
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
            return 0

        if not replies:
            print("у агенды нет задач к обсуждению — постить нечего.")
            return 0

        posted = 0
        for r in replies:
            ts = runner._send_slack_dm(r, thread_ts=args.thread_ts)  # noqa: SLF001
            if ts:
                posted += 1
            else:
                print("  предупреждение: реплай не отправился", file=sys.stderr)
        print(f"запостил {posted}/{len(replies)} реплаев в тред {args.thread_ts}")
        return 0
    finally:
        _restore()
        print("dedup-строка восстановлена — плановый раннер дубль не запостит.")


if __name__ == "__main__":
    sys.exit(main())
