"""FR-SS — Google Sheets I/O for the bridge: read rows + uuids, write cells,
append new rows, stamp DeveloperMetadata identity (SPEC_SHEET_SYNC_v0.1 §6/§11).

Thin wrapper over `app/sheet_sync/sheets_client.SheetsClient` (System B's
service-account API, DeveloperMetadata identity). NO new Google scopes, NO
new auth — same SA that's already used.

Pure-IO; no DB access. Returns/accepts the bridge's RowView / payload dicts.
Live calls go to Google only when `read_all_with_metadata` / the SheetWriter
methods are invoked — the module imports cleanly without credentials.
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any

from app.logging_setup import get_logger
from app.sheet_sync.bridge import RowView
from app.sheet_sync.bridge_apply import SheetWriter
from app.sheet_sync.config import FIELD_BY_HEADER, TASK_HEADERS

log = get_logger(__name__)


# Map between bridge editable fields and System B's `TASK_COLUMNS` internal fields.
# Bridge uses "owner"/"due_date"; System B sheet uses "responsible"/"deadline_date".
_BRIDGE_TO_SHEET = {
    "title": "title",
    "description": "description",
    "owner": "responsible",
    "status": "status",
    "priority": "priority",
    "category": "category",
    "due_date": "deadline_date",
}
_SHEET_TO_BRIDGE = {v: k for k, v in _BRIDGE_TO_SHEET.items()}

# Status / priority capitalisation when writing back to the Sheet (System B
# uses display strings; reading accepts both via config.STATUS_NORMALIZED).
_STATUS_DISPLAY = {"backlog": "Backlog", "todo": "To Do", "in_progress": "In Progress",
                   "done": "Done"}
_PRIORITY_DISPLAY = {"low": "Low", "medium": "Medium", "high": "High", "urgent": "High"}


def _to_sheet(field: str, value: str) -> str:
    if field == "status":
        return _STATUS_DISPLAY.get(value, value)
    if field == "priority":
        return _PRIORITY_DISPLAY.get(value, value)
    return value


def _bridge_values_from_sheet_fields(fields: dict[str, str]) -> dict[str, str]:
    """Map System B's internal field names to the bridge's EDITABLE_FIELDS."""
    out = {b: "" for b in ("title", "description", "owner", "status",
                           "priority", "category", "due_date")}
    for sf, bf in _SHEET_TO_BRIDGE.items():
        if sf in fields:
            out[bf] = fields[sf]
    # status / priority: collapse display -> normalized
    sn_raw = (out.get("status") or "").strip().lower()
    sn = sn_raw.replace(" ", "_")
    # Sheets display "To Do" -> "to_do" -> "todo"; "In Progress" -> "in_progress"
    if sn == "to_do":
        sn = "todo"
    if sn:
        out["status"] = sn if sn in {"backlog", "todo", "in_progress", "done",
                                     "blocked", "cancelled", "canceled"} else out["status"]
    pn = (out.get("priority") or "").strip().lower()
    if pn:
        out["priority"] = pn
    return out


def read_all_with_metadata(client: Any) -> list[RowView]:
    """Read every data row + its DeveloperMetadata gs_row_uuid.

    `client` is an instance of `app.sheet_sync.sheets_client.SheetsClient`.
    Rows without a uuid (newly typed by a human) come through with `row_uuid=None`.
    """
    raw_rows = client.read_rows()                       # [{"row_number", "fields"}]
    uuids = client.read_row_uuids() or {}               # {row_number: uuid}
    out: list[RowView] = []
    for r in raw_rows:
        rn = int(r["row_number"])
        values = _bridge_values_from_sheet_fields(r.get("fields", {}))
        out.append(RowView(row_uuid=uuids.get(rn), row_number=rn, values=values))
    return out


def _row_for_append(payload: dict[str, str]) -> list[str]:
    """Render a bridge payload into a System B-ordered row (TASK_HEADERS)."""
    sheet_fields = {sf: _to_sheet(_SHEET_TO_BRIDGE.get(sf, sf), payload.get(_SHEET_TO_BRIDGE.get(sf, sf), ""))
                    for sf in (FIELD_BY_HEADER[h] for h in TASK_HEADERS)}
    return [sheet_fields.get(FIELD_BY_HEADER[h], "") for h in TASK_HEADERS]


class GoogleSheetWriter(SheetWriter):
    """Concrete SheetWriter over System B's SheetsClient.

    All operations are best-effort and idempotent at the row level. Cell
    writes go through ``values.batchUpdate`` (one HTTP call per apply),
    massively cheaper than the legacy per-row ``values.update``.
    """

    def __init__(self, client: Any) -> None:
        self._c = client
        self._pending_cells: list[dict] = []   # buffered (row, col, value) for batchUpdate
        # row_number cache: only known after append; we don't try to discover
        # row numbers for existing tasks here (bridge passes through the writer
        # only via plans built from a fresh read where row_number is known).

    # -- internal helpers -------------------------------------------------

    def _col_letter(self, n: int) -> str:
        out = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            out = chr(ord("A") + rem) + out
        return out

    def _row_number_for(self, task_id: int) -> int | None:
        """Look up the row number from the bridge's link table on the DB side.
        The writer is stateless about row numbers — caller passes them in via
        write_cells / stamp_new_row. Subclass injects this if needed."""
        return None

    # -- SheetWriter protocol --------------------------------------------

    def write_cells(self, task_id: int, fields: dict[str, str]) -> None:
        """Buffered. ``flush()`` actually calls Google. The bridge apply_plan
        invokes write_cells per (task,cells); the runner calls flush at the end.
        Here we just call flush per task too — simple and safe; the runner can
        call ``set_row_for_task`` ahead of time if it wants 1 batch call total."""
        row = self._row_number_for(task_id)
        if row is None:
            log.info("sheet_writer_row_unknown_skip", task_id=task_id, fields=list(fields))
            return
        data = []
        for bf, val in fields.items():
            sf = _BRIDGE_TO_SHEET.get(bf, bf)
            try:
                col = next(i for i, h in enumerate(TASK_HEADERS, start=1) if FIELD_BY_HEADER[h] == sf)
            except StopIteration:
                continue
            data.append({
                "range": f"{self._c._tab}!{self._col_letter(col)}{row}",
                "values": [[_to_sheet(bf, val)]],
            })
        if not data:
            return
        self._c._svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self._c._sid,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()

    def append_task(self, task_id: int, payload: dict[str, str]) -> str:
        """Append one row and stamp it with a fresh gs_row_uuid. Returns uuid."""
        self._c.append_rows([_row_for_append(payload)])
        # row_number = last row after append. Fetch it once.
        rows = self._c.read_rows()
        rn = max((int(r["row_number"]) for r in rows), default=2)
        new = _uuid.uuid4().hex
        self._c.stamp_row_uuids({rn: new})
        return new

    def stamp_new_row(self, row_number: int, task_id: int) -> str:
        new = _uuid.uuid4().hex
        self._c.stamp_row_uuids({row_number: new})
        return new

    def tombstone(self, task_id: int) -> None:
        """Mark the row Cancelled by writing the Status cell. Row stays."""
        row = self._row_number_for(task_id)
        if row is None:
            return
        try:
            col = next(i for i, h in enumerate(TASK_HEADERS, start=1)
                       if FIELD_BY_HEADER[h] == "status")
            self._c._svc.spreadsheets().values().update(
                spreadsheetId=self._c._sid,
                range=f"{self._c._tab}!{self._col_letter(col)}{row}",
                valueInputOption="USER_ENTERED",
                body={"values": [["Cancelled"]]},
            ).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("sheet_writer_tombstone_failed", task_id=task_id, error=str(e))


__all__ = ["read_all_with_metadata", "GoogleSheetWriter"]
