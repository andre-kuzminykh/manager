"""FR-CR-05-61 — Pull side of the Google Tasks integration.

Push (Task → Google Tasks) lives in `tasks_api.py` and fires
on every commit. Pull (Google Tasks → Task) is this module.

What it does on each tick:

  - List every task in the configured `tasklist_id`.
  - For each API task, look up the DB row via
    `google_tasks_id`. Skip tasks that were created directly
    in Google Tasks UI (no DB row).
  - Diff title / notes / due / status against the DB row.
    Apply changes; record a `TaskStatusHistory` row when
    `status` flipped to/from `done`.
  - Detect deletes: any DB row with a `google_tasks_id` set
    but NOT present in the current API list → soft-delete
    (`deleted_at = now`) and refresh the TG card with the
    cancellation banner.
  - Refresh the TG card after every change so the operator
    sees the fresh state without leaving Telegram.

Best-effort. Failures on individual tasks log + continue. The
caller is the listener's per-tick loop, so a Google outage
just means the next tick retries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger
from app.models import (
    GoogleTasksSync,
    Task,
    TaskStatus,
    TaskStatusHistory,
)

log = get_logger(__name__)


@dataclass
class GoogleTasksPullReport:
    seen: int = 0
    updated: int = 0
    deleted: int = 0
    status_changed: int = 0
    errors: int = 0


class GoogleTasksPullService:
    """Periodic Google Tasks → DB sync. One instance per
    configured tasklist."""

    def __init__(
        self,
        *,
        credentials: Credentials,
        tasklist_id: str,
    ) -> None:
        self._service = build(
            "tasks", "v1", credentials=credentials, cache_discovery=False
        )
        self._tasklist_id = tasklist_id

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _list_all(self) -> list[dict[str, Any]]:
        """Page through every active task in the tasklist —
        Google Tasks API caps each page at 100. We don't request
        deleted/hidden tasks; deletes are detected by absence."""
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "tasklist": self._tasklist_id,
                "maxResults": 100,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._service.tasks().list(**kwargs).execute()
            out.extend(resp.get("items") or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def pull(
        self,
        session: Session,
        *,
        refresh_card: Callable[[Task], None] | None = None,
    ) -> GoogleTasksPullReport:
        """One pull cycle. Returns a counters report; never
        raises (per-task failures logged and counted)."""
        report = GoogleTasksPullReport()
        try:
            api_tasks = self._list_all()
        except Exception as e:  # noqa: BLE001
            log.warning("google_tasks_pull_list_failed", error=str(e))
            report.errors += 1
            return report

        report.seen = len(api_tasks)
        seen_google_ids: set[str] = set()
        # Track DB rows that the API mirrors. Anything in
        # `GoogleTasksSync` for this tasklist that isn't in the
        # set after this loop = deleted in Google Tasks.
        for api in api_tasks:
            gid = api.get("id")
            if not gid:
                continue
            seen_google_ids.add(gid)
            try:
                if self._apply_one(
                    session, api=api, refresh_card=refresh_card,
                    report=report,
                ):
                    pass
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                log.warning(
                    "google_tasks_pull_apply_failed",
                    google_task_id=gid,
                    error=str(e),
                )

        # Detect deletes.
        try:
            sync_rows = (
                session.query(GoogleTasksSync)
                .filter(
                    GoogleTasksSync.tasklist_id == self._tasklist_id,
                    GoogleTasksSync.google_task_id.isnot(None),
                )
                .all()
            )
        except Exception as e:  # noqa: BLE001
            log.warning("google_tasks_pull_query_sync_failed", error=str(e))
            return report
        for s in sync_rows:
            gid = s.google_task_id
            if gid is None or gid in seen_google_ids:
                continue
            task = session.get(Task, s.task_id)
            if task is None or task.deleted_at is not None:
                continue
            try:
                self._mark_deleted(
                    session, task=task, refresh_card=refresh_card
                )
                report.deleted += 1
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                log.warning(
                    "google_tasks_pull_delete_failed",
                    task_id=task.id,
                    error=str(e),
                )

        return report

    # ---- helpers ----------------------------------------------------

    def _apply_one(
        self,
        session: Session,
        *,
        api: dict[str, Any],
        refresh_card: Callable[[Task], None] | None,
        report: GoogleTasksPullReport,
    ) -> bool:
        """Apply one Google Tasks API row onto the matching DB
        row. Skip when there's no DB row (operator created the
        task directly in the Google Tasks UI — out of scope for
        this iteration). Returns True if a real change landed."""
        gid = api["id"]
        sync_row = (
            session.query(GoogleTasksSync)
            .filter(
                GoogleTasksSync.tasklist_id == self._tasklist_id,
                GoogleTasksSync.google_task_id == gid,
            )
            .one_or_none()
        )
        if sync_row is None:
            return False
        task = session.get(Task, sync_row.task_id)
        if task is None or task.deleted_at is not None:
            return False

        changed = False
        new_title = (api.get("title") or "").strip()
        if new_title and new_title != (task.title or ""):
            task.title = new_title[:10_000]
            changed = True

        # Google Tasks calls the description field `notes`.
        new_notes = (api.get("notes") or "").strip() or None
        if new_notes != task.description:
            task.description = new_notes
            changed = True

        # Due date: API returns RFC3339 datetime — normalise to date.
        new_due = _parse_due(api.get("due"))
        if new_due != task.due_date:
            task.due_date = new_due
            changed = True

        # Status: completed → done; needsAction → keep current
        # OPEN status (we don't downgrade done back to todo
        # silently — the operator would have edited the task in
        # the bot if they wanted that).
        api_status = (api.get("status") or "").lower()
        new_status: TaskStatus | None = None
        if api_status == "completed" and task.status != TaskStatus.done:
            new_status = TaskStatus.done
        elif (
            api_status == "needsaction"
            and task.status == TaskStatus.done
        ):
            # Google Tasks lets the operator un-check a done item;
            # respect that.
            new_status = TaskStatus.todo

        if new_status is not None:
            session.add(
                TaskStatusHistory(
                    task_id=task.id,
                    from_status=task.status,
                    to_status=new_status,
                    changed_by_slack_user_id=None,
                    reason="google_tasks_pull",
                    at=datetime.now(timezone.utc),
                )
            )
            task.status = new_status
            if new_status == TaskStatus.done:
                task.completed_at = datetime.now(timezone.utc)
            else:
                task.completed_at = None
            changed = True
            report.status_changed += 1

        if changed:
            sync_row.last_synced_at = datetime.now(timezone.utc)
            session.flush()
            report.updated += 1
            if refresh_card is not None:
                try:
                    refresh_card(task)
                except Exception as e:  # noqa: BLE001
                    log.info(
                        "google_tasks_pull_card_refresh_failed",
                        task_id=task.id,
                        error=str(e),
                    )
        return changed

    def _mark_deleted(
        self,
        session: Session,
        *,
        task: Task,
        refresh_card: Callable[[Task], None] | None,
    ) -> None:
        """Soft-delete a task that disappeared from Google Tasks.
        Mirrors the FR-CR-04-20 soft-delete pattern: set
        `deleted_at`, write a `cancelled` history row,
        refresh the TG card."""
        now = datetime.now(timezone.utc)
        task.deleted_at = now
        # Write a status-history row to make the audit trail
        # explicit: «cancelled by google_tasks_pull».
        session.add(
            TaskStatusHistory(
                task_id=task.id,
                from_status=task.status,
                to_status=TaskStatus.done,
                changed_by_slack_user_id=None,
                reason="google_tasks_pull_deleted",
                at=now,
            )
        )
        session.flush()
        if refresh_card is not None:
            try:
                refresh_card(task)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "google_tasks_pull_delete_card_refresh_failed",
                    task_id=task.id,
                    error=str(e),
                )


def _parse_due(due: str | None) -> date | None:
    """Google Tasks returns due in RFC3339 datetime form
    (`2026-04-30T00:00:00.000Z`). We store dates only — strip
    the time."""
    if not due:
        return None
    try:
        return datetime.fromisoformat(due.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


__all__ = ["GoogleTasksPullReport", "GoogleTasksPullService"]
