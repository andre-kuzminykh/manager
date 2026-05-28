"""FR-CR-05-192f — Focused pre-send checklist for the 9 records
that ship to Artem (19-21 May 2026).

One row per record × the columns the operator asked for:

   1. title                — meeting title
   2. link                 — first line wrapped in <a href> (FR-CR-05-127)
   3. biling (zoom only)   — transcript has BOTH Cyrillic AND Latin
                             script ≥ 3 % (proxy for FR-CR-05-170
                             bilingual restoration)
   4. det_ppl              — # of canonical TeamMember names mentioned
                             in detailed_summary  (= «нашли людей»)
   5. det_cp               — # of canonical Counterparty names
                             mentioned in detailed_summary
                             (= «нашли контрагентов»)
   6. short                — short_summary present
   7. tsk                  — # tasks parsed from TODO block of
                             short_summary
   8. tsk_own              — every task owner exists in TeamMember
                             (= «у задач есть ответственный»)
   9. tsk_dl               — every task deadline is non-default
                             (= «дедлайн отдельно определился»,
                             not the meeting_date 18:00 fallback)

Default-targets the canonical 9-record send list; override via
``--include-id <zoom_id>`` / ``--include-fid <fireflies_id>``.

Usage:
    docker exec manager-bot-1 python -m ops.microcheck_send_list

    docker exec manager-bot-1 python -m ops.microcheck_send_list \\
        --include-id "k9We5mXQRsy3aiv5rOOsHg==" \\
        --include-fid "01KS0551XQZSNXQ9GGS6DSEMP7"
"""
from __future__ import annotations

import argparse
import re
import sys

from datetime import time as dtime

from app.db import session_scope
from app.fireflies.pipeline import _build_todo_section
from app.models import MeetingRecording, TeamMember, ZoomRecording
from app.models.counterparty import Counterparty
from app.models.task import Task, TaskSourceKind
from app.services.slack_mirror import (
    _compact_for_slack,
    _to_slack_mrkdwn,
)
from app.services.task_direction import DIRECTIONS_IMPORTANT
from ops._send_helpers import build_parent_raw


def _task_direction(t: Task) -> str | None:
    """Mirror `_build_todo_section`'s lookup: t.extra is JSON, may
    carry `direction` ∈ DIRECTIONS_IMPORTANT. Tasks without that
    direction are silently dropped from the rendered thread reply."""
    try:
        extra = getattr(t, "extra", None) or {}
        if isinstance(extra, dict):
            return extra.get("direction")
    except Exception:  # noqa: BLE001
        return None
    return None


CYR_RE = re.compile(r"[А-Яа-яЁё]")
LAT_RE = re.compile(r"[A-Za-z]")
TASK_RE = re.compile(
    r"^\s*(\d+)\)\s*(.+?)\s+—\s+([^•—]+?)\s*•\s*"
    r"(\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})",
    re.MULTILINE,
)


# 9-record send list — operator pinned 2026-05-21.
DEFAULT_SEND_LIST: list[tuple[str, str]] = [
    ("fireflies", "01KS0551XQZSNXQ9GGS6DSEMP7"),  # 19/05 13:00 Object First / Dutchess
    ("zoom",      "k9We5mXQRsy3aiv5rOOsHg=="),    # 20/05 09:56 Weekly TM
    ("fireflies", "01KS2V4MZ5RXVYXMY0GPK6RKF1"),  # 20/05 14:05 Joe (Millenia)
    ("zoom",      "eQc28t2oR7u7l4ocnk6YLA=="),    # 20/05 14:18 Ирина
    ("zoom",      "8OA3y90MR3+ZCJn57oterw=="),    # 21/05 13:03 Алина, Ирина
    ("fireflies", "01KS5HS60Z2V7N1ZZV1FWVEGA1"),  # 21/05 15:15 Ben Verwaayen
    ("zoom",      "Y1qagtqRQ0OzJlS77rtHQw=="),    # 21/05 15:56 Ирина-аутрич
    ("fireflies", "01KS5PBJ7EQS4311TCXK5V7QMQ"),  # 21/05 16:35 Erik Goodman
    ("zoom",      "jzh/h42zS0WpbI9eGImdQA=="),    # 21/05 17:30 Chris Watkins
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _bilingual(text: str) -> bool:
    if not text or len(text) < 200:
        return False
    cyr = len(CYR_RE.findall(text))
    lat = len(LAT_RE.findall(text))
    total = cyr + lat
    if total == 0:
        return False
    return cyr / total >= 0.03 and lat / total >= 0.03


def _check(b: bool) -> str:
    return "✓" if b else "✗"


def _first_line(text: str) -> str:
    return (text or "").split("\n", 1)[0]


def _todo_block(text: str) -> str:
    for marker in ["To-Do:", "TODO:"]:
        if marker in (text or ""):
            return text.split(marker, 1)[1].strip()
    return ""


def _count_directory_matches(text: str, norms: list[str]) -> int:
    """Count distinct directory norms whose surface form appears as
    a case-insensitive substring of ``text`` (= «found AND
    canonicalized»: if the canonical form is in the body, the
    rewrite step succeeded for that mention)."""
    if not text:
        return 0
    body_low = text.lower()
    hits = 0
    seen: set[str] = set()
    for n in norms:
        if not n or len(n) < 3:
            continue
        if n in seen:
            continue
        if n in body_low:
            hits += 1
            seen.add(n)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--include-id", action="append", default=[],
        help="Override default 9-list with ZoomRecording.zoom_id (repeatable).",
    )
    ap.add_argument(
        "--include-fid", action="append", default=[],
        help="Override default 9-list with MeetingRecording.fireflies_id (repeatable).",
    )
    args = ap.parse_args()

    targets: list[tuple[str, str]]
    if args.include_id or args.include_fid:
        targets = (
            [("zoom", z) for z in args.include_id]
            + [("fireflies", f) for f in args.include_fid]
        )
    else:
        targets = list(DEFAULT_SEND_LIST)

    with session_scope() as session:
        members = session.query(TeamMember).filter(
            TeamMember.active.is_(True),
            TeamMember.real_name.isnot(None),
        ).all()
        member_norms = sorted({_norm(m.real_name) for m in members if m.real_name},
                              key=len, reverse=True)
        members_by_norm = {_norm(m.real_name): m for m in members if m.real_name}

        counterparties = session.query(Counterparty).all()
        cp_norms = sorted(
            {(cp.name_normalised or _norm(cp.name)) for cp in counterparties if cp.name},
            key=len, reverse=True,
        )

        rows: list[tuple[str, object]] = []
        for src, rid in targets:
            if src == "zoom":
                r = session.query(ZoomRecording).filter(
                    ZoomRecording.zoom_id == rid,
                ).first()
            else:
                r = session.query(MeetingRecording).filter(
                    MeetingRecording.fireflies_id == rid,
                ).first()
            if r is None:
                print(f"  ⚠ {src} {rid} — NOT FOUND in DB")
                continue
            rows.append((src, r))
        rows.sort(key=lambda x: x[1].meeting_date)

        report = []
        for i, (src, r) in enumerate(rows, start=1):
            short = r.short_summary or ""
            detailed = r.detailed_summary or ""
            transcript = (getattr(r, "transcript_text", None) or "")

            first = _first_line(short)
            has_link = ("<a href=" in first) or ("<http" in first)
            is_zoom = (src == "zoom")
            biling = _bilingual(transcript) if is_zoom else None

            n_ppl = _count_directory_matches(detailed, member_norms)
            n_cp = _count_directory_matches(detailed, cp_norms)
            has_short = bool(short.strip())

            # ---- Tasks straight from DB (READ-ONLY) ---- #
            # source_kind = "fireflies" or "zoom", source_conversation_id
            # = fireflies_id / zoom_id. Alive rows only (deleted_at IS NULL).
            src_kind = (
                TaskSourceKind.fireflies if src == "fireflies"
                else TaskSourceKind.zoom
            )
            conv_id = r.fireflies_id if src == "fireflies" else r.zoom_id
            db_tasks = (
                session.query(Task)
                .filter(Task.source_kind == src_kind)
                .filter(Task.source_conversation_id == conv_id)
                .filter(Task.deleted_at.is_(None))
                .order_by(Task.id)
                .all()
            )
            n_tasks_db = len(db_tasks)
            # Only tasks whose `extra.direction` is in DIRECTIONS_IMPORTANT
            # land in the Slack thread reply (FR-CR-05-163 follow-up).
            filtered_tasks = [
                t for t in db_tasks
                if _task_direction(t) in DIRECTIONS_IMPORTANT
            ]
            n_tasks_filtered = len(filtered_tasks)

            md = r.meeting_date
            default_due_date = md.date()
            default_due_time = dtime(23, 59)
            owner_ok = True
            dl_real = True
            owner_misses: list[str] = []
            dl_misses: list[str] = []
            task_lines: list[str] = []
            for t in db_tasks:
                direction = _task_direction(t) or ""
                in_filter = direction in DIRECTIONS_IMPORTANT
                owner = (t.owner_display_name or "").strip()
                # Only count owner/deadline misses for tasks that
                # actually ship to Slack (post-filter).
                if in_filter:
                    if not owner or _norm(owner) not in members_by_norm:
                        owner_ok = False
                        owner_misses.append(owner or "(none)")
                    is_default_dl = (
                        t.due_date == default_due_date
                        and (
                            t.due_time is None
                            or t.due_time == default_due_time
                        )
                    )
                    if t.due_date is None or is_default_dl:
                        dl_real = False
                        dl_misses.append((t.title or "")[:40])
                dl_str = (
                    t.due_date.strftime("%d.%m.%Y")
                    + (
                        f" {t.due_time.strftime('%H:%M')}"
                        if t.due_time else ""
                    )
                ) if t.due_date else "—"
                in_filt_mark = "✓" if in_filter else "✗"
                task_lines.append(
                    f"        · #{t.id} {(t.title or '')[:50]:<50} | "
                    f"owner={owner[:22]:<22} | due={dl_str:<16} | "
                    f"dir={direction[:14]:<14} | filt={in_filt_mark}"
                )

            report.append({
                "idx": i,
                "src": src,
                "rid": r.zoom_id if src == "zoom" else r.fireflies_id,
                "date": r.meeting_date.strftime("%m-%d %H:%M"),
                "title": (r.title or "")[:40],
                "link": _check(has_link),
                "biling": "—" if biling is None else _check(biling),
                "det_ppl": str(n_ppl),
                "det_cp": str(n_cp),
                "short": _check(has_short),
                "tsk": str(n_tasks_db),
                "tsk_filt": (
                    "—" if n_tasks_db == 0
                    else f"{n_tasks_filtered}/{n_tasks_db}"
                ),
                "tsk_own": "—" if n_tasks_filtered == 0 else _check(owner_ok),
                "tsk_dl": "—" if n_tasks_filtered == 0 else _check(dl_real),
                "_owner_miss": owner_misses,
                "_dl_miss": dl_misses,
                "_task_lines": task_lines,
                "_first_line": first[:90],
                "_last_err": (getattr(r, "last_error", None) or "")[:80],
            })

        cols = [
            ("#",        "idx",      3),
            ("date",     "date",     11),
            ("src",      "src",      4),
            ("title",    "title",    40),
            ("link",     "link",     4),
            ("biling",   "biling",   6),
            ("det_ppl",  "det_ppl",  7),
            ("det_cp",   "det_cp",   6),
            ("short",    "short",    5),
            ("tsk",      "tsk",      3),
            ("tsk_filt", "tsk_filt", 6),
            ("tsk_own",  "tsk_own",  7),
            ("tsk_dl",   "tsk_dl",   6),
        ]
        header = " | ".join(h.ljust(w) for h, _, w in cols)
        sep = "-+-".join("-" * w for _, _, w in cols)
        print()
        print(header)
        print(sep)
        for rec in report:
            print(" | ".join(str(rec[k]).ljust(w) for _, k, w in cols))

        print()
        print("LEGEND:")
        print("  link     first line is <a href=…> hyperlink to Google Doc")
        print("  biling   (zoom) transcript has ≥3 % Cyrillic AND ≥3 % Latin")
        print("  det_ppl  # canonical TeamMember names in detailed_summary")
        print("  det_cp   # canonical Counterparty names in detailed_summary")
        print("  short    short_summary present")
        print("  tsk      # alive Task rows in DB (deleted_at IS NULL)")
        print("  tsk_filt # tasks that survive `extra.direction in")
        print("           DIRECTIONS_IMPORTANT` filter / # in DB  —  only")
        print("           filtered ones land in the Slack thread reply")
        print("  tsk_own  every FILTERED task owner exists in TeamMember")
        print("  tsk_dl   every FILTERED task due_date ≠ default 18:00")
        print()

        print("DETAIL PER RECORD:")
        for rec in report:
            print(
                f"\n  [{rec['idx']}] {rec['date']} [{rec['src']}] "
                f"{rec['title']}"
            )
            if rec['_task_lines']:
                print(f"      tasks ({rec['tsk']}):")
                for line in rec['_task_lines']:
                    print(line)
            else:
                print("      tasks: (none in DB)")
            if rec['tsk_own'] == "✗":
                print(f"      ⚠ filtered task owner not in TM: {', '.join(rec['_owner_miss'])}")
            if rec['tsk_dl'] == "✗":
                print(f"      ⚠ filtered tasks with default deadline: {', '.join(rec['_dl_miss'])}")
            if rec['_last_err']:
                print(f"      last_error: {rec['_last_err']}")

        # ---- Rendered Slack-ready preview per record ---- #
        print()
        print("=" * 100)
        print("RENDERED PREVIEW (exactly what would land in Slack):")
        print("=" * 100)
        for i, (src, r) in enumerate(rows, start=1):
            src_kind = (
                TaskSourceKind.fireflies if src == "fireflies"
                else TaskSourceKind.zoom
            )
            conv_id = r.fireflies_id if src == "fireflies" else r.zoom_id
            tasks_block = _build_todo_section(
                session,
                source_kind=src_kind,
                source_conversation_id=conv_id,
            )
            # Same compose pipeline as send_one_* — body is the stored
            # short_summary (already has the <a href> hyperlink on
            # line 1 from _wrap_short_summary_with_doc_link). The
            # «TODO:» trailer is appended only when there's a tasks
            # block (FR-CR-05-189b).
            body = (r.short_summary or "").rstrip()
            parent_raw = build_parent_raw(body, tasks_block or None)
            parent_text = _compact_for_slack(_to_slack_mrkdwn(parent_raw))
            thread_text = (
                _compact_for_slack(_to_slack_mrkdwn(tasks_block))
                if tasks_block else ""
            )
            print()
            print("─" * 100)
            print(
                f"# [{i}] {r.meeting_date.strftime('%d/%m %H:%M')} "
                f"[{src}] {(r.title or '')[:60]}"
            )
            print("─" * 100)
            print("PARENT MESSAGE:")
            print(parent_text)
            if thread_text:
                print()
                print("THREAD REPLY (To-Do):")
                print(thread_text)
            else:
                print()
                print("THREAD REPLY: (none — no tasks, no TODO: trailer either)")
        print()
        print("─" * 100)

    return 0


if __name__ == "__main__":
    sys.exit(main())
