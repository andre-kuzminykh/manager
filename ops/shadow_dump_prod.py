"""FR-CR-05-241 — Phase A of the OFFLINE shadow: dump recent prod meetings
(transcript + v1 counterparty resolution) to JSON, for v2 comparison in
the pgvector sidecar. READ-ONLY on prod; writes only a JSON file.

Run in the prod container (slack-task-db):
    docker exec manager-zoom-ff-1 python -m ops.shadow_dump_prod \\
        --limit 20 --min-chars 800 --out /tmp/shadow_prod.json
    docker cp manager-zoom-ff-1:/tmp/shadow_prod.json /tmp/shadow_prod.json
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app.db import session_scope
from app.models import ZoomRecording
from app.models.counterparty import Counterparty, CounterpartyMention


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-chars", type=int, default=800,
                    help="skip thin transcripts (no real content)")
    ap.add_argument("--out", default="/tmp/shadow_prod.json")
    args = ap.parse_args()

    with session_scope() as s:
        recs = (
            s.query(ZoomRecording)
            .filter(ZoomRecording.transcribed.is_(True))
            .order_by(ZoomRecording.meeting_date.desc())
            .limit(args.limit * 3)  # over-fetch, filter thin below
            .all()
        )
        meetings = []
        for r in recs:
            if len((r.transcript_text or "")) < args.min_chars:
                continue
            ms = s.execute(
                select(CounterpartyMention)
                .where(CounterpartyMention.source_id == r.zoom_id)
            ).scalars().all()
            v1 = []
            for m in ms:
                cp = s.get(Counterparty, m.counterparty_id)
                if cp:
                    v1.append(cp.name)
            meetings.append({
                "zoom_id": r.zoom_id,
                "title": r.title or "",
                "meeting_date": str(r.meeting_date),
                "transcript_chars": len(r.transcript_text or ""),
                "transcript": r.transcript_text or "",
                "v1_counterparties": v1,
            })
            if len(meetings) >= args.limit:
                break
        s.rollback()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"meetings": meetings}, fh, ensure_ascii=False)
    print(f"[ok] выгружено встреч: {len(meetings)} → {args.out}")
    for mt in meetings:
        print(f"  {mt['meeting_date'][:16]} | {mt['title'][:38]:<38} | "
              f"chars={mt['transcript_chars']:>6} | v1={len(mt['v1_counterparties'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
