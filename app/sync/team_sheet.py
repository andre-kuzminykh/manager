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

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _append(self, rows: list[list[str]]) -> None:
        """Append ``rows`` after the last filled row in the tab.
        Used by the non-destructive `push` (FR-CR-05-27) to add
        new DB rows without touching operator-edited cells.
        """
        if not rows:
            return
        end_col = chr(ord("A") + len(SHEET_HEADERS) - 1)
        rng = f"{self._sheet_name}!A:{end_col}"
        self._service.spreadsheets().values().append(
            spreadsheetId=self._spreadsheet_id,
            range=rng,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def push(self, session: Session) -> int:
        """FR-CR-05-27 — NON-DESTRUCTIVE push: appends only DB
        rows that aren't on the sheet yet (matched by `id`,
        `telegram_user_id`, or `slack_user_id`). Operator-edited
        cells are never touched.

        Trade-off vs. the old «clear + rewrite» behaviour:
        deletions in the DB do NOT propagate to the sheet (the
        sheet is the operator's source of truth). To remove a
        row, the operator deletes it on the sheet, runs `--pull`,
        and the DB row vanishes too. Anything else risks losing
        operator edits — that bit us hard once already.

        Returns the row count actually appended.
        """
        # Read current sheet contents to figure out which DB rows
        # are missing.
        existing_rows = self._read_all()
        if existing_rows and existing_rows[0] and existing_rows[0][0].strip().lower() == "id":
            existing_rows = existing_rows[1:]
        sheet_ids: set[str] = set()
        sheet_tg_ids: set[str] = set()
        sheet_slack_ids: set[str] = set()
        for r in existing_rows:
            cells = list(r) + [""] * (len(SHEET_HEADERS) - len(r))
            id_cell = (cells[0] or "").strip()
            if id_cell:
                sheet_ids.add(id_cell)
            tg_id = (cells[2] or "").strip()
            if tg_id:
                sheet_tg_ids.add(tg_id)
            slack_id = (cells[4] or "").strip()
            if slack_id:
                sheet_slack_ids.add(slack_id)

        all_rows = to_sheet_rows(session)  # header + body
        if not all_rows:
            return 0
        body = all_rows[1:]
        new_rows: list[list[str]] = []
        for row in body:
            row_id = (row[0] or "").strip()
            tg_id = (row[2] or "").strip()
            slack_id = (row[4] or "").strip()
            already_on_sheet = (
                (row_id and row_id in sheet_ids)
                or (tg_id and tg_id in sheet_tg_ids)
                or (slack_id and slack_id in sheet_slack_ids)
            )
            if not already_on_sheet:
                new_rows.append(row)

        # If the sheet is empty (no header row at all), write the
        # full table including the header — first-time bootstrap.
        if not existing_rows and not any(c.strip() for r in (existing_rows or [[]]) for c in r):
            self._write(all_rows)
            return len(body)

        if new_rows:
            self._append(new_rows)
        return len(new_rows)


__all__ = ["TeamSheetSync"]
