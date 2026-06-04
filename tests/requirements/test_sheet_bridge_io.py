"""FR-SS — B-IO adapters: read_all_with_metadata + bridge<->sheet field map.
Uses a fake sheets_client (no live Google) — exercises field name + status/
priority display mapping, and row_uuid attachment via DeveloperMetadata.
"""
from __future__ import annotations

from app.sheet_sync.bridge_io import read_all_with_metadata


class FakeClient:
    def __init__(self, rows, uuids):
        self._rows = rows
        self._uuids = uuids
    def read_rows(self):
        return self._rows
    def read_row_uuids(self):
        return self._uuids


def test_reads_rows_and_attaches_uuids() -> None:
    rows = [
        {"row_number": 2, "fields": {
            "title": "A", "description": "d", "responsible": "Anna",
            "status": "To Do", "priority": "High", "category": "fundraising",
            "deadline_date": "2026-06-10"}},
        {"row_number": 3, "fields": {
            "title": "B", "description": "", "responsible": "",
            "status": "Done", "priority": "low", "category": "",
            "deadline_date": ""}},
        {"row_number": 4, "fields": {                 # no uuid — new row by human
            "title": "Заведена", "description": "", "responsible": "",
            "status": "todo", "priority": "", "category": "", "deadline_date": ""}},
    ]
    uuids = {2: "uuid-A", 3: "uuid-B"}                # row 4 missing on purpose
    out = read_all_with_metadata(FakeClient(rows, uuids))
    assert [r.row_number for r in out] == [2, 3, 4]
    assert out[0].row_uuid == "uuid-A"
    assert out[2].row_uuid is None                    # new row marker
    # field rename + status/priority normalization
    assert out[0].values["owner"] == "Anna"
    assert out[0].values["due_date"] == "2026-06-10"
    assert out[0].values["status"] == "to_do" or out[0].values["status"] == "todo"  # normalized
    assert out[1].values["status"] == "done"
    assert out[1].values["priority"] == "low"


def test_handles_missing_uuids_table() -> None:
    out = read_all_with_metadata(FakeClient(
        [{"row_number": 2, "fields": {"title": "T", "status": "todo"}}], {}))
    assert out[0].row_uuid is None


__all__: list[str] = []
