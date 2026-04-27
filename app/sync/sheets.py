from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger
from app.models import Employee, GoogleSheetsSync, SyncStatus, Task

_MENTION_RE = re.compile(r"^<@([UW][A-Z0-9]+)>$")

log = get_logger(__name__)

_HEADER_ROW = [
    "task_id",
    "title",
    "description",
    "owner",
    "priority",
    "category",
    "start_date",
    "start_time",
    "due_date",
    "due_time",
    "is_recurring",
    "recurring_weekdays",
    "recurring_start_time",
    "recurring_end_time",
    "status",
    "parent_task_id",
    "source_permalink",
    "created_at",
    "updated_at",
    "deleted_at",
    "completion_artifact",
]


def _resolve_owner_name(session: Session | None, task: Task) -> str:
    """Pick the most human-readable owner string for the spreadsheet.

    Order of preference:
    1. Employee.real_name / display_name (looked up by `owner_user_id`).
       Real name first because Slack's `display_name` often falls back
       to the @username (e.g. "admin"), while `real_name_normalized`
       almost always carries the actual person's name.
    2. `owner_display_name`, with a leading `<@Uxxx>` Slack mention
       stripped to a bare uid (so the cell never shows raw mention syntax).
    3. `owner_user_id` as last resort.
    """
    if task.owner_user_id and session is not None:
        emp = session.get(Employee, task.owner_user_id)
        if emp is not None:
            name = emp.real_name or emp.display_name
            if name:
                return name
    raw = (task.owner_display_name or "").strip()
    m = _MENTION_RE.match(raw)
    if m:
        return m.group(1)  # bare uid — better than "<@Uxxx>"
    if raw:
        return raw
    return task.owner_user_id or ""


def _task_row(task: Task, *, session: Session | None = None) -> list[str]:
    # Soft-deleted tasks: keep the row in the sheet but flip status to
    # "deleted" so the user sees what happened. The `deleted_at`
    # timestamp carries the audit info.
    status_text = "deleted" if task.deleted_at is not None else task.status.value
    return [
        str(task.id),
        task.title,
        task.description or "",
        _resolve_owner_name(session, task),
        task.priority.value,
        task.category or "",
        task.start_date.isoformat() if task.start_date else "",
        task.start_time.strftime("%H:%M") if task.start_time else "",
        task.due_date.isoformat() if task.due_date else "",
        task.due_time.strftime("%H:%M") if task.due_time else "",
        "yes" if task.is_recurring else "",
        ",".join(task.recurring_weekdays) if task.recurring_weekdays else "",
        task.recurring_start_time.strftime("%H:%M") if task.recurring_start_time else "",
        task.recurring_end_time.strftime("%H:%M") if task.recurring_end_time else "",
        status_text,
        str(task.parent_task_id) if task.parent_task_id else "",
        task.source_permalink or "",
        task.created_at.isoformat() if task.created_at else "",
        task.updated_at.isoformat() if task.updated_at else "",
        task.deleted_at.isoformat() if task.deleted_at else "",
        task.completion_artifact or "",
    ]


class SheetsSyncService:
    """Create/update a row in Google Sheets for each Task.

    Sync is idempotent per task: on first sync we append a row and remember
    row_id; subsequent syncs update the same row.
    """

    def __init__(
        self,
        *,
        credentials: Credentials,
        spreadsheet_id: str,
        sheet_name: str = "Main",
    ) -> None:
        # `Credentials` here is the type from google.oauth2.credentials, but
        # google.oauth2.service_account.Credentials also satisfies the
        # signed-request protocol — googleapiclient accepts both.
        self._service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name
        # Headers are written at most once per process; flip after the
        # first call to `_ensure_headers()` so we don't hit Sheets on
        # every sync.
        self._headers_checked = False

    def _ensure_headers(self) -> None:
        """Write our header row to row 1 if it's missing or doesn't match
        the current schema.

        This costs one extra GET per process. After the first call we
        flip a flag so subsequent syncs skip it. If the existing headers
        already match `_HEADER_ROW`, we don't touch them; if they
        differ, we overwrite row 1 — the bot's column order is the
        source of truth for the sheet.
        """
        if getattr(self, "_headers_checked", False):
            return
        self._headers_checked = True
        if getattr(self, "_service", None) is None:
            return  # tests inject `_service=None`; nothing to call
        end_col = chr(ord("A") + len(_HEADER_ROW) - 1)
        rng = f"{self._sheet_name}!A1:{end_col}1"
        try:
            resp = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._spreadsheet_id, range=rng)
                .execute()
            )
            current = (resp.get("values") or [[]])[0]
            if current == _HEADER_ROW:
                return
            self._service.spreadsheets().values().update(
                spreadsheetId=self._spreadsheet_id,
                range=rng,
                valueInputOption="RAW",
                body={"values": [_HEADER_ROW]},
            ).execute()
            log.info("sheets_headers_written", sheet=self._sheet_name)
        except HttpError as e:
            log.warning("sheets_headers_write_failed", error=str(e))

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _append(self, row: list[str]) -> dict[str, Any]:
        return (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A:Z",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            .execute()
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _update(self, row_id: int, row: list[str]) -> dict[str, Any]:
        return (
            self._service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A{row_id}:Z{row_id}",
                valueInputOption="RAW",
                body={"values": [row]},
            )
            .execute()
        )

    def sync(self, session: Session, task: Task) -> GoogleSheetsSync:
        # Make sure the header row exists / matches the current schema.
        # No-op after the first call per process.
        self._ensure_headers()

        record = (
            session.query(GoogleSheetsSync).filter_by(task_id=task.id).one_or_none()
        )
        if record is None:
            record = GoogleSheetsSync(
                task_id=task.id,
                spreadsheet_id=self._spreadsheet_id,
                status=SyncStatus.pending,
            )
            session.add(record)
            session.flush()

        row = _task_row(task, session=session)
        record.attempts += 1
        try:
            if record.row_id is None:
                resp = self._append(row)
                # updatedRange looks like "Tasks!A12:J12"; parse the row number
                updated_range = resp.get("updates", {}).get("updatedRange", "")
                row_id = _parse_row_id(updated_range)
                record.row_id = row_id
                task.google_sheets_row_id = row_id
            else:
                self._update(record.row_id, row)

            record.status = SyncStatus.success
            record.last_error = None
            record.last_synced_at = datetime.now(timezone.utc)
        except HttpError as e:
            record.status = SyncStatus.failed
            record.last_error = str(e)
            log.warning("sheets_sync_failed", task_id=task.id, error=str(e))
            raise
        finally:
            session.flush()
        return record


def _parse_row_id(updated_range: str) -> int | None:
    # Example: "Tasks!A12:J12" → 12
    try:
        _, cells = updated_range.split("!", 1)
        start = cells.split(":", 1)[0]
        digits = "".join(ch for ch in start if ch.isdigit())
        return int(digits) if digits else None
    except Exception:  # noqa: BLE001
        return None
