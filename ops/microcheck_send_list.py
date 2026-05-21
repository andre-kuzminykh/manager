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

from app.db import session_scope
from app.models import MeetingRecording, TeamMember, ZoomRecording
from app.models.counterparty import Counterparty


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

            todo = _todo_block(short)
            tasks = TASK_RE.findall(todo)
            n_tasks = len(tasks)

            md = r.meeting_date
            default_dl = f"{md.day:02d}.{md.month:02d}.{md.year} 18:00"
            owner_ok = True
            dl_real = True
            owner_misses: list[str] = []
            dl_misses: list[str] = []
            for _num, t_title, owner, dl in tasks:
                if _norm(owner.strip()) not in members_by_norm:
                    owner_ok = False
                    owner_misses.append(owner.strip())
                if dl.strip() == default_dl:
                    dl_real = False
                    dl_misses.append(t_title.strip()[:40])

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
                "tsk": str(n_tasks),
                "tsk_own": "—" if n_tasks == 0 else _check(owner_ok),
                "tsk_dl": "—" if n_tasks == 0 else _check(dl_real),
                "_owner_miss": owner_misses,
                "_dl_miss": dl_misses,
                "_first_line": first[:90],
                "_last_err": (getattr(r, "last_error", None) or "")[:80],
            })

        cols = [
            ("#",        "idx",     3),
            ("date",     "date",    11),
            ("src",      "src",     4),
            ("title",    "title",   40),
            ("link",     "link",    4),
            ("biling",   "biling",  6),
            ("det_ppl",  "det_ppl", 7),
            ("det_cp",   "det_cp",  6),
            ("short",    "short",   5),
            ("tsk",      "tsk",     3),
            ("tsk_own",  "tsk_own", 7),
            ("tsk_dl",   "tsk_dl",  6),
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
        print("  tsk      # tasks parsed from TODO block")
        print("  tsk_own  every task owner exists in TeamMember")
        print("  tsk_dl   every task deadline ≠ default meeting_date 18:00")
        print()

        print("DETAIL PER RECORD:")
        for rec in report:
            print(
                f"\n  [{rec['idx']}] {rec['date']} [{rec['src']}] "
                f"{rec['title']}"
            )
            print(f"      first line: {rec['_first_line']}")
            if rec['tsk_own'] == "✗":
                print(f"      owner not in TM: {', '.join(rec['_owner_miss'])}")
            if rec['tsk_dl'] == "✗":
                print(f"      tasks with default deadline: {', '.join(rec['_dl_miss'])}")
            if rec['_last_err']:
                print(f"      last_error: {rec['_last_err']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
