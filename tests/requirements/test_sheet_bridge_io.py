"""FR-SS — B-IO adapters: read_all_with_metadata (quoted range) + bridge<->sheet
field map. Fake sheets_client (no live Google).
"""
from __future__ import annotations

from app.sheet_sync.bridge_io import read_all_with_metadata
from app.sheet_sync.config import FIELD_BY_HEADER, TASK_HEADERS


def _cells(fields: dict) -> list[str]:
    return [fields.get(FIELD_BY_HEADER[h], "") for h in TASK_HEADERS]


def _raw(rows_fields: list[dict]) -> list[list[str]]:
    return [list(TASK_HEADERS)] + [_cells(f) for f in rows_fields]


class _Get:
    def __init__(self, values): self._v = values
    def execute(self): return {"values": self._v}


class _Values:
    def __init__(self, values): self._v = values
    def get(self, **kw): return _Get(self._v)


class _SS:
    def __init__(self, values): self._v = values
    def values(self): return _Values(self._v)


class _Svc:
    def __init__(self, values): self._v = values
    def spreadsheets(self): return _SS(self._v)


class FakeClient:
    def __init__(self, rows_fields, uuids):
        self._svc = _Svc(_raw(rows_fields))
        self._sid = "SS"
        self._tab = "Copy of main"        # spaced -> exercises quoting path
        self._uuids = uuids
    def read_row_uuids(self):
        return self._uuids


def test_reads_rows_and_attaches_uuids() -> None:
    rows_fields = [
        {"title": "A", "description": "d", "responsible": "Anna",
         "status": "To Do", "priority": "High", "category": "fundraising",
         "deadline_date": "2026-06-10"},
        {"title": "B", "status": "Done", "priority": "low"},
        {"title": "Заведена", "status": "todo"},        # row 4: human-added, no uuid
    ]
    uuids = {2: "uuid-A", 3: "uuid-B"}                   # row 4 absent
    out = read_all_with_metadata(FakeClient(rows_fields, uuids))
    assert [r.row_number for r in out] == [2, 3, 4]
    assert out[0].row_uuid == "uuid-A"
    assert out[2].row_uuid is None
    # field rename + status/priority normalization
    assert out[0].values["owner"] == "Anna"
    assert out[0].values["due_date"] == "2026-06-10"
    assert out[0].values["status"] == "todo"            # "To Do" -> todo
    assert out[1].values["status"] == "done"
    assert out[1].values["priority"] == "low"


def test_handles_missing_uuids_table() -> None:
    out = read_all_with_metadata(FakeClient([{"title": "T", "status": "todo"}], {}))
    assert out[0].row_uuid is None
    assert out[0].values["title"] == "T"


__all__: list[str] = []
