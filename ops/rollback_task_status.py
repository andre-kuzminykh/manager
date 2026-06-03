"""FR-ST-RB — roll back a task field change from the unified event log.

Restores the pre-change value of a logged event via the normal write path
and records a new ``source=rollback`` event (append-only preserved). This is
the CLI face of «откат из мастер-таблицы»; the Google Sheet `status_log` tab
is the GUI face (same event_id).

Usage:
    python -m ops.rollback_task_status --list --task-id 8201       # show recent events
    python -m ops.rollback_task_status --event-id 123              # roll back that event
    python -m ops.rollback_task_status --task-id 8201 --last       # roll back task's last non-rollback event
"""
from __future__ import annotations

import argparse
import sys

from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.models.task_status_event import TaskStatusEvent
from app.services.status_events import rollback_event

log = get_logger(__name__)


def _sync(task_id: int) -> None:
    """Propagate the restored value to Sheets/Tasks/cards (best-effort)."""
    try:
        from app.sync.task_sync import sync_task

        sync_task(task_id)
    except Exception as e:  # noqa: BLE001
        print(f"  sync warning (rollback already persisted): {e}")


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--event-id", type=int)
    ap.add_argument("--task-id", type=int)
    ap.add_argument("--last", action="store_true", help="roll back task's last non-rollback event")
    ap.add_argument("--list", action="store_true", help="list recent events (use with --task-id)")
    ap.add_argument("--actor", default="ops:rollback")
    a = ap.parse_args()

    with session_scope() as s:
        if a.list:
            q = s.query(TaskStatusEvent)
            if a.task_id:
                q = q.filter(TaskStatusEvent.task_id == a.task_id)
            rows = q.order_by(TaskStatusEvent.id.desc()).limit(30).all()
            if not rows:
                print("no events")
                return 0
            print(f"{'id':>6}  {'task':>6}  {'source':<9} {'field':<10} {'from':<18} -> to")
            for e in rows:
                print(f"{e.id:>6}  {e.task_id:>6}  {e.source:<9} {e.field:<10} "
                      f"{str(e.from_value)[:18]:<18} -> {str(e.to_value)[:24]}")
            return 0

        eid = a.event_id
        if eid is None and a.task_id and a.last:
            ev = (
                s.query(TaskStatusEvent)
                .filter(
                    TaskStatusEvent.task_id == a.task_id,
                    TaskStatusEvent.source != "rollback",
                )
                .order_by(TaskStatusEvent.id.desc())
                .first()
            )
            eid = ev.id if ev else None
        if eid is None:
            print("need --event-id, or --task-id --last (see --list)")
            return 2

        res = rollback_event(s, event_id=eid, actor=a.actor, sync=_sync)
        print(res)
        return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
