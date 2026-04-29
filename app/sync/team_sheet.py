"""FR-CR-05-10 — Two-way sync between `team_members` and the
`Team` tab of the spreadsheet pointed to by
``GOOGLE_TEAM_SHEETS_SPREADSHEET_ID``.

Operator workflow:

1. First run: ``python -m ops.sync_team --pull-then-push`` seeds
   the table from `telegram_chat_members` + `employees`, writes
   the seeded rows to the sheet (with `id` populated so the
   operator can edit them in place).
2. Operator polishes the sheet — sets `real_name`, `role`,
   `email`, marks bot accounts inactive.
3. Subsequent runs: ``python -m ops.sync_team --pull`` reads the
   sheet back into the DB. The bot's owner-resolution sees the
   updated registry on the next ingest call.

Sync is row-level. The `id` column is the primary join key — when
present, the row is matched by id; otherwise we fall back to
`telegram_user_id` then `slack_user_id`. Rows added by hand on the
sheet (no `id`) get inserted as new DB rows on the next pull, and
the next push echoes their assigned id back to the sheet.
"""
from __future__ import annotations

from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger
from app.services.team_members import (
    SHEET_HEADERS,
    to_sheet_rows,
    upsert_from_sheet_rows,
)

log = get_logger(__name__)


class TeamSheetSync:
    """Pull-then-push wrapper around the `Team` tab.

    Constructor takes the same ``credentials`` shape as the existing
    `SheetsSyncService` — service-account or OAuth user creds, both
    work because googleapiclient accepts either.
    """

    def __init__(
        self,
        *,
        credentials: Any,
        spreadsheet_id: str,
        sheet_name: str = "Team",
    ) -> None:
        self._service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name

    # --- read --------------------------------------------------------

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _read_all(self) -> list[list[str]]:
        end_col = chr(ord("A") + len(SHEET_HEADERS) - 1)
        rng = f"{self._sheet_name}!A1:{end_col}"
        resp = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=rng)
            .execute()
        )
        return list(resp.get("values") or [])

    def pull(self, session: Session) -> tuple[int, int]:
        """Read the sheet, upsert into the DB. Returns
        ``(updated, inserted)``. Skips the header row."""
        rows = self._read_all()
        if not rows:
            log.info("team_sheet_pull_empty", sheet=self._sheet_name)
            return 0, 0
        # Drop the header if present.
        if rows and rows[0] and rows[0][0].strip().lower() == "id":
            rows = rows[1:]
        return upsert_from_sheet_rows(session, rows)

    # --- write -------------------------------------------------------

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _clear(self) -> None:
        end_col = chr(ord("A") + len(SHEET_HEADERS) - 1)
        rng = f"{self._sheet_name}!A1:{end_col}"
        self._service.spreadsheets().values().clear(
            spreadsheetId=self._spreadsheet_id,
            range=rng,
            body={},
        ).execute()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _write(self, values: list[list[str]]) -> None:
        end_col = chr(ord("A") + len(SHEET_HEADERS) - 1)
        last_row = max(len(values), 1)
        rng = f"{self._sheet_name}!A1:{end_col}{last_row}"
        self._service.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet_id,
            range=rng,
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

    def push(self, session: Session) -> int:
        """Materialise the DB into the sheet. Returns the row count
        written (excluding the header)."""
        values = to_sheet_rows(session)
        # Clear first so deletions in the DB are reflected. Cheap on
        # a sub-1000-row registry.
        try:
            self._clear()
        except HttpError as e:
            log.warning("team_sheet_clear_failed", error=str(e))
        self._write(values)
        return max(0, len(values) - 1)


__all__ = ["TeamSheetSync"]
