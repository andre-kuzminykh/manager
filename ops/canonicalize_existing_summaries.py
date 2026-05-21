"""FR-CR-05-191 retro — canonicalize names in existing 19-21 May
short_summary + detailed_summary using TeamMember + Counterparty
directories.

Reads each Zoom + Fireflies recording in date range, runs the
new ``canonicalize_summary_text`` service over both summaries in
PARALLEL via ThreadPoolExecutor (LLM calls dominate runtime),
writes back if anything changed.

Usage:
    docker exec manager-bot-1 python -m ops.canonicalize_existing_summaries \\
        --start 2026-05-19 --end 2026-05-22 [--dry-run] [--workers 6]
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.models import MeetingRecording, ZoomRecording
from app.services.summary_canonicalize import canonicalize_summary_text


@dataclass
class WorkItem:
    idx: int
    src: str  # "zoom" | "fireflies"
    rid: str  # zoom_id or fireflies_id
    title: str
    field: str  # "detailed" | "short"
    text: str


@dataclass
class WorkResult:
    item: WorkItem
    new_text: str | None
    applied: dict[str, str]


def _process_one(item: WorkItem, llm_backend, model: str) -> WorkResult:
    """Worker: open own session, run canonicalize, return result."""
    with session_scope() as session:
        new_text, applied = canonicalize_summary_text(
            item.text,
            session=session, llm_backend=llm_backend, model=model,
            trace_source=f"{item.src}_{item.field}",
            trace_recording_id=item.rid,
        )
    return WorkResult(item=item, new_text=new_text, applied=applied)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--skip-detailed", action="store_true",
        help="Only canonicalize short_summary (faster).",
    )
    ap.add_argument("--skip-zoom", action="store_true")
    ap.add_argument("--skip-fireflies", action="store_true")
    ap.add_argument(
        "--workers", type=int, default=6,
        help="Parallel LLM workers (default 6).",
    )
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    s = get_settings()
    from openai import OpenAI
    oc = OpenAI(api_key=s.openai_api_key)
    llm = OpenAIBackend(client=oc, model=s.fireflies_tasks_model)
    model = s.fireflies_tasks_model

    # Step 1: build worklist (each (record, field) → one WorkItem)
    worklist: list[WorkItem] = []
    with session_scope() as session:
        if not args.skip_zoom:
            for i, r in enumerate(session.query(ZoomRecording).filter(
                ZoomRecording.meeting_date >= start,
                ZoomRecording.meeting_date < end,
            ).order_by(ZoomRecording.meeting_date).all(), start=1):
                if not (r.short_summary or "").strip():
                    continue
                title = (r.title or "")[:55]
                if not args.skip_detailed and (r.detailed_summary or "").strip():
                    worklist.append(WorkItem(
                        idx=i, src="zoom", rid=r.zoom_id, title=title,
                        field="detailed", text=r.detailed_summary,
                    ))
                worklist.append(WorkItem(
                    idx=i, src="zoom", rid=r.zoom_id, title=title,
                    field="short", text=r.short_summary,
                ))
        if not args.skip_fireflies:
            for i, r in enumerate(session.query(MeetingRecording).filter(
                MeetingRecording.meeting_date >= start,
                MeetingRecording.meeting_date < end,
            ).order_by(MeetingRecording.meeting_date).all(), start=1):
                if not (r.short_summary or "").strip():
                    continue
                title = (r.title or "")[:55]
                if not args.skip_detailed and (r.detailed_summary or "").strip():
                    worklist.append(WorkItem(
                        idx=i, src="fireflies", rid=r.fireflies_id, title=title,
                        field="detailed", text=r.detailed_summary,
                    ))
                worklist.append(WorkItem(
                    idx=i, src="fireflies", rid=r.fireflies_id, title=title,
                    field="short", text=r.short_summary,
                ))
    print(f"Worklist: {len(worklist)} (record, field) pairs. "
          f"Parallelism: {args.workers}.")

    # Step 2: parallel LLM canonicalize
    results: list[WorkResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_process_one, item, llm, model): item
                for item in worklist}
        done_count = 0
        for fut in as_completed(futs):
            done_count += 1
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                item = futs[fut]
                print(f"  ERR [{item.src} {item.field} {item.rid}]: {e}")
                continue
            results.append(res)
            mark = "✏" if res.applied else "—"
            print(f"  [{done_count}/{len(worklist)}] {mark} "
                  f"{res.item.src} {res.item.field} | {res.item.title}")
            if res.applied:
                for m, c in res.applied.items():
                    print(f"        '{m}' → '{c}'")

    # Step 3: apply DB writes if not dry-run
    if not args.dry_run:
        with session_scope() as session:
            for res in results:
                if not res.applied:
                    continue
                if res.item.src == "zoom":
                    r = session.query(ZoomRecording).filter(
                        ZoomRecording.zoom_id == res.item.rid,
                    ).first()
                else:
                    r = session.query(MeetingRecording).filter(
                        MeetingRecording.fireflies_id == res.item.rid,
                    ).first()
                if r is None:
                    continue
                if res.item.field == "detailed":
                    r.detailed_summary = res.new_text
                else:
                    r.short_summary = res.new_text
            session.commit()

    # Summary
    total = sum(1 for r in results if r.applied)
    total_rewrites = sum(len(r.applied) for r in results)
    print()
    print(f"Items with rewrites: {total}/{len(results)}")
    print(f"Total rewrites applied: {total_rewrites}")
    if args.dry_run:
        print("--dry-run — no commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
