"""FR-CR-05-192j — Backfill missing emails / activate humanoid
TeamMember rows so calendar→people resolution stops dropping
internal teammates as bare email addresses.

Diagnosed via `ops/trace_record_full.py` on 2026-05-22:
  - `1@thehumanoid.ai` (Артем Соколов): active=False → flip to True
  - `dmitry.sedov@thehumanoid.ai` (Дмитрий Седов): active=False → True
  - `jarc@thehumanoid.ai` (Jarad Cannon, id=55): email NULL → set
  - `jochen@thehumanoid.ai` (Jochen Ruda): no TM row → INSERT
  - `sots@thehumanoid.ai` (Sotirios Stasinopoulos, CPO): no TM row → INSERT

Lookup strategy per entry:
  1. Try by email (case-insensitive).
  2. If not found, try by real_name (case-insensitive).
  3. If not found, INSERT a fresh row.

In all branches, after the lookup we set active=True, fill in
email + real_name + role, and stamp `last_synced_at`. Pure
idempotent — re-running is a no-op.

Usage:
    docker exec manager-bot-1 python -m ops.fix_team_member_emails [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import func

from app.db import session_scope
from app.models import TeamMember


# (real_name, email, role) — role can be "" if operator hasn't pinned
# it yet (Team-sheet sync will fill it later).
ENTRIES: list[tuple[str, str, str]] = [
    ("Артем Соколов",          "1@thehumanoid.ai",           ""),
    ("Дмитрий Седов",          "dmitry.sedov@thehumanoid.ai", ""),
    ("Jarad Cannon",           "jarc@thehumanoid.ai",         "CTO"),
    ("Jochen Ruda",            "jochen@thehumanoid.ai",       ""),
    ("Sotirios Stasinopoulos", "sots@thehumanoid.ai",         "Chief Product Officer"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    changes: list[str] = []
    with session_scope() as session:
        for real_name, email, role in ENTRIES:
            email_low = email.lower().strip()
            # 1) by email
            m = (
                session.query(TeamMember)
                .filter(func.lower(TeamMember.email) == email_low)
                .first()
            )
            mode = "by-email"
            if m is None:
                # 2) by exact real_name
                m = (
                    session.query(TeamMember)
                    .filter(func.lower(TeamMember.real_name) == real_name.lower())
                    .first()
                )
                mode = "by-name" if m else "insert"
            if m is None:
                m = TeamMember(
                    real_name=real_name,
                    email=email,
                    role=role or None,
                    active=True,
                    notes=(
                        "Seeded 2026-05-22 via "
                        "ops/fix_team_member_emails.py — calendar→people "
                        "resolution was dropping this teammate as bare "
                        "email in calendar_attendees."
                    ),
                    last_synced_at=now,
                )
                if not args.dry_run:
                    session.add(m)
                    session.flush()
                changes.append(
                    f"  ✓ INSERT  real_name='{real_name}'  "
                    f"email='{email}'  role='{role or '—'}'  "
                    f"(id={getattr(m, 'id', '—')})"
                )
                continue
            # Existing row → patch in place
            before_active = m.active
            before_email = m.email
            before_role = m.role
            m.active = True
            if not m.email:
                m.email = email
            if role and not (m.role or "").strip():
                m.role = role
            m.last_synced_at = now
            if not args.dry_run:
                session.flush()
            diffs: list[str] = []
            if before_active != m.active:
                diffs.append(f"active {before_active}→{m.active}")
            if before_email != m.email:
                diffs.append(f"email {before_email!r}→{m.email!r}")
            if before_role != m.role:
                diffs.append(f"role {before_role!r}→{m.role!r}")
            if not diffs:
                changes.append(
                    f"  · no-op   real_name='{m.real_name}' id={m.id} "
                    f"({mode}, already clean)"
                )
            else:
                changes.append(
                    f"  ✓ UPDATE id={m.id} real_name='{m.real_name}' "
                    f"({mode}): {', '.join(diffs)}"
                )
        if not args.dry_run:
            session.commit()
    print()
    for c in changes:
        print(c)
    print()
    print(f"Total entries: {len(ENTRIES)}  "
          f"{'(dry-run, no commit)' if args.dry_run else 'COMMITTED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
