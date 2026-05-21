"""FR-CR-05-192c — Full micro-check table for every meeting record.

One row per record × one column per micro-check. Each cell is one of
``✓`` / ``✗`` / ``⚠`` / a count / a short blob. Output goes to stdout
(aligned) and to a Markdown file (``/tmp/microcheck.md``) suitable for
pasting into Slack/Notion and a CSV (``/tmp/microcheck.csv``) for Excel.

Micro-checks (per record):

  1. doc_url            google_doc_url is non-empty
  2. transcript         transcript_text ≥ 1500 chars
  3. bilingual          transcript has BOTH Cyrillic AND Latin script
                        (≥3 % each)
  4. detailed           detailed_summary non-empty
  5. short              short_summary non-empty
  6. title+link         first line of short_summary contains a Slack
                        hyperlink (``<a href=…``)
  7. parts_line         «Участники:» line present
  8. cal_attendees      count of calendar_attendees (≥1 = ✓)
  9. cal_resolved       count resolved to TeamMember via people
  10. part_in_TM        every name in «Участники:» exists in TeamMember
  11. cp_in_body        count of Counterparty names mentioned in
                        detailed_summary
  12. canon_names       no «Jared/Sotiris»-style broken token in
                        short_summary (legacy mis-spellings allow-list)
  13. tasks_count       number of «N) Title — Owner • DD.MM.YYYY HH:MM»
                        rows in the TODO: block
  14. tasks_owner_ok    every owner is in TeamMember
  15. tasks_dl_real     every deadline differs from
                        ``meeting_date 18:00`` (= LLM extracted, not
                        fallback)
  16. todo_trailer_ok   short_summary ends with «TODO:» IFF tasks
                        present (FR-CR-05-189b)
  17. last_error        last_error is None
  18. short_sent        short_summary_sent flag
  19. tasks_sent        tasks_sent flag (if column exists)

Usage:
    docker exec manager-bot-1 python -m ops.full_microcheck_table \\
        --start 2026-05-19 --end 2026-05-22 \\
        --md-out /tmp/microcheck.md --csv-out /tmp/microcheck.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone

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

# Known previous-iteration bad names we manually patched — if any of
# these tokens reappear in a short_summary it means canonicalize
# regressed.
BROKEN_NAME_TOKENS = [
    "Jared Kinnan", "Sotiris Dastanopoulos",
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


def _warn_if(b: bool, ok_value: str = "✓") -> str:
    return ok_value if b else "⚠"


def _first_line(text: str) -> str:
    if not text:
        return ""
    return text.split("\n", 1)[0]


def _participants_line(text: str) -> str:
    for line in (text or "").split("\n"):
        if line.startswith("Участники:"):
            return line[len("Участники:"):].strip()
    return ""


def _todo_block(text: str) -> str:
    for marker in ["To-Do:", "TODO:"]:
        if marker in (text or ""):
            return text.split(marker, 1)[1].strip()
    return ""


def _ends_with_todo_trailer(text: str) -> bool:
    return (text or "").rstrip().endswith("TODO:") or "\nTO" in (text or "")[-12:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--md-out", default="/tmp/microcheck.md")
    ap.add_argument("--csv-out", default="/tmp/microcheck.csv")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    with session_scope() as session:
        members = session.query(TeamMember).filter(
            TeamMember.active.is_(True),
        ).all()
        members_by_norm = {
            _norm(m.real_name): m for m in members if m.real_name
        }
        counterparties = session.query(Counterparty).all()
        cps_by_norm = {
            (cp.name_normalised or _norm(cp.name)): cp
            for cp in counterparties if cp.name
        }

        rows: list[tuple[str, object]] = []
        for r in session.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).all():
            rows.append(("zoom", r))
        for r in session.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).all():
            rows.append(("fireflies", r))
        rows.sort(key=lambda x: x[1].meeting_date)

        report: list[dict] = []
        for i, (src, r) in enumerate(rows, start=1):
            short = r.short_summary or ""
            detailed = r.detailed_summary or ""
            transcript = (
                getattr(r, "transcript_text", None) or ""
            )

            # --- 1 doc_url
            has_doc = bool((r.google_doc_url or "").strip())

            # --- 2 transcript
            has_trans = len(transcript.strip()) >= 1500

            # --- 3 bilingual
            biling = _bilingual(transcript)

            # --- 4 detailed
            has_detailed = bool(detailed.strip())

            # --- 5 short
            has_short = bool(short.strip())

            # --- 6 title+link
            first = _first_line(short)
            has_link = ("<a href=" in first) or ("<http" in first)

            # --- 7 participants line
            part_line = _participants_line(short)
            has_part = bool(part_line)

            # --- 8/9 calendar attendees
            cal = r.calendar_attendees or []
            if not isinstance(cal, list):
                cal = []
            n_cal = sum(1 for a in cal if isinstance(a, dict))
            n_cal_resolved = sum(
                1 for a in cal
                if isinstance(a, dict) and a.get("resolved_name")
            )

            # --- 10 every participant in TeamMember
            part_names = [n.strip() for n in part_line.split(",") if n.strip()]
            part_in_tm = True
            part_misses: list[str] = []
            for nm in part_names:
                nm_clean = nm.replace("и другие", "").strip()
                if not nm_clean:
                    continue
                if _norm(nm_clean) not in members_by_norm:
                    part_in_tm = False
                    part_misses.append(nm_clean)

            # --- 11 counterparties mentioned in body
            n_cp = 0
            for cp_norm in cps_by_norm:
                if not cp_norm or len(cp_norm) < 3:
                    continue
                if cp_norm in detailed.lower():
                    n_cp += 1

            # --- 12 canonicalize regression check
            canon_ok = not any(t in short for t in BROKEN_NAME_TOKENS)

            # --- 13 tasks
            todo = _todo_block(short)
            tasks = TASK_RE.findall(todo)
            n_tasks = len(tasks)

            # --- 14 owners in TM
            md = r.meeting_date
            default_dl = (
                f"{md.day:02d}.{md.month:02d}.{md.year} 18:00"
            )
            owner_ok = True
            dl_real = True
            owner_misses: list[str] = []
            for _num, _title, owner, dl in tasks:
                if _norm(owner.strip()) not in members_by_norm:
                    owner_ok = False
                    owner_misses.append(owner.strip())
                if dl.strip() == default_dl:
                    dl_real = False

            # --- 16 TODO trailer rule (FR-CR-05-189b)
            ends_todo = short.rstrip().endswith("TODO:")
            trailer_ok = (
                (n_tasks > 0 and ends_todo)
                or (n_tasks == 0 and not ends_todo)
            )

            # --- 17 last_error
            last_err = getattr(r, "last_error", None) or ""
            no_err = not last_err.strip()

            # --- 18 short_sent
            short_sent = bool(getattr(r, "short_summary_sent", False))
            # --- 19 tasks_sent (col may or may not exist)
            tasks_sent = bool(getattr(r, "tasks_sent", False))

            rid = r.zoom_id if src == "zoom" else r.fireflies_id
            report.append({
                "idx": i,
                "src": src,
                "rid": rid,
                "date": r.meeting_date.strftime("%m-%d %H:%M"),
                "title": (r.title or "")[:36],
                "doc": _check(has_doc),
                "trans": _check(has_trans),
                "biling": _check(biling),
                "detail": _check(has_detailed),
                "short": _check(has_short),
                "link": _check(has_link),
                "parts": _check(has_part),
                "cal_n": str(n_cal),
                "cal_resv": str(n_cal_resolved),
                "part_tm": _check(part_in_tm) if part_names else "—",
                "cp_n": str(n_cp),
                "canon": _check(canon_ok),
                "tasks": str(n_tasks),
                "ownr_ok": _check(owner_ok) if n_tasks else "—",
                "dl_real": _check(dl_real) if n_tasks else "—",
                "trailer": _check(trailer_ok),
                "err": _check(no_err),
                "p_sent": _check(short_sent),
                "t_sent": _check(tasks_sent),
                "_misses_part": part_misses,
                "_misses_own": owner_misses,
                "_last_err": last_err,
            })

        # -------- stdout table -------- #
        cols = [
            ("#",        "idx",      3),
            ("date",     "date",     11),
            ("src",      "src",      4),
            ("title",    "title",    36),
            ("doc",      "doc",      3),
            ("trnsc",    "trans",    5),
            ("biling",   "biling",   6),
            ("detail",   "detail",   6),
            ("short",    "short",    5),
            ("link",     "link",     4),
            ("parts",    "parts",    5),
            ("cal",      "cal_n",    3),
            ("cal_r",    "cal_resv", 5),
            ("part_TM",  "part_tm",  7),
            ("cp",       "cp_n",     3),
            ("canon",    "canon",    5),
            ("tsk",      "tasks",    3),
            ("ownr",     "ownr_ok",  4),
            ("dl_re",    "dl_real",  5),
            ("trail",    "trailer",  5),
            ("err",      "err",      3),
            ("p_snt",    "p_sent",   5),
            ("t_snt",    "t_sent",   5),
        ]
        header = " | ".join(h.ljust(w) for h, _, w in cols)
        sep = "-+-".join("-" * w for _, _, w in cols)
        print()
        print(header)
        print(sep)
        for rec in report:
            row_cells = []
            for _, k, w in cols:
                v = str(rec.get(k, ""))
                row_cells.append(v[:w].ljust(w))
            print(" | ".join(row_cells))

        # -------- legend -------- #
        print()
        print("LEGEND:")
        print("  doc        google_doc_url present")
        print("  trnsc      transcript ≥ 1500 chars")
        print("  biling     transcript has ≥3% Cyrillic AND ≥3% Latin")
        print("  detail     detailed_summary present")
        print("  short      short_summary present")
        print("  link       first line of short has Slack hyperlink")
        print("  parts      «Участники:» line present")
        print("  cal        # calendar_attendees")
        print("  cal_r      # of those resolved → TeamMember")
        print("  part_TM    every Участник exists in TeamMember")
        print("  cp         # Counterparty names mentioned in detailed_summary")
        print("  canon      no «Jared/Sotiris» legacy mis-spelling")
        print("  tsk        # tasks parsed from TODO block")
        print("  ownr       every task owner exists in TeamMember")
        print("  dl_re      every task deadline ≠ default (meeting+18:00)")
        print("  trail      FR-CR-05-189b TODO: trailer correct (only if tasks)")
        print("  err        last_error empty")
        print("  p_snt      parent (short_summary) sent to Slack")
        print("  t_snt      thread tasks reply sent to Slack")
        print()

        # -------- diagnostics for failures -------- #
        print("FAILURES BY RECORD:")
        any_fail = False
        for rec in report:
            problems: list[str] = []
            for k, label in [
                ("doc", "no doc_url"),
                ("trans", "transcript<1500"),
                ("biling", "not bilingual"),
                ("detail", "no detailed"),
                ("short", "no short"),
                ("link", "no hyperlink"),
                ("parts", "no Участники:"),
                ("canon", "broken-name token"),
                ("trailer", "TODO trailer wrong"),
                ("err", f"last_error={rec['_last_err'][:60]}"),
            ]:
                if rec.get(k) == "✗":
                    problems.append(label)
            if rec.get("part_tm") == "✗":
                problems.append(
                    f"part not in TM: {', '.join(rec['_misses_part'][:3])}"
                )
            if rec.get("ownr_ok") == "✗":
                problems.append(
                    f"owner not in TM: {', '.join(rec['_misses_own'][:3])}"
                )
            if rec.get("dl_real") == "✗":
                problems.append("some deadlines = default (meeting+18:00)")
            if problems:
                any_fail = True
                print(
                    f"  [{rec['idx']:>2}] {rec['date']} {rec['title']:<36} "
                    f"→ {'; '.join(problems)}"
                )
        if not any_fail:
            print("  (none — every record passes every micro-check)")

        # -------- Markdown -------- #
        with open(args.md_out, "w", encoding="utf-8") as f:
            f.write("# Full micro-check table\n\n")
            f.write(f"Date range: {args.start} → {args.end}\n\n")
            f.write("| " + " | ".join(h for h, _, _ in cols) + " |\n")
            f.write(
                "|" + "|".join(["---"] * len(cols)) + "|\n"
            )
            for rec in report:
                f.write(
                    "| " + " | ".join(
                        str(rec.get(k, "")) for _, k, _ in cols
                    ) + " |\n"
                )
        print(f"\nMarkdown: {args.md_out}")

        # -------- CSV -------- #
        with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([h for h, _, _ in cols] + ["rid"])
            for rec in report:
                w.writerow(
                    [str(rec.get(k, "")) for _, k, _ in cols]
                    + [rec.get("rid", "")]
                )
        print(f"CSV:      {args.csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
