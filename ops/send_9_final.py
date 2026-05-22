"""FR-CR-05-192-final — batch-send the 9 Object/Weekly/etc. records
for Artem in one shot. Fast path (no LLM extraction):

  Step A (DB owner cleanup, no inserts):
    UPDATE Task SET owner_display_name = TeamMember.real_name
    WHERE owner_display_name LIKE '%@%' AND deleted_at IS NULL
    AND email matches an active TeamMember row.

  Step B (per record):
    - #1, #2, #9 (no DB tasks)  → send_one_* --no-tasks
    - #3-#8 (DB tasks present)  → send_one_* --use-db-tasks

  Each send is ~2 sec (no LLM call). Total wall time ≈ 30 sec.
  FR-CR-05-178 defensive Task DELETE at the end of each send
  wipes the rows so DB is clean after the run.

Usage:
    docker exec manager-bot-1 python -m ops.send_9_final
"""
from __future__ import annotations

import subprocess
import sys

from app.db import session_scope
from app.models import Task, TeamMember


# (source, conv_id, mode)
#   mode = "db"        — render TODO via _build_todo_section from DB
#                        Task rows (FR-CR-05-119/-163 filter applies),
#                        no LLM call, ~2 sec per send
#   mode = "ephemeral" — fresh LLM TASK_EXTRACTION_SYSTEM call with
#                        the FR-CR-05-192k known_employees table +
#                        email→real_name resolver. ~60-180 sec per
#                        send on long transcripts.
SEND_LIST: list[tuple[str, str, str]] = [
    ("fireflies", "01KS0551XQZSNXQ9GGS6DSEMP7", "ephemeral"),  # 19/05 13:00 Object First
    ("zoom",      "k9We5mXQRsy3aiv5rOOsHg==",   "ephemeral"),  # 20/05 09:56 Weekly TM
    ("fireflies", "01KS2V4MZ5RXVYXMY0GPK6RKF1", "db"),         # 20/05 14:05 Joe Millenia
    ("zoom",      "eQc28t2oR7u7l4ocnk6YLA==",   "db"),         # 20/05 14:18 Ирина
    ("zoom",      "8OA3y90MR3+ZCJn57oterw==",   "db"),         # 21/05 13:03 Алина Ирина
    ("fireflies", "01KS5HS60Z2V7N1ZZV1FWVEGA1", "db"),         # 21/05 15:15 Ben Verwaayen
    ("zoom",      "Y1qagtqRQ0OzJlS77rtHQw==",   "db"),         # 21/05 15:56 Ирина-аутрич
    ("fireflies", "01KS5PBJ7EQS4311TCXK5V7QMQ", "db"),         # 21/05 16:35 Erik Goodman
    ("zoom",      "jzh/h42zS0WpbI9eGImdQA==",   "ephemeral"),  # 21/05 17:30 Chris Watkins
]
CHANNEL = "D0ASY5QF6UX"


def step_a_fix_owners() -> int:
    print("\n[A] Fixing email-form owner_display_name in DB Task rows…")
    with session_scope() as s:
        tm = {
            m.email.lower().strip(): m.real_name
            for m in s.query(TeamMember)
            .filter(TeamMember.email.isnot(None))
            .filter(TeamMember.real_name.isnot(None))
            .filter(TeamMember.active.is_(True))
            .all()
        }
        n = 0
        for t in (
            s.query(Task)
            .filter(Task.owner_display_name.like("%@%"))
            .filter(Task.deleted_at.is_(None))
            .all()
        ):
            em = (t.owner_display_name or "").lower().strip()
            if em in tm:
                print(f"   #{t.id}  {t.owner_display_name}  →  {tm[em]}")
                t.owner_display_name = tm[em]
                n += 1
        s.commit()
        print(f"   Patched {n} task rows.")
        return n


def step_b_send_all() -> None:
    print(f"\n[B] Sending {len(SEND_LIST)} records to {CHANNEL}…")
    for i, (src, cid, mode) in enumerate(SEND_LIST, start=1):
        print(
            f"\n  [{i}/{len(SEND_LIST)}] {src}  {cid[:48]}  "
            f"({mode})"
        )
        module = (
            "ops.send_one_fireflies"
            if src == "fireflies"
            else "ops.send_one_zoom"
        )
        id_flag = (
            "--fireflies-id" if src == "fireflies" else "--zoom-id"
        )
        cmd = [
            "python", "-m", module,
            id_flag, cid,
            "--channel", CHANNEL,
            "--no-mark-sent",
        ]
        if mode == "db":
            cmd.append("--use-db-tasks")
        # mode == "ephemeral" → default flags trigger ephemeral extract
        # (no --use-db-tasks, no --no-tasks). FR-CR-05-192k resolver
        # converts email owners to real_name before posting.
        # Ephemeral can take 60-180 sec on long transcripts.
        timeout = 360 if mode == "ephemeral" else 60
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"    ❌ TIMEOUT after {timeout}s — skipping")
            continue
        if result.returncode != 0:
            print(f"    ❌ FAILED  rc={result.returncode}")
            print(result.stdout[-2000:] or "")
            print(result.stderr[-2000:] or "")
            continue
        # Pull the «parent ts=…» line out of stdout for the operator.
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("parent ts="):
                print(f"    ✓ {line}")
                break
        else:
            print("    ✓ posted (no ts in output)")


def main() -> int:
    step_a_fix_owners()
    step_b_send_all()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
