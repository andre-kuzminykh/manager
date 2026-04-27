from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger
from app.models import GoogleSheetsSync, SyncStatus, Task

log = get_logger(__name__)

_HEADER_ROW = [
    "task_id",
    "title",
    "description",
    "owner",
    "priority",
    "due_date",
    "due_time",
    "status",
    "source_permalink",
    "created_at",
    "updated_at",
]


def _task_row(task: Task) -> list[str]:
    return [
        str(task.id),
        task.title,
        task.description or "",
        task.owner_display_name or task.owner_user_id or "",
        task.priority.value,
        task.due_date.isoformat() if task.due_date else "",
        task.due_time.strftime("%H:%M") if task.due_time else "",
        task.status.value,
        task.source_permalink or "",
        task.created_at.isoformat() if task.created_at else "",
        task.updated_at.isoformat() if task.updated_at else "",
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
        sheet_name: str = "Tasks",
    ) -> None:
        self._service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name

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

        row = _task_row(task)
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
