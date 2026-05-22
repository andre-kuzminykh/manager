"""Operator-pinned 2026-05-21 — post per-meeting summaries 19-21 May
to Slack в новом формате:

  - Parent message: HEADER (DD/MM - Title, гиперссылка на Google Doc)
    + короткое саммери body (БЕЗ To-Do).
  - Thread reply под parent: To-Do list (по одному паграфу — Slack
    отдельно рендерит).

Это отличается от текущего пайплайн-пути (где To-Do приклеивается
к телу parent-сообщения). Operator-pinned: «гиперссылка, короткое
саммери, задачи в треде».

Использует ту же Slack-конвертацию (mrkdwn) что и
`app.services.slack_mirror.post_meeting_summary_to_slack`, но шлёт
parent + thread напрямую через slack_sdk WebClient.

Безопасность:
  - SKIP, если `row.short_summary_sent` == True (уже отправлено)
  - SKIP, если нет short_summary / google_doc_url / detailed_summary
  - --dry-run: ничего не шлёт, печатает что бы отправилось
  - --yes: skip interactive confirm

Usage:
    docker compose exec -T bot python -m ops.send_summaries_19_21 \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10 --dry-run

    # реально шлём:
    docker compose exec -T bot python -m ops.send_summaries_19_21 \\
        --start 2026-05-19 --end 2026-05-22 --min-minutes 10 --yes
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.fireflies.pipeline import _strip_llm_todo_block
from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, Task, TaskSourceKind, ZoomRecording
from app.services.slack_mirror import (
    SLACK_TEXT_CHUNK_CHARS,
    _compact_for_slack,
    _split_for_slack,
    _to_slack_mrkdwn,
)
from app.services.task_direction import (
    DIRECTIONS_IMPORTANT,
    classify_directions,
)


def _is_ready(row) -> bool:
    if row.short_summary_sent:
        return False
    if not (row.transcript_text or "").strip():
        return False
    if not (row.detailed_summary or "").strip():
        return False
    if not (row.short_summary or "").strip():
        return False
    if not (row.google_doc_url or "").strip():
        return False
    return True


def _gather_candidates(
    session, *, start: datetime, end: datetime, min_secs: int,
) -> list[tuple[datetime, str, object]]:
    out: list[tuple[datetime, str, object]] = []
    for r in session.query(ZoomRecording).filter(
        ZoomRecording.meeting_date >= start,
        ZoomRecording.meeting_date < end,
    ).all():
        if (r.duration_seconds or 0) < min_secs:
            continue
        if not _is_ready(r):
            continue
        out.append((r.meeting_date, "zoom", r))
    for r in session.query(MeetingRecording).filter(
        MeetingRecording.meeting_date >= start,
        MeetingRecording.meeting_date < end,
    ).all():
        if (r.duration_seconds or 0) < min_secs:
            continue
        if not _is_ready(r):
            continue
        out.append((r.meeting_date, "fireflies", r))
    out.sort(key=lambda x: x[0])
    return out


def _extract_important_tasks_ephemeral(
    row,
    *,
    settings,
    llm_backend,
) -> list[dict]:
    """FR-CR-05-178 — ephemeral task extraction at send time.

    Calls TASK_EXTRACTION_SYSTEM LLM on row.transcript_text +
    detailed_summary, classifies each task by direction, and
    returns ONLY tasks with direction ∈ DIRECTIONS_IMPORTANT.
    Nothing is persisted to DB.

    Returns: list of {"title": str, "owner": str, "direction": str}.
    """
    import json as _json

    from app.db import session_scope as _session_scope
    from app.fireflies.pipeline import _render_known_employees_table
    from app.models import TeamMember as _TM
    from app.services.team_members import as_known_employees

    transcript = (row.transcript_text or "").strip()
    detailed = (row.detailed_summary or "").strip()
    if not transcript and not detailed:
        return []

    # FR-CR-05-192k — load the same `known_employees` directory the
    # live `_step_extract_tasks` passes to TASK_EXTRACTION_SYSTEM,
    # so the LLM can pick a canonical real_name based on Role +
    # Notes (instead of grabbing the closest «@email» mention from
    # the transcript). Pre-fetch the resolver maps in the same
    # session so we don't reopen.
    with _session_scope() as _sess:
        known_employees = as_known_employees(
            _sess, prefer_telegram=False,
        )
        tm_by_email: dict[str, str] = {}
        tm_by_slack: dict[str, str] = {}
        tm_by_telegram: dict[str, str] = {}
        tm_real_names: set[str] = set()
        for _m in (
            _sess.query(_TM)
            .filter(_TM.real_name.isnot(None))
            .filter(_TM.active.is_(True))
            .all()
        ):
            if _m.email:
                tm_by_email[_m.email.lower().strip()] = _m.real_name
            if _m.slack_user_id:
                tm_by_slack[_m.slack_user_id] = _m.real_name
            if _m.telegram_user_id is not None:
                tm_by_telegram[str(_m.telegram_user_id)] = _m.real_name
            tm_real_names.add(_m.real_name)
    known_table = _render_known_employees_table(known_employees)
    # Build the participants line — same precedence as the pipeline.
    parts: list[str] = []
    cal = row.calendar_attendees or []
    if isinstance(cal, list) and cal:
        for a in cal:
            if isinstance(a, dict):
                name = (
                    a.get("resolved_name") or a.get("display_name")
                    or a.get("email") or ""
                ).strip()
                if name:
                    parts.append(name)
    if not parts and row.participants:
        parts = [p for p in row.participants if p]
    participants_line = (
        ("meeting_participants:\n  - " + "\n  - ".join(parts))
        if parts else ""
    )
    # FR-CR-05-185 — pass today's date so the LLM can resolve relative
    # deadlines («завтра», «в понедельник», «к концу июня») into
    # absolute ISO `YYYY-MM-DD` values for the `due_date` field.
    # FR-CR-05-192k — known_employees TABLE so LLM picks canonical
    # owners by Role/Notes (not by «closest email mention»).
    from datetime import date as _date

    user_prompt = (
        "Return JSON: `{\"tasks\": [{\"title\": ..., "
        "\"description\": ..., \"owner\": <ONE of: slack_user_id from "
        "the table OR exact real_name from the table — NEVER a raw "
        "email>, \"priority\": ..., \"due_date\": \"YYYY-MM-DD or null\", "
        "\"due_time\": \"HH:MM or null\"}, ...]}`. Empty list ok.\n\n"
        "known_employees (pick owner from this table — use Role + "
        "Notes to disambiguate, NEVER emit a raw email as owner):\n"
        f"{known_table}\n\n"
        f"today_date: {_date.today().isoformat()}\n"
        f"meeting_date: "
        f"{row.meeting_date.date().isoformat() if row.meeting_date else ''}\n"
        f"Заголовок: {row.title or '(без названия)'}\n"
        f"{participants_line}\n\n"
        "ДЕТАЛЬНОЕ САММЕРИ:\n"
        f"{detailed[:15000]}\n\n"
        "ТРАНСКРИПТ:\n"
        f"{transcript[:60000]}"
    )
    try:
        raw = llm_backend.complete_text(
            system_prompt=TASK_EXTRACTION_SYSTEM,
            user_prompt=user_prompt,
            model=settings.fireflies_tasks_model,
            reasoning_effort=(
                settings.fireflies_tasks_reasoning_effort or None
            ),
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        print(f"    WARN: ephemeral task extraction failed: {e}")
        return []
    try:
        parsed = _json.loads(raw) if raw else {}
    except _json.JSONDecodeError:
        return []
    raw_tasks = (parsed or {}).get("tasks") or []
    if not isinstance(raw_tasks, list):
        return []
    # Direction classification: assign sequential ids so the
    # classifier returns {id: direction}.
    direction_input = [
        {
            "id": i,
            "title": (t.get("title") or "")[:200],
            "description": (t.get("description") or "")[:300],
        }
        for i, t in enumerate(raw_tasks)
        if isinstance(t, dict)
    ]
    directions = classify_directions(
        tasks=direction_input,
        meeting_context=detailed[:3000] or None,
        llm_backend=llm_backend,
        model=settings.fireflies_tasks_model,
    )
    # FR-CR-05-192k — owner resolver with 4-step lookup:
    #   1. slack_user_id (LLM picked U… from the known_employees table)
    #   2. telegram_user_id (LLM picked the numeric id when the row
    #      had no slack_user_id — `as_known_employees` falls back to
    #      TG numeric so the LLM never sees an empty primary_id cell)
    #   3. email (LLM regressed and emitted «@…» despite the prompt)
    #   4. exact real_name match (LLM did the right thing, no resolve)
    # All maps were prefetched above inside `_session_scope`.
    def _resolve_owner(owner_raw: str) -> str:
        if not owner_raw:
            return ""
        o = owner_raw.strip()
        if o in tm_by_slack:
            return tm_by_slack[o]
        if o.isdigit() and o in tm_by_telegram:
            return tm_by_telegram[o]
        if "@" in o:
            hit = tm_by_email.get(o.lower())
            if hit:
                return hit
        # Exact real_name passthrough (idempotent).
        if o in tm_real_names:
            return o
        return o

    out: list[dict] = []
    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            continue
        d = directions.get(i, "other")
        if d not in DIRECTIONS_IMPORTANT:
            continue
        out.append({
            "title": (t.get("title") or "").strip(),
            "description": (t.get("description") or "").strip(),
            "owner": _resolve_owner(t.get("owner") or ""),
            "direction": d,
            # Forward the LLM-emitted deadline so the renderer can
            # use it instead of the fallback «today 18:00».
            "due_date": (t.get("due_date") or "").strip() or None,
            "due_time": (t.get("due_time") or "").strip() or None,
        })
    return out


def _render_tasks_block(tasks: list[dict]) -> str:
    """FR-CR-05-178 / FR-CR-05-184 — numbered To-Do for the Slack
    thread reply. Mirrors `app.fireflies.pipeline._build_todo_section`:

        N) <description (else title)> — <owner> • DD.MM.YYYY HH:MM

    FR-CR-05-185 — deadline source order:
      1. task['due_date'] (LLM-extracted ISO YYYY-MM-DD) +
         task['due_time'] (HH:MM); time defaults to 18:00 when
         the LLM emits a date but no time.
      2. fallback: today 18:00.
    """
    if not tasks:
        return ""
    from datetime import date as _date, datetime, time as _time

    today_18 = datetime.combine(
        _date.today(), _time(18, 0),
    ).strftime("%d.%m.%Y %H:%M")

    def _format_deadline(t: dict) -> str:
        raw_date = (t.get("due_date") or "").strip()
        raw_time = (t.get("due_time") or "").strip()
        if not raw_date:
            return today_18
        try:
            d = _date.fromisoformat(raw_date)
        except ValueError:
            return today_18
        if raw_time:
            try:
                hh, mm = raw_time.split(":")
                tt = _time(int(hh), int(mm))
            except (ValueError, TypeError):
                tt = _time(18, 0)
        else:
            tt = _time(18, 0)
        return datetime.combine(d, tt).strftime("%d.%m.%Y %H:%M")

    lines: list[str] = []
    for i, t in enumerate(tasks, start=1):
        body_text = (
            (t.get("description") or "").strip()
            or (t.get("title") or "").strip()
            or "(без описания)"
        )
        # Match _build_todo_section: cap at 350 chars to keep the
        # Slack reply scannable; soft-cut on the last space before
        # the boundary.
        if len(body_text) > 350:
            cut = body_text.rfind(" ", 0, 350)
            body_text = (
                body_text[: cut if cut > 200 else 350].rstrip(",;:- ")
                + "…"
            )
        owner = (t.get("owner") or "").strip()
        suffix_parts: list[str] = []
        if owner:
            suffix_parts.append(owner)
        suffix_parts.append(_format_deadline(t))
        lines.append(f"{i}) {body_text} — {' • '.join(suffix_parts)}")
    return "\n\n".join(lines)


def _split_parent_and_tasks(short_summary: str) -> tuple[str, str]:
    """Returns (parent_body, tasks_block).
    `_strip_llm_todo_block` removes the deterministic / LLM To-Do
    section; whatever it removed is the tasks block.
    """
    parent = _strip_llm_todo_block(short_summary).rstrip()
    # `_TODO_SECTION_HEADERS_RE` matched what's between parent and
    # end-of-body; recover it by diffing.
    if parent == short_summary.rstrip():
        return parent, ""
    # The block we stripped is the suffix after `parent` (with the
    # leading «\n\n» eaten by the strip).
    suffix = short_summary[len(parent):].lstrip("\n").rstrip()
    return parent, suffix


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--min-minutes", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument(
        "--sleep", type=float, default=1.5,
        help="Seconds between Slack posts (rate-limit cushion).",
    )
    args = ap.parse_args()

    s = get_settings()
    token = s.slack_bot_token
    channel = s.slack_meeting_channel_id
    if not args.dry_run and (not token or not channel):
        print(
            "ERROR: SLACK_BOT_TOKEN and SLACK_MEETING_CHANNEL_ID "
            "must be set unless --dry-run.",
            file=sys.stderr,
        )
        return 2

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    min_secs = args.min_minutes * 60

    with session_scope() as session:
        cands = _gather_candidates(
            session, start=start, end=end, min_secs=min_secs,
        )
        if not cands:
            print("Nothing to send (no READY rows in range).")
            return 0

        print(f"\n# Sending {len(cands)} meetings to Slack channel "
              f"{channel} (chronological order):\n")
        for i, (dt, src, r) in enumerate(cands, start=1):
            print(
                f"  {i:>2}. {dt.strftime('%m-%d %H:%M')}  {src:<10} "
                f"{(r.title or '')[:60]}"
            )
        if args.dry_run:
            print("\n--dry-run set, exiting before any Slack call.\n")
        elif not args.yes:
            ans = input("\nProceed and post to Slack? [yes/N]: ").strip().lower()
            if ans != "yes":
                print("Aborted.")
                return 1

        if not args.dry_run:
            try:
                from slack_sdk import WebClient
                from slack_sdk.errors import SlackApiError
            except ImportError:
                print("ERROR: slack_sdk not installed.", file=sys.stderr)
                return 3
            client = WebClient(token=token)
        else:
            client = None
            SlackApiError = Exception  # noqa: N806

        # FR-CR-05-178 — LLM backend for ephemeral task extraction.
        # No Task rows are persisted; we just call the LLM, classify
        # by direction, render to Slack thread, discard.
        from openai import OpenAI
        openai_client = OpenAI(api_key=s.openai_api_key)
        ephemeral_llm = OpenAIBackend(
            client=openai_client,
            model=s.fireflies_tasks_model,
        )

        sent = 0
        for i, (dt, src, r) in enumerate(cands, start=1):
            # Parent text: strip any LLM-emitted To-Do from existing
            # short_summary. We DO NOT use the existing deterministic
            # To-Do (which would read DB Task rows — in mock mode
            # there are none anyway).
            parent_text = _strip_llm_todo_block(
                r.short_summary or ""
            ).rstrip()
            # Tasks block: ephemeral LLM extraction + DIRECTIONS
            # filter, NO DB writes.
            tasks_list = _extract_important_tasks_ephemeral(
                r, settings=s, llm_backend=ephemeral_llm,
            )
            tasks_text = _render_tasks_block(tasks_list)

            parent_text = _compact_for_slack(_to_slack_mrkdwn(parent_text))
            if tasks_text:
                tasks_text = _compact_for_slack(_to_slack_mrkdwn(tasks_text))
            # Parent чанки на случай если body > 35K (редко, но
            # «Weekly Top Management» 2ч транскрипт мог разнести
            # короткое до сотни Kчар если LLM не сжал).
            parent_chunks = _split_for_slack(
                parent_text, limit=SLACK_TEXT_CHUNK_CHARS,
            )
            print(
                f"\n=== [{i}/{len(cands)}] {dt.strftime('%m-%d %H:%M')} "
                f"{src} | {(r.title or '')[:50]}"
            )
            print(
                f"    parent chunks: {len(parent_chunks)}   "
                f"tasks block chars: {len(tasks_text)}"
            )
            if args.dry_run:
                print(f"    parent[0] preview: "
                      f"{parent_chunks[0][:200].replace(chr(10), ' / ')}…")
                if tasks_list:
                    print(f"    important tasks ({len(tasks_list)}):")
                    for j, t in enumerate(tasks_list, start=1):
                        owner = f" — {t['owner']}" if t['owner'] else ""
                        print(f"      {j}) [{t['direction']}] "
                              f"{t['title'][:80]}{owner}")
                else:
                    print("    important tasks: (none)")
                continue

            parent_ts = None
            try:
                resp = client.chat_postMessage(
                    channel=channel,
                    text=parent_chunks[0],
                    unfurl_links=False,
                    unfurl_media=False,
                )
                data = resp.data if hasattr(resp, "data") else dict(resp)
                parent_ts = data.get("ts")
                # Any continuation chunks of parent body → thread.
                for c in parent_chunks[1:]:
                    client.chat_postMessage(
                        channel=channel, text=c,
                        thread_ts=parent_ts,
                        unfurl_links=False, unfurl_media=False,
                    )
                    time.sleep(args.sleep)
                # Tasks in thread (always a separate reply, never
                # appended to parent).
                if tasks_text:
                    client.chat_postMessage(
                        channel=channel, text=tasks_text,
                        thread_ts=parent_ts,
                        unfurl_links=False, unfurl_media=False,
                    )
            except SlackApiError as e:  # noqa: BLE001
                err = (
                    e.response.data.get("error")
                    if e.response is not None
                    and isinstance(e.response.data, dict)
                    else str(e)
                )
                print(f"    ERROR: Slack rejected: {err}")
                continue

            r.short_summary_sent = True
            # FR-CR-05-178 — defensive: hard-DELETE any Task rows
            # that may have slipped through (e.g. if batch was run
            # without --skip-tasks). In normal flow this finds zero
            # rows since reprocess used --skip-tasks.
            sk = (TaskSourceKind.zoom if src == "zoom"
                  else TaskSourceKind.fireflies)
            sid = r.zoom_id if src == "zoom" else r.fireflies_id
            wiped = (
                session.query(Task)
                .filter(Task.source_kind == sk)
                .filter(Task.source_conversation_id == sid)
                .delete(synchronize_session=False)
            )
            session.flush()
            sent += 1
            print(
                f"    posted ts={parent_ts}    "
                f"ephemeral tasks={len(tasks_list)}   "
                f"defensive-deleted {wiped} DB tasks"
            )
            time.sleep(args.sleep)

        session.commit()
        print(f"\nDone. Sent: {sent}/{len(cands)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
