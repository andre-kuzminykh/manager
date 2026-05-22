"""FR-CR-05-192i — Full per-record diagnostic trace.

For a single Fireflies or Zoom recording, dumps EVERY artefact the
send pipeline touches:

  1. Record metadata (title, doc_url, dates, sizes)
  2. Calendar attendees full list (resolved name + email + rsvp +
     resolution_method)
  3. TeamMember directory entries that match the call's attendees
     (real_name, role, slack_user_id, email)
  4. Stored short_summary VERBATIM (raw HTML form)
  5. Stored detailed_summary first 400 chars
  6. All alive Task rows for this conversation (FULL Task columns
     incl. `extra` JSON — operator can see `extra.direction`,
     `extra.owner_resolution_method`, …)
  7. `_build_todo_section` rendered output — exactly what
     `--use-db-tasks` would post to the Slack thread
  8. DRY-RUN ephemeral extract (only when ``--include-ephemeral``):
     - raw LLM JSON response
     - parsed task list with title / description / owner / priority
     - direction classification per task
     - final filtered list

  9. Diagnosis section flagging:
     - tasks whose owner is an email instead of resolved real_name
     - tasks with default deadline (= meeting_date 18:00)

Read-only — no DB writes, no Slack posts. Use `--include-ephemeral`
to also fire the LLM extract (1 call ≈ $0.05-0.20 on gpt-5.5).

Usage:
    docker exec manager-bot-1 python -m ops.trace_record_full \\
        --fireflies-id 01KS0551XQZSNXQ9GGS6DSEMP7
    docker exec manager-bot-1 python -m ops.trace_record_full \\
        --zoom-id "k9We5mXQRsy3aiv5rOOsHg==" --include-ephemeral
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import time as dtime

from app.config import get_settings
from app.db import session_scope
from app.fireflies.pipeline import _build_todo_section
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, TeamMember, ZoomRecording
from app.models.task import Task, TaskSourceKind


def _ruler(s: str = "") -> None:
    print()
    print("─" * 100)
    if s:
        print(s)
        print("─" * 100)


def _section(s: str) -> None:
    print()
    print("=" * 100)
    print(s)
    print("=" * 100)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fireflies-id", default=None)
    ap.add_argument("--zoom-id", default=None)
    ap.add_argument(
        "--include-ephemeral", action="store_true",
        help="Also fire ephemeral LLM task extraction (costs $0.05+, "
             "1-3 min latency on long transcripts with reasoning=high).",
    )
    args = ap.parse_args()
    if not (args.fireflies_id or args.zoom_id):
        print("ERROR: --fireflies-id or --zoom-id required", file=sys.stderr)
        return 1

    s = get_settings()
    with session_scope() as session:
        if args.fireflies_id:
            r = session.query(MeetingRecording).filter(
                MeetingRecording.fireflies_id == args.fireflies_id,
            ).first()
            src = "fireflies"
            conv_id = args.fireflies_id
            src_kind = TaskSourceKind.fireflies
        else:
            r = session.query(ZoomRecording).filter(
                ZoomRecording.zoom_id == args.zoom_id,
            ).first()
            src = "zoom"
            conv_id = args.zoom_id
            src_kind = TaskSourceKind.zoom
        if r is None:
            print("ERROR: record not found", file=sys.stderr)
            return 2

        # ---- 1) Metadata ---- #
        _section(f"[1] RECORD METADATA  ({src})")
        print(f"  conv_id            : {conv_id}")
        print(f"  title              : {r.title}")
        print(f"  meeting_date       : {r.meeting_date.isoformat() if r.meeting_date else '—'}")
        print(f"  duration_seconds   : {r.duration_seconds or 0}  ({(r.duration_seconds or 0) // 60} min)")
        print(f"  google_doc_url     : {r.google_doc_url or '—'}")
        print(f"  transcript_chars   : {len((r.transcript_text or ''))}")
        print(f"  detailed_chars     : {len((r.detailed_summary or ''))}")
        print(f"  short_chars        : {len((r.short_summary or ''))}")
        print(f"  short_summary_sent : {bool(getattr(r, 'short_summary_sent', False))}")
        print(f"  last_error         : {getattr(r, 'last_error', None) or '—'}")
        if hasattr(r, "participants"):
            ps = list(r.participants or [])
            print(f"  fireflies/zoom raw participants ({len(ps)}):")
            for p in ps[:15]:
                print(f"    · {p}")
            if len(ps) > 15:
                print(f"    · ...and {len(ps) - 15} more")

        # ---- 2) Calendar attendees ---- #
        _section("[2] CALENDAR ATTENDEES (from row.calendar_attendees)")
        cal = r.calendar_attendees or []
        if not isinstance(cal, list) or not cal:
            print("  (empty)")
        else:
            print(
                f"  total={len(cal)}  resolved={sum(1 for a in cal if isinstance(a, dict) and a.get('resolved_name'))}"
            )
            for a in cal:
                if not isinstance(a, dict):
                    continue
                nm = (a.get("resolved_name") or a.get("display_name") or "—")[:30]
                em = (a.get("email") or "—")[:40]
                rsvp = a.get("response_status") or "—"
                method = a.get("resolution_method") or "—"
                src_label = a.get("source") or "—"
                print(
                    f"    · {nm:<30} {em:<40} rsvp={rsvp:<14} "
                    f"src={src_label:<14} method={method}"
                )

        # ---- 3) TeamMember matches by email ---- #
        _section("[3] TeamMember ROWS MATCHING CALENDAR EMAILS")
        emails = sorted({
            (a.get("email") or "").lower().strip()
            for a in cal if isinstance(a, dict)
        } - {""})
        if not emails:
            print("  (no emails in calendar_attendees)")
        else:
            tms = session.query(TeamMember).filter(
                TeamMember.email.in_(emails),
            ).all()
            tm_by_email = {(m.email or "").lower(): m for m in tms}
            print(
                f"  {len(tm_by_email)}/{len(emails)} of calendar emails "
                f"have a TeamMember row\n"
            )
            for em in emails:
                m = tm_by_email.get(em)
                if m:
                    print(
                        f"    ✓ {em:<40} → real_name='{m.real_name}'  "
                        f"role='{m.role or '—'}'  slack_user_id='{m.slack_user_id or '—'}'  "
                        f"active={m.active}"
                    )
                else:
                    print(f"    ✗ {em:<40} (no TeamMember row)")

        # ---- 4) Short summary verbatim ---- #
        _section("[4] STORED short_summary (raw HTML — what send_one_* will use)")
        print(r.short_summary or "(empty)")

        # ---- 5) Detailed summary preview ---- #
        _section("[5] STORED detailed_summary  (first 600 chars)")
        det = (r.detailed_summary or "")
        print(det[:600] + ("…" if len(det) > 600 else ""))

        # ---- 6) DB Task rows full dump ---- #
        _section("[6] DB Task ROWS (alive, deleted_at IS NULL)")
        tasks = (
            session.query(Task)
            .filter(Task.source_kind == src_kind)
            .filter(Task.source_conversation_id == conv_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id)
            .all()
        )
        print(f"  count={len(tasks)}")
        if not tasks:
            print("  (no tasks — nothing for `--use-db-tasks` to render)")
        for t in tasks:
            extra = {}
            try:
                extra = t.extra or {}
                if isinstance(extra, str):
                    extra = json.loads(extra)
            except Exception:  # noqa: BLE001
                extra = {}
            owner = (t.owner_display_name or "").strip()
            owner_is_email = "@" in owner
            dl_str = (
                t.due_date.strftime("%d.%m.%Y")
                + (f" {t.due_time.strftime('%H:%M')}" if t.due_time else "")
            ) if t.due_date else "—"
            email_tag = "  ⚠ EMAIL OWNER" if owner_is_email else ""
            print(
                f"\n    Task #{t.id}{email_tag}\n"
                f"      title       : {(t.title or '')[:120]}\n"
                f"      description : {(t.description or '')[:200]}\n"
                f"      owner       : {owner or '—'}\n"
                f"      due         : {dl_str}\n"
                f"      category    : {t.category or '—'}\n"
                f"      priority    : {t.priority}\n"
                f"      status      : {t.status}\n"
                f"      extra       : {json.dumps(extra, ensure_ascii=False)[:250]}"
            )

        # ---- 7) _build_todo_section rendered ---- #
        _section("[7] _build_todo_section RENDER (what --use-db-tasks posts)")
        todo_rendered = _build_todo_section(
            session,
            source_kind=src_kind,
            source_conversation_id=conv_id,
        )
        if not todo_rendered:
            print("  (empty — no important-direction tasks → no thread reply)")
        else:
            print(todo_rendered)

        # ---- 8) Ephemeral extract (optional) ---- #
        if args.include_ephemeral:
            _section("[8] DRY-RUN EPHEMERAL EXTRACT  (live LLM call)")
            print("  Firing TASK_EXTRACTION_SYSTEM on transcript…")
            from openai import OpenAI
            oc = OpenAI(api_key=s.openai_api_key)
            llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)
            from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM
            # Build same prompt as _extract_important_tasks_ephemeral
            transcript = (r.transcript_text or "").strip()
            detailed = (r.detailed_summary or "").strip()
            parts: list[str] = []
            for a in (r.calendar_attendees or []):
                if isinstance(a, dict):
                    nm = (
                        a.get("resolved_name") or a.get("display_name")
                        or a.get("email") or ""
                    ).strip()
                    if nm:
                        parts.append(nm)
            participants_line = (
                ("meeting_participants:\n  - " + "\n  - ".join(parts))
                if parts else ""
            )
            user_prompt = (
                "Return JSON: `{\"tasks\": [{\"title\": ..., "
                "\"description\": ..., \"owner\": ..., "
                "\"priority\": ...}, ...]}`. Empty list ok.\n\n"
                f"Заголовок: {r.title or '(без названия)'}\n"
                f"{participants_line}\n\n"
                "ДЕТАЛЬНОЕ САММЕРИ:\n"
                f"{detailed[:15000]}\n\n"
                "ТРАНСКРИПТ:\n"
                f"{transcript[:60000]}"
            )
            try:
                raw = llm.complete_text(
                    system_prompt=TASK_EXTRACTION_SYSTEM,
                    user_prompt=user_prompt,
                    model=s.fireflies_tasks_model,
                    reasoning_effort=(
                        s.fireflies_tasks_reasoning_effort or None
                    ),
                    response_format={"type": "json_object"},
                ) or ""
            except Exception as e:  # noqa: BLE001
                print(f"  ERR: {e}")
                raw = ""
            _ruler("RAW LLM JSON (first 4000 chars):")
            print(raw[:4000] + ("…" if len(raw) > 4000 else ""))
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                parsed = {}
                print(f"\n  JSON parse error: {e}")
            tasks_list = (parsed or {}).get("tasks") or []
            _ruler(f"PARSED {len(tasks_list)} tasks (owner-email diagnosis)")
            for i, t in enumerate(tasks_list, start=1):
                if not isinstance(t, dict):
                    continue
                owner = (t.get("owner") or "").strip()
                owner_is_email = "@" in owner
                tag = "  ⚠ EMAIL" if owner_is_email else ""
                title = (t.get("title") or "")[:80]
                print(f"  [{i:>2}]{tag}  owner='{owner}'  title='{title}'")

        # ---- 9) Diagnosis ---- #
        _section("[9] DIAGNOSIS / RECOMMENDATIONS")
        email_owners = [
            t for t in tasks
            if "@" in (t.owner_display_name or "")
        ]
        default_due = [
            t for t in tasks
            if t.due_date == r.meeting_date.date()
            and (t.due_time is None or t.due_time == dtime(18, 0))
        ]
        if email_owners:
            print(f"  ⚠ {len(email_owners)} DB tasks have an EMAIL as owner_display_name:")
            for t in email_owners[:5]:
                print(f"      #{t.id}  owner='{t.owner_display_name}'  title='{(t.title or '')[:60]}'")
            print(
                "    → LLM TASK_EXTRACTION picked up email mentions from "
                "transcript/detailed text. Pipeline's `_step_extract_tasks` "
                "normally runs an email→real_name resolve step that the "
                "ephemeral path skips. Fix: either patch owners via "
                "directory before send, or expand TASK_EXTRACTION_SYSTEM "
                "to forbid emails in owner field."
            )
        if default_due:
            print(
                f"  ⚠ {len(default_due)} DB tasks have default deadline "
                f"(= meeting_date 18:00). LLM didn't extract real deadlines."
            )
        if not email_owners and not default_due and tasks:
            print("  ✓ tasks look clean (no email owners, no default deadlines)")
        if not tasks:
            print(
                "  · No DB tasks. Use `--no-tasks` to send parent-only "
                "(no TODO trailer, no thread)."
            )
        elif not email_owners and not default_due:
            print(
                f"  → safe to send via:  python -m ops.send_one_{src} "
                f"--{src.replace('fireflies','fireflies-id').replace('zoom','zoom-id')} "
                f"\"{conv_id}\" --channel D0ASY5QF6UX --use-db-tasks"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
