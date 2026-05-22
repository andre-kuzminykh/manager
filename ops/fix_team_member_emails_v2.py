"""FR-CR-05-192m — Backfill @skl.vc calendar emails into TeamMember.

Production calendar invites for the humanoid team use the @skl.vc
domain (`jrud@skl.vc`, `sots@skl.vc`, `jarc@skl.vc`, …) — the
@thehumanoid.ai-pattern emails we'd seeded earlier never matched
because that's the public-facing brand, not the calendar one.
Symptom (#2 Weekly TM, 2026-05-22): 8/11 calendar attendees fell
through to «email as resolved_name», the SHORT_SUMMARY_SYSTEM LLM
saw raw emails in the participants block, hallucinated «Ирина
Шипилова» as one of the 6 attendees, mis-spelled «Jared» from
«jarc@skl.vc», and dropped 5 real attendees entirely.

Fix (UPDATE-only — no inserts where row exists, idempotent):

  Existing TM rows — replace email with the @skl.vc form so calendar
  matcher can resolve:
    - Sotirios Stasinopoulos: sots@thehumanoid.ai → sots@skl.vc
    - Jochen Ruda (real_name «Jochen Rudat» per calendar — also
      patch real_name to match the canonical spelling): jochen@…
      → jrud@skl.vc
    - Jarad Cannon: jarc@thehumanoid.ai → jarc@skl.vc
    - Alina Kolpakova: kaa@thehumanoid.ai → kaa@skl.vc

  Missing humanoid TM rows — INSERT with @skl.vc primary email:
    - Daniella Shabarina (dans@skl.vc, role tba)
    - Thomas Shepherd (thsh@skl.vc, role tba)
    - Ivan Zaitsev (ivan.zaitsev@thehumanoid.ai — kept on
      @thehumanoid.ai per the trace which shows that's his calendar
      address; not on @skl.vc)

Idempotent. Run via `--dry-run` first to preview.

Usage:
    docker exec manager-bot-1 python -m ops.fix_team_member_emails_v2 \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import func

from app.db import session_scope
from app.models import TeamMember


# (real_name, new_email, role_if_missing) — used both for UPDATEs of
# existing rows (matched by real_name OR by `old_email`) and INSERTs.
UPDATES: list[tuple[str, str, str]] = [
    ("Sotirios Stasinopoulos", "sots@skl.vc", "Chief Product Officer"),
    ("Jochen Rudat",           "jrud@skl.vc", "CRO / CGO"),
    ("Jarad Cannon",           "jarc@skl.vc", "CTO"),
    ("Alina Kolpakova",        "kaa@skl.vc",  ""),
]
INSERTS: list[tuple[str, str, str]] = [
    ("Daniella Shabarina", "dans@skl.vc", ""),
    ("Thomas Shepherd",    "thsh@skl.vc", ""),
    ("Ivan Zaitsev",       "ivan.zaitsev@thehumanoid.ai", ""),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    changes: list[str] = []
    with session_scope() as s:
        # --- UPDATEs ---- #
        for real_name, new_email, fallback_role in UPDATES:
            # Look up by real_name (case-insensitive). If duplicates,
            # take the first.
            m = (
                s.query(TeamMember)
                .filter(func.lower(TeamMember.real_name) == real_name.lower())
                .first()
            )
            if m is None:
                # No row found — treat as INSERT for safety.
                m = TeamMember(
                    real_name=real_name,
                    email=new_email,
                    role=fallback_role or None,
                    active=True,
                    notes="Seeded via fix_team_member_emails_v2.",
                    last_synced_at=now,
                )
                if not args.dry_run:
                    s.add(m)
                    s.flush()
                changes.append(
                    f"  ✓ INSERT (fallback) real_name='{real_name}' "
                    f"email='{new_email}' role='{fallback_role or '—'}'"
                )
                continue
            before = (m.email, m.role, m.active, m.real_name)
            m.email = new_email
            m.active = True
            if real_name != m.real_name:
                # Calendar uses «Jochen Rudat» (with t) — patch the
                # canonical real_name so the LLM sees the right form
                # in known_employees.
                m.real_name = real_name
            if fallback_role and not (m.role or "").strip():
                m.role = fallback_role
            m.last_synced_at = now
            if not args.dry_run:
                s.flush()
            diffs: list[str] = []
            if before[0] != m.email:
                diffs.append(f"email {before[0]!r}→{m.email!r}")
            if before[1] != m.role:
                diffs.append(f"role {before[1]!r}→{m.role!r}")
            if before[2] != m.active:
                diffs.append(f"active {before[2]}→{m.active}")
            if before[3] != m.real_name:
                diffs.append(f"real_name {before[3]!r}→{m.real_name!r}")
            if diffs:
                changes.append(
                    f"  ✓ UPDATE id={m.id} '{m.real_name}': "
                    + ", ".join(diffs)
                )
            else:
                changes.append(
                    f"  · no-op  id={m.id} '{m.real_name}' (already clean)"
                )

        # --- INSERTs ---- #
        for real_name, email, role in INSERTS:
            existing_by_email = (
                s.query(TeamMember)
                .filter(func.lower(TeamMember.email) == email.lower())
                .first()
            )
            existing_by_name = (
                s.query(TeamMember)
                .filter(func.lower(TeamMember.real_name) == real_name.lower())
                .first()
            )
            target = existing_by_email or existing_by_name
            if target is None:
                m = TeamMember(
                    real_name=real_name,
                    email=email,
                    role=role or None,
                    active=True,
                    notes="Seeded via fix_team_member_emails_v2.",
                    last_synced_at=now,
                )
                if not args.dry_run:
                    s.add(m)
                    s.flush()
                changes.append(
                    f"  ✓ INSERT real_name='{real_name}' "
                    f"email='{email}' role='{role or '—'}'"
                )
            else:
                # Patch missing fields on existing row.
                before = (target.email, target.role, target.active)
                if not target.email:
                    target.email = email
                if role and not (target.role or "").strip():
                    target.role = role
                target.active = True
                if not args.dry_run:
                    s.flush()
                diffs: list[str] = []
                if before[0] != target.email:
                    diffs.append(f"email {before[0]!r}→{target.email!r}")
                if before[1] != target.role:
                    diffs.append(f"role {before[1]!r}→{target.role!r}")
                if before[2] != target.active:
                    diffs.append(f"active {before[2]}→{target.active}")
                if diffs:
                    changes.append(
                        f"  ✓ UPDATE id={target.id} '{target.real_name}': "
                        + ", ".join(diffs)
                    )
                else:
                    changes.append(
                        f"  · no-op  id={target.id} '{target.real_name}' "
                        "(already clean)"
                    )

        if not args.dry_run:
            s.commit()

    print()
    for c in changes:
        print(c)
    print()
    print(
        f"Total: {len(UPDATES)} UPDATEs + {len(INSERTS)} INSERTs  "
        f"{'(dry-run, no commit)' if args.dry_run else 'COMMITTED'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
