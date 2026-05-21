"""FR-CR-05-191 — Full pre-send verification table.

For each READY record (sorted oldest → newest), prints a comprehensive
trace of EVERYTHING that will land in Slack:

  - Title + hyperlink validation
  - Calendar attendees (count + per-attendee with method)
  - People canonical trace (which TeamMember rows the «Участники:»
    line resolves to)
  - Counterparty canonical trace (which Counterparty rows the body
    references)
  - The actual «Участники:» Slack line
  - Body preview (first ~200 chars)
  - Task-by-task breakdown: owner ✅/❌ in TeamMember, deadline
    ⚠ default / ✅ extracted
  - Pre-formatted send command for one-at-a-time posting

Usage:
    docker exec manager-bot-1 python -m ops.verify_full_19_21 \\
        --start 2026-05-19 --end 2026-05-22 \\
        --only-artem \\
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
from app.models import MeetingRecording, TeamMember, ZoomRecording
from app.models.counterparty import Counterparty


TASK_RE = re.compile(
    r"^\s*(\d+)\)\s*(.+?)\s+—\s+([^•—]+?)\s*•\s*"
    r"(\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2})",
    re.MULTILINE,
)


def _has_artem(s: str) -> bool:
    for line in (s or "").split("\n"):
        if line.startswith("Участники:"):
            ll = line.lower()
            return any(
                n in ll for n in ["артем", "артём", "artem", "sokolov"]
            )
    return False


def _ready(r) -> bool:
    return (
        not r.short_summary_sent
        and bool((r.short_summary or "").strip())
        and bool((r.google_doc_url or "").strip())
    )


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--only-artem", action="store_true")
    ap.add_argument(
        "--exclude-title-contains", action="append", default=[],
    )
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    excludes = [s.lower() for s in (args.exclude_title_contains or []) if s]

    with session_scope() as s:
        # Pre-fetch TeamMember + Counterparty for trace lookups
        members = (
            s.query(TeamMember)
            .filter(TeamMember.active.is_(True))
            .filter(TeamMember.real_name.isnot(None))
            .all()
        )
        members_by_norm = {_norm(m.real_name): m for m in members}
        counterparties = s.query(Counterparty).all()
        cp_by_norm = {
            (cp.name_normalised or _norm(cp.name)): cp for cp in counterparties
        }

        rows = []
        for r in s.query(ZoomRecording).filter(
            ZoomRecording.meeting_date >= start,
            ZoomRecording.meeting_date < end,
        ).all():
            if (r.duration_seconds or 0) < 600:
                continue
            if not _ready(r):
                continue
            if any(e in (r.title or "").lower() for e in excludes):
                continue
            if args.only_artem and not _has_artem(r.short_summary):
                continue
            rows.append(("zoom", r))
        for r in s.query(MeetingRecording).filter(
            MeetingRecording.meeting_date >= start,
            MeetingRecording.meeting_date < end,
        ).all():
            if len((r.transcript_text or "").strip()) < 1500:
                continue
            if not _ready(r):
                continue
            if any(e in (r.title or "").lower() for e in excludes):
                continue
            if args.only_artem and not _has_artem(r.short_summary):
                continue
            rows.append(("fireflies", r))

        rows.sort(key=lambda x: x[1].meeting_date)

        print(f"\nFull verify: {len(rows)} READY records sorted oldest → newest")

        for i, (src, r) in enumerate(rows, start=1):
            s_text = r.short_summary or ""
            first_line = s_text.split("\n", 1)[0] if s_text else ""
            has_hyper = "<a href=" in first_line or "<http" in first_line

            # Calendar attendees
            cal = getattr(r, "calendar_attendees", None) or []
            if not isinstance(cal, list):
                cal = []
            n_cal = len(cal)
            n_resolved = sum(
                1 for a in cal
                if isinstance(a, dict) and a.get("resolved_name")
            )
            n_decline = sum(
                1 for a in cal
                if isinstance(a, dict)
                and a.get("response_status") == "declined"
            )
            n_unknown = n_cal - n_resolved - n_decline

            # Participants line
            part_line = ""
            for line in s_text.split("\n"):
                if line.startswith("Участники:"):
                    part_line = line[len("Участники:"):].strip()
                    break

            # People trace — match every name in participants line to TeamMember
            part_names = [n.strip() for n in part_line.split(",") if n.strip()]
            people_hits = []
            people_misses = []
            for nm in part_names:
                nm_clean = nm.replace("и другие", "").strip()
                if not nm_clean:
                    continue
                if _norm(nm_clean) in members_by_norm:
                    people_hits.append(nm_clean)
                else:
                    people_misses.append(nm_clean)

            # Counterparty trace — heuristic scan body for known counterparty names
            cp_hits = set()
            body_full = s_text
            for cp_norm, cp in cp_by_norm.items():
                if not cp_norm or len(cp_norm) < 3:
                    continue
                if cp_norm in body_full.lower():
                    cp_hits.add(cp.name)

            # Body preview
            body = ""
            skip_first = True
            for line in s_text.split("\n"):
                if skip_first:
                    skip_first = False
                    continue
                if not line.strip():
                    continue
                if line.startswith("Участники:"):
                    continue
                if line.startswith("http"):
                    continue
                body = line.strip()
                break

            # TODO block
            todo = ""
            for marker in ["To-Do:", "TODO:"]:
                if marker in s_text:
                    todo = s_text.split(marker, 1)[1].strip()
                    break
            tasks = TASK_RE.findall(todo)

            # Default deadline for this meeting
            md = r.meeting_date
            default_dl = (
                f"{md.day:02d}.{md.month:02d}.{md.year} 18:00"
            )

            rid = r.zoom_id if src == "zoom" else r.fireflies_id
            print()
            print("=" * 100)
            print(
                f"# {i}. {r.meeting_date.strftime('%m-%d %H:%M')} "
                f"[{src}]  {(r.title or '')[:60]}"
            )
            print("=" * 100)

            # Title + hyperlink
            if has_hyper:
                url_match = re.search(
                    r"https?://[^\s<>|]+", first_line
                )
                url = url_match.group(0) if url_match else "?"
                print(f"  [TITLE+LINK]    ✅ hyperlinked → {url[:80]}")
            else:
                print(
                    f"  [TITLE+LINK]    ❌ no hyperlink in first line:\n"
                    f"                  {first_line[:100]}"
                )

            # Calendar
            print(
                f"  [CALENDAR]      attendees={n_cal} "
                f"resolved_via_people={n_resolved} unknown={n_unknown} "
                f"declined={n_decline}"
            )
            for a in cal[:8]:
                if not isinstance(a, dict):
                    continue
                nm = (
                    a.get("resolved_name") or a.get("display_name")
                    or a.get("email") or "?"
                )
                em = a.get("email") or "—"
                rsvp = a.get("response_status") or "—"
                src_tag = (
                    "Calendar→People"
                    if a.get("resolved_name") else "Calendar(raw)"
                )
                print(
                    f"                  · {nm[:25]:<25} "
                    f"{em[:35]:<35} via={src_tag:<16} rsvp={rsvp}"
                )
            if n_cal > 8:
                print(f"                  · ...and {n_cal - 8} more")

            # People trace
            print(
                f"  [PEOPLE TRACE]  in_TeamMember={len(people_hits)} "
                f"not_in_TeamMember={len(people_misses)}"
            )
            for nm in people_hits[:6]:
                m = members_by_norm[_norm(nm)]
                print(
                    f"                  ✅ {nm:<24} → TeamMember id={m.id} "
                    f"role={(m.role or '—')[:30]}"
                )
            for nm in people_misses[:4]:
                print(f"                  ❌ {nm} (not in TeamMember)")

            # Counterparty trace
            print(
                f"  [COUNTERPARTY]  mentioned in body: {len(cp_hits)} "
                f"matched canonical names"
            )
            for nm in list(cp_hits)[:8]:
                print(f"                  ✅ {nm}")
            if len(cp_hits) > 8:
                print(f"                  · ...and {len(cp_hits) - 8} more")

            # Participants line in Slack
            print(f"  [PARTICIPANTS]  «Участники: {part_line[:100]}»")

            # Body
            print(f"  [BODY first 200] {body[:200]}")

            # Tasks
            print(f"  [TODO]          {len(tasks)} tasks")
            for num, title, owner, dl in tasks:
                owner_clean = owner.strip()
                in_tm = _norm(owner_clean) in members_by_norm
                owner_check = "✅" if in_tm else "❌"
                dl_clean = dl.strip()
                if dl_clean == default_dl:
                    dl_check = "⚠ default(meeting_date 18:00)"
                else:
                    dl_check = "✅ extracted-from-text"
                title_short = title.strip()[:55]
                print(
                    f"                  [{num:>2}] {title_short:<57} | "
                    f"{owner_clean:<22} {owner_check} | "
                    f"{dl_clean} {dl_check}"
                )

            # Send command
            if src == "zoom":
                cmd = (
                    f'docker exec manager-bot-1 python -m ops.send_one_zoom '
                    f'--zoom-id "{rid}" --channel D0ASY5QF6UX'
                )
            else:
                cmd = (
                    f"docker exec manager-bot-1 python -m ops.send_one_fireflies "
                    f"--fireflies-id {rid} --channel D0ASY5QF6UX"
                )
            print(f"  [SEND CMD]      {cmd}")

        print()
        print("=" * 100)
        print(f"Total READY: {len(rows)} — send chronologically (oldest first)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
