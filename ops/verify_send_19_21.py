"""Verify what will be sent to Slack by `send_batch_chronological`.

For each READY record (after excludes), prints a structured report:
  - Title (and hyperlink check in short_summary first line)
  - Source (zoom | fireflies)
  - Calendar attendees trace (count, resolved, unmatched, declined)
  - Final «Участники:» line (from short_summary)
  - First N chars of body (sanity check — no «Сути:»)
  - To-Do block (what WOULD go in thread if `--no-tasks` was off)
  - Send mode summary (--no-tasks → parent only, no thread)

Read-only. No DB changes, no Slack calls.

Usage:
    docker exec manager-bot-1 python -m ops.verify_send_19_21 \\
        --start 2026-05-19 --end 2026-05-22 \\
        --exclude-title-contains "Fundraising daily" \\
        --exclude-title-contains "Zia <> Artem" \\
        --exclude-title-contains "Genia Xasis"
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

from app.db import session_scope
from app.fireflies.pipeline import _strip_llm_todo_block
from app.models import MeetingRecording, ZoomRecording


def _ready(row) -> bool:
    return (
        not row.short_summary_sent
        and bool((row.short_summary or "").strip())
        and bool((row.google_doc_url or "").strip())
    )


def _split_short_summary(text: str) -> tuple[str, str]:
    body = _strip_llm_todo_block(text).rstrip()
    if body == text.rstrip():
        return body, ""
    suffix = text[len(body):].lstrip("\n").rstrip()
    return body, suffix


_PARTICIPANTS_LINE = re.compile(r"(?im)^Участники:\s*(.+)$")
_HREF_TAG = re.compile(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', re.I)


def _verify_record(idx: int, dt: datetime, src: str, row) -> None:
    title = row.title or ""
    short = (row.short_summary or "").strip()
    body, todo = _split_short_summary(short)

    print()
    print("=" * 100)
    print(f"# {idx}.  {dt.strftime('%Y-%m-%d %H:%M')} UTC   src={src}")
    print(f"   title: {title}")
    print("=" * 100)

    # 1) Hyperlink check
    first_line = short.split("\n", 1)[0]
    href = _HREF_TAG.search(first_line)
    if href:
        url, text = href.group(1), href.group(2)
        # Verify it's a Google Doc URL
        if "docs.google.com" in url:
            print(f"\n  [1] HYPERLINK   ✅  <{url}|{text}>")
        else:
            print(f"\n  [1] HYPERLINK   ⚠  not a Google Doc: {url}")
    elif row.google_doc_url:
        print(f"\n  [1] HYPERLINK   ⚠  не в формате <a href>, "
              f"но doc URL есть: {row.google_doc_url[:70]}")
    else:
        print(f"\n  [1] HYPERLINK   ❌  нет hyperlink в первой строке")

    # 2) Calendar attendees trace
    cal_attendees = getattr(row, "calendar_attendees", None) or []
    if not isinstance(cal_attendees, list):
        cal_attendees = []
    n_cal = len(cal_attendees)
    n_resolved = sum(
        1 for a in cal_attendees
        if isinstance(a, dict) and a.get("resolved_name")
    )
    n_decline = sum(
        1 for a in cal_attendees
        if isinstance(a, dict) and a.get("response_status") == "declined"
    )
    n_unknown = n_cal - n_resolved - n_decline
    print(
        f"\n  [2] CALENDAR    "
        f"attendees={n_cal}  resolved={n_resolved}  "
        f"unknown={n_unknown}  declined={n_decline}"
    )
    for a in cal_attendees:
        if not isinstance(a, dict):
            continue
        nm = a.get("resolved_name") or a.get("display_name") or ""
        em = a.get("email") or ""
        meth = a.get("resolution_method") or "—"
        rs = a.get("response_status") or "—"
        print(f"          • {nm or '?':<28} {em:<35} "
              f"method={meth:<14} rsvp={rs}")

    if n_cal == 0:
        print(f"          ⚠  Calendar attendees пусты — pипотечно "
              f"FR-CR-05-181 не отработало")
    else:
        print(f"          ✅  FR-CR-05-181/183: participants from Calendar")

    # 3) Final «Участники:» line (what user will see in Slack)
    m = _PARTICIPANTS_LINE.search(short)
    if m:
        names = m.group(1).strip()
        print(f"\n  [3] PARTICIPANTS (Slack line)")
        print(f"          «Участники: {names}»")
    else:
        print(f"\n  [3] PARTICIPANTS   ❌  нет строки «Участники:» в "
              f"short_summary")

    # 4) Body preview (check no «Сути:»)
    body_lines = body.split("\n")
    body_start = ""
    for line in body_lines:
        if not line.strip():
            continue
        if line.startswith("<a href=") or line.startswith("http"):
            continue
        if line.lower().startswith("участники:"):
            continue
        body_start = line
        break
    if body_start.lower().startswith("суть:"):
        print(f"\n  [4] BODY        ❌  начинается со «Сути:»")
    else:
        print(f"\n  [4] BODY        ✅  без «Сути:» — FR-CR-05-182")
    print(f"          first ~150 chars: {body_start[:150]}…")

    # 5) TODO block (would go to thread if --no-tasks=False)
    print(f"\n  [5] TODO BLOCK  {len(todo)} chars in row.short_summary")
    if todo:
        # First 5 lines
        todo_lines = [
            l for l in todo.split("\n") if l.strip()
        ][:5]
        for l in todo_lines:
            print(f"          {l[:150]}")
        if len([l for l in todo.split('\n') if l.strip()]) > 5:
            print("          … (truncated)")
    else:
        print(f"          (empty)")

    # 6) Send-mode summary
    print(f"\n  [6] SEND MODE   --no-tasks → parent ONLY, no thread")
    print(f"          parent will be: title hyperlink + Участники + body")
    print(f"          NO «TODO:» trailer, NO thread reply")

    # Final verdict
    has_hyper = bool(href)
    has_cal = n_cal > 0
    has_part_line = bool(m)
    no_suti = not body_start.lower().startswith("суть:")
    all_ok = has_hyper and has_cal and has_part_line and no_suti

    print(f"\n  VERDICT: "
          f"{'✅ READY' if all_ok else '⚠  CHECK ISSUES ABOVE'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument(
        "--exclude-title-contains", action="append", default=[],
    )
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    excludes = [s.lower() for s in (args.exclude_title_contains or []) if s]

    candidates = []
    with session_scope() as s:
        for r in s.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).all():
            if not _ready(r):
                continue
            title = r.title or ""
            if any(e in title.lower() for e in excludes):
                continue
            candidates.append((r.meeting_date, "zoom", r))
        for r in s.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).all():
            if not _ready(r):
                continue
            title = r.title or ""
            if any(e in title.lower() for e in excludes):
                continue
            candidates.append((r.meeting_date, "fireflies", r))

        candidates.sort(key=lambda x: x[0])

        print(f"\nVerify plan: {len(candidates)} READY records "
              f"(после excludes)")
        for i, (dt, src, r) in enumerate(candidates, start=1):
            _verify_record(i, dt, src, r)

    print("\n" + "=" * 100)
    print(f"Total: {len(candidates)} records verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
