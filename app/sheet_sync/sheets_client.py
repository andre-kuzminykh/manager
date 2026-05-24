"""Google Sheets I/O for the Task sync feature (service-account auth).

Reuses `load_service_account_credentials` (the SA already used for Calendar/
Docs/Sheets). Scope: spreadsheets read+write — the target sheet must be SHARED
with the service-account email as Editor.

This phase covers: resolve tab gid, read data rows, and seed structure
(headers + frozen header + dropdowns). Row-identity via DeveloperMetadata is
added with the sync engine.
"""
from __future__ import annotations

from typing import Any

from googleapiclient.discovery import build

from app.logging_setup import get_logger
from app.sheet_sync.config import (
    CATEGORY_DISPLAY,
    FIELD_BY_HEADER,
    PRIORITY_DISPLAY,
    STATUS_DISPLAY,
    TASK_COLUMNS,
    TASK_HEADERS,
)
from app.sync.google_auth import load_service_account_credentials

log = get_logger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _col_letter(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


_META_KEY = "gs_row_uuid"
_END_COL = _col_letter(len(TASK_HEADERS))
# 0-based column index per field (for DeveloperMetadata / data-validation ranges)
_COL_INDEX: dict[str, int] = {field: i for i, (_, field) in enumerate(TASK_COLUMNS)}


class SheetTabNotFound(RuntimeError):
    pass


class TasksSheetClient:
    def __init__(self, *, spreadsheet_id: str, tab_title: str, credentials: Any = None):
        creds = credentials or load_service_account_credentials(_SCOPES)
        if creds is None:
            raise RuntimeError(
                "no Google service-account credentials "
                "(GOOGLE_SERVICE_ACCOUNT_JSON[_PATH])"
            )
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._sid = spreadsheet_id
        self._tab = tab_title
        self._gid: int | None = None
        self._title: str | None = None

    # -- metadata ---------------------------------------------------------
    def resolve_tab(self) -> int:
        """Resolve the tab title → sheetId (gid). Caches. Raises if absent."""
        if self._gid is not None:
            return self._gid
        meta = (
            self._svc.spreadsheets()
            .get(spreadsheetId=self._sid, fields="properties(title),sheets(properties(sheetId,title))")
            .execute()
        )
        self._title = (meta.get("properties") or {}).get("title")
        for sh in meta.get("sheets", []):
            props = sh.get("properties") or {}
            if props.get("title") == self._tab:
                self._gid = int(props["sheetId"])
                return self._gid
        available = [(_sh.get("properties") or {}).get("title") for _sh in meta.get("sheets", [])]
        raise SheetTabNotFound(f"tab {self._tab!r} not found; available={available}")

    @property
    def spreadsheet_title(self) -> str | None:
        return self._title

    # -- read -------------------------------------------------------------
    def read_rows(self) -> list[dict]:
        """Return data rows (header skipped) as
        ``[{"row_number": int, "fields": {field: value}}]``.
        `row_number` is the 1-based Sheet row (header = row 1)."""
        rng = f"{self._tab}!A1:{_END_COL}"
        resp = (
            self._svc.spreadsheets().values()
            .get(spreadsheetId=self._sid, range=rng).execute()
        )
        values = list(resp.get("values") or [])
        if not values:
            return []
        out: list[dict] = []
        width = len(TASK_HEADERS)
        for i, raw in enumerate(values[1:], start=2):  # row 1 = header
            cells = list(raw) + [""] * (width - len(raw))
            fields = {FIELD_BY_HEADER[h]: cells[idx] for idx, h in enumerate(TASK_HEADERS)}
            out.append({"row_number": i, "fields": fields})
        return out

    # -- row identity via DeveloperMetadata (spec §20.5) ------------------
    def read_row_uuids(self) -> dict[int, str]:
        """Return {sheet_row_number: gs_row_uuid} from row DeveloperMetadata."""
        gid = self.resolve_tab()
        resp = (
            self._svc.spreadsheets().developerMetadata()
            .search(
                spreadsheetId=self._sid,
                body={"dataFilters": [{"developerMetadataLookup": {"metadataKey": _META_KEY}}]},
            )
            .execute()
        )
        out: dict[int, str] = {}
        for m in resp.get("matchedDeveloperMetadata", []):
            dm = m.get("developerMetadata") or {}
            rng = (dm.get("location") or {}).get("dimensionRange") or {}
            if rng.get("dimension") == "ROWS" and int(rng.get("sheetId", -1)) == gid:
                start = rng.get("startIndex")
                if start is not None and dm.get("metadataValue"):
                    out[int(start) + 1] = dm["metadataValue"]
        return out

    def stamp_row_uuids(self, mapping: dict[int, str]) -> None:
        """Attach hidden gs_row_uuid to many rows in ONE batchUpdate
        (1 write request — avoids the Sheets write-per-minute quota)."""
        if not mapping:
            return
        gid = self.resolve_tab()
        reqs = [
            {"createDeveloperMetadata": {"developerMetadata": {
                "metadataKey": _META_KEY,
                "metadataValue": value,
                "visibility": "DOCUMENT",
                "location": {"dimensionRange": {
                    "sheetId": gid, "dimension": "ROWS",
                    "startIndex": row_number - 1, "endIndex": row_number,
                }},
            }}}
            for row_number, value in sorted(mapping.items())
        ]
        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self._sid, body={"requests": reqs}
        ).execute()

    def stamp_row_uuid(self, row_number: int, value: str) -> None:
        self.stamp_row_uuids({row_number: value})

    def clear_data_rows(self) -> None:
        """Clear all data rows (below the header). Header/validation kept."""
        self._svc.spreadsheets().values().clear(
            spreadsheetId=self._sid, range=f"{self._tab}!A2:{_END_COL}", body={}
        ).execute()

    # -- append rows (DB → Sheet seed/export) -----------------------------
    def append_rows(self, rows: list[list[str]]) -> int:
        """Append data rows below the header (USER_ENTERED so dates/dropdowns
        parse). Each row must be in TASK_HEADERS order."""
        if not rows:
            return 0
        self._svc.spreadsheets().values().append(
            spreadsheetId=self._sid,
            range=f"{self._tab}!A:{_END_COL}",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        return len(rows)

    # -- write structure --------------------------------------------------
    def ensure_structure(self, *, responsible_options: list[str] | None = None) -> dict:
        """Write header row, freeze it, and set dropdown validations
        (Status / Priority / Category / Responsible). Idempotent."""
        gid = self.resolve_tab()
        # 1) headers
        self._svc.spreadsheets().values().update(
            spreadsheetId=self._sid,
            range=f"{self._tab}!A1:{_END_COL}1",
            valueInputOption="RAW",
            body={"values": [list(TASK_HEADERS)]},
        ).execute()

        requests: list[dict] = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        ]

        def _dropdown(field: str, options: list[str]) -> dict:
            col = _COL_INDEX[field]
            return {
                "setDataValidation": {
                    "range": {
                        "sheetId": gid,
                        "startRowIndex": 1,
                        "startColumnIndex": col,
                        "endColumnIndex": col + 1,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": v} for v in options],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            }

        requests.append(_dropdown("status", list(STATUS_DISPLAY)))
        requests.append(_dropdown("priority", list(PRIORITY_DISPLAY)))
        requests.append(_dropdown("category", list(CATEGORY_DISPLAY)))
        if responsible_options:
            requests.append(_dropdown("responsible", responsible_options))

        # Number formats so date/time cells render as dates/times (not serials).
        def _numfmt(field: str, type_: str, pattern: str) -> dict:
            col = _COL_INDEX[field]
            return {
                "repeatCell": {
                    "range": {
                        "sheetId": gid, "startRowIndex": 1,
                        "startColumnIndex": col, "endColumnIndex": col + 1,
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": type_, "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }

        for fld in ("start_date", "deadline_date", "completed_date"):
            requests.append(_numfmt(fld, "DATE", "yyyy-mm-dd"))
        for fld in ("start_time", "deadline_time", "completed_time"):
            requests.append(_numfmt(fld, "TIME", "HH:mm"))

        self._svc.spreadsheets().batchUpdate(
            spreadsheetId=self._sid, body={"requests": requests}
        ).execute()
        log.info(
            "sheet_sync_structure_applied",
            spreadsheet_id=self._sid, tab=self._tab,
            responsible_options=len(responsible_options or []),
        )
        return {"ok": True, "sheet_id": gid, "headers": len(TASK_HEADERS)}


__all__ = ["TasksSheetClient", "SheetTabNotFound"]
