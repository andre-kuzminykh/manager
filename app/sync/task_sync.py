"""Central task → external-systems syncer.

`TaskSyncer.sync(task_id)` pushes the current state of a task to every
configured external system (Google Sheets, Google Tasks). Idempotent
per task: the Sheets row is appended on first sync and updated in place
on every subsequent call.

Wired in once at app startup (`app/main.py`) and called from every
handler that mutates a task (start, mark done, edit, cancel, delete).
A module-level holder lets the handlers reach the syncer without
threading it through every signature.
"""
from __future__ import annotations

from typing import Callable

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Task
from app.sync.sheets import SheetsSyncService
from app.sync.tasks_api import GoogleTasksSyncService

log = get_logger(__name__)


class TaskSyncer:
    """Push the current state of a task to every configured external system."""

    def __init__(
        self,
        *,
        sheets_factory: Callable[[], SheetsSyncService | None] | None,
        google_tasks_factory: Callable[[], GoogleTasksSyncService | None] | None,
    ) -> None:
        self._sheets_factory = sheets_factory
        self._google_tasks_factory = google_tasks_factory

    def sync(self, task_id: int) -> None:
        """Best-effort push. Never raises — logging only on failure so a
        Slack handler doesn't break because Google is down."""
        if task_id is None:
            return
        sheets = self._sheets_factory() if self._sheets_factory else None
        gtasks = (
            self._google_tasks_factory() if self._google_tasks_factory else None
        )
        if sheets is None and gtasks is None:
            return

        with session_scope() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            if sheets is not None:
                try:
                    sheets.sync(session, task)
                except Exception as e:  # noqa: BLE001
                    log.warning("sheets_sync_failed", task_id=task_id, error=str(e))
            if gtasks is not None:
                try:
                    gtasks.sync(session, task)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "google_tasks_sync_failed", task_id=task_id, error=str(e)
                    )


# Module-level holder — set once at startup, read everywhere.
_active: TaskSyncer | None = None


def set_active_syncer(syncer: TaskSyncer | None) -> None:
    global _active
    _active = syncer


def sync_task(task_id: int | None) -> None:
    """Best-effort sync of `task_id` via the active syncer.

    No-op when no syncer is registered (tests, dev runs without
    Google credentials, etc.). Never raises.
    """
    if task_id is None or _active is None:
        return
    try:
        _active.sync(task_id)
    except Exception as e:  # noqa: BLE001
        log.warning("task_sync_unexpected_failure", task_id=task_id, error=str(e))
