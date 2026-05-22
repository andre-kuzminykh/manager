"""FR-CR-05-192r — Operator-pinned: don't assign tasks to Артем
(CEO), delegate to Ирина Шипилова (assistant). Pin the rule via
TM notes so the LLM also sees it; backfill via UPDATE on existing
DB Task rows.

Steps (idempotent):

  1. INSERT or UPDATE Ирина Шипилова in TeamMember
     (real_name='Ирина Шипилова', notes='Handles CEO follow-ups /
     scheduling / contact relay — operator-pinned delegate for
     Артем Соколов').

  2. UPDATE Артем's TM notes to carry the delegate marker:
     «DELEGATE_TASKS_TO: Ирина Шипилова. Operator-pinned 2026-05-22:
     CEO does not own actionable items; assistant Ирина Шипилова
     receives them.»

  3. UPDATE every alive Task row where owner_display_name='Артем
     Соколов' → 'Ирина Шипилова'. Operator's earlier contract «не
     создавать задачи в БД» applies to INSERTs; this is an UPDATE on
     existing data the operator wants re-attributed before the next
     Slack send.

Usage:
    docker exec manager-bot-1 python -m ops.fix_delegate_artem_to_irina \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import func

from app.db import session_scope
from app.models import Task, TeamMember


ARTEM_REAL_NAME = "Артем Соколов"
IRINA_REAL_NAME = "Ирина Шипилова"
ARTEM_NOTES_MARKER = (
    "DELEGATE_TASKS_TO: Ирина Шипилова. "
    "Operator-pinned 2026-05-22: CEO does not own actionable items; "
    "assistant Ирина Шипилова receives them."
)
IRINA_NOTES = (
    "Handles CEO follow-ups, scheduling, contact relay. "
    "Operator-pinned 2026-05-22: receives all tasks the LLM would "
    "otherwise assign to Артем Соколов."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # 1) INSERT or UPDATE Ирина
        irina = (
            s.query(TeamMember)
            .filter(func.lower(TeamMember.real_name) == IRINA_REAL_NAME.lower())
            .first()
        )
        if irina is None:
            irina = TeamMember(
                real_name=IRINA_REAL_NAME,
                active=True,
                notes=IRINA_NOTES,
                last_synced_at=now,
            )
            if not args.dry_run:
                s.add(irina)
                s.flush()
            print(
                f"  ✓ INSERT '{IRINA_REAL_NAME}' (id={irina.id}) "
                f"notes='{IRINA_NOTES[:60]}…'"
            )
        else:
            irina.active = True
            if not (irina.notes or "").strip():
                irina.notes = IRINA_NOTES
            irina.last_synced_at = now
            if not args.dry_run:
                s.flush()
            print(
                f"  · UPDATE id={irina.id} '{irina.real_name}' "
                f"(active=True, notes preserved)"
            )

        # 2) UPDATE Артем's notes with delegate marker
        artem = (
            s.query(TeamMember)
            .filter(func.lower(TeamMember.real_name) == ARTEM_REAL_NAME.lower())
            .first()
        )
        if artem is None:
            print(f"  ⚠ Артем not in TM — skipping notes update")
        else:
            existing_notes = (artem.notes or "").strip()
            if "DELEGATE_TASKS_TO" in existing_notes:
                print(
                    f"  · Артем (id={artem.id}) — DELEGATE_TASKS_TO "
                    "already in notes, skip"
                )
            else:
                merged = (
                    existing_notes + "\n\n" + ARTEM_NOTES_MARKER
                    if existing_notes else ARTEM_NOTES_MARKER
                )
                artem.notes = merged
                artem.last_synced_at = now
                if not args.dry_run:
                    s.flush()
                print(
                    f"  ✓ UPDATE id={artem.id} '{artem.real_name}' "
                    f"+ DELEGATE_TASKS_TO marker ({len(merged)} chars)"
                )

        # 3) UPDATE alive Task rows: Артем → Ирина
        rows = (
            s.query(Task)
            .filter(Task.owner_display_name == ARTEM_REAL_NAME)
            .filter(Task.deleted_at.is_(None))
            .all()
        )
        n_swapped = 0
        for t in rows:
            t.owner_display_name = IRINA_REAL_NAME
            n_swapped += 1
        if not args.dry_run:
            s.flush()
            s.commit()
        print(
            f"  ✓ UPDATE {n_swapped} Task rows: owner_display_name "
            f"'{ARTEM_REAL_NAME}' → '{IRINA_REAL_NAME}'"
        )

    print()
    print(
        f"{'(dry-run, no commit)' if args.dry_run else 'COMMITTED'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
