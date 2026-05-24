"""FR-GS-* — versioned sync engine logic (DB-free, fake repo).

Covers UC-GS-005 (new→create), UC-GS-006 (update), UC-GS-007 (no-op),
UC-GS-008 (missing→soft-delete), restore, UC-GS-010 (invalid→no mutation),
UC-GS-009 (empty ignored), and new-row DeveloperMetadata writeback.
"""
from __future__ import annotations

from app.sheet_sync.engine import RecordView, run_sync
from app.sheet_sync.validation import validate_task_row


def _resolver(_name):  # always resolves (assignee irrelevant for these tests)
    return ("empty", None)


def _row(row_number, row_uuid, **fields):
    base = {h: "" for h in (
        "title", "description", "responsible", "status", "priority", "category",
        "start_date", "start_time", "deadline_date", "deadline_time",
        "completed_date", "completed_time", "comments",
    )}
    base.update(fields)
    return {"row_number": row_number, "row_uuid": row_uuid, "fields": base}


def _hash_for(**fields):
    base = {h: "" for h in (
        "title", "description", "responsible", "status", "priority", "category",
        "start_date", "start_time", "deadline_date", "deadline_time",
        "completed_date", "completed_time", "comments",
    )}
    base.update(fields)
    return validate_task_row(base, resolve_assignee=_resolver).payload_hash


class FakeRepo:
    def __init__(self, records=None, seen=None):
        self.records: dict[str, RecordView] = records or {}
        self.seen: set[str] = seen or set()
        self.errors: list[dict] = []
        self.snapshots: list[dict] = []
        self.created: list[str] = []
        self.updated: list[str] = []
        self.restored: list[str] = []
        self.deleted: list[str] = []

    def previously_seen_keys(self):
        return set(self.seen)

    def find_record(self, business_key):
        return self.records.get(business_key)

    def create_record(self, *, business_key, payload, payload_hash, signature):
        rid = "rec_" + business_key
        self.records[business_key] = RecordView(rid, payload_hash, "st_" + business_key, False)
        self.created.append(business_key)
        return rid

    def update_record(self, *, record, payload, payload_hash):
        record.current_payload_hash = payload_hash
        self.updated.append(record.record_id)

    def restore_record(self, *, record, payload, payload_hash):
        record.deleted = False
        record.current_payload_hash = payload_hash
        self.restored.append(record.record_id)

    def soft_delete(self, *, business_key):
        self.deleted.append(business_key)
        if business_key in self.records:
            self.records[business_key].deleted = True

    def save_error(self, **kw):
        self.errors.append(kw)

    def save_snapshot(self, **kw):
        self.snapshots.append(kw)


_VALID = dict(title="Prepare launch", status="To Do", priority="High")


def test_new_row_creates_record_and_writeback():
    repo = FakeRepo()
    stats = run_sync(
        [_row(2, None, **_VALID)], repo=repo, resolve_assignee=_resolver,
        uuid_factory=lambda: "U-NEW",
    )
    assert stats.created == 1 and stats.writebacks == [(2, "U-NEW")]
    assert "U-NEW" in repo.records and len(repo.snapshots) == 1


def test_existing_changed_row_updates_state():
    repo = FakeRepo(records={"u1": RecordView("rec_u1", "sha256:old", "st1", False)},
                    seen={"u1"})
    stats = run_sync([_row(2, "u1", **_VALID)], repo=repo, resolve_assignee=_resolver)
    assert stats.updated == 1 and "rec_u1" in repo.updated


def test_noop_row_unchanged():
    h = _hash_for(**_VALID)
    repo = FakeRepo(records={"u1": RecordView("rec_u1", h, "st1", False)}, seen={"u1"})
    stats = run_sync([_row(2, "u1", **_VALID)], repo=repo, resolve_assignee=_resolver)
    assert stats.unchanged == 1 and stats.updated == 0 and not repo.updated


def test_missing_known_row_soft_deleted():
    repo = FakeRepo(records={"u1": RecordView("rec_u1", "sha256:x", "st1", False)},
                    seen={"u1"})
    stats = run_sync([], repo=repo, resolve_assignee=_resolver)  # u1 not present
    assert stats.deleted == 1 and repo.deleted == ["u1"]


def test_reappearing_deleted_row_restored():
    repo = FakeRepo(records={"u1": RecordView("rec_u1", "sha256:old", "st1", True)},
                    seen=set())
    stats = run_sync([_row(2, "u1", **_VALID)], repo=repo, resolve_assignee=_resolver)
    assert stats.updated == 1 and "rec_u1" in repo.restored
    assert repo.records["u1"].deleted is False


def test_invalid_row_records_error_no_mutation():
    repo = FakeRepo()
    stats = run_sync(
        [_row(2, None, status="To Do", priority="High")],  # missing title
        repo=repo, resolve_assignee=_resolver,
    )
    assert stats.errors == 1 and stats.created == 0
    assert repo.errors and repo.errors[0]["error_type"] == "missing_title"
    assert not repo.records


def test_empty_row_ignored():
    repo = FakeRepo()
    stats = run_sync([_row(2, None)], repo=repo, resolve_assignee=_resolver)
    assert stats == type(stats)()  # all zero, no writebacks
    assert not repo.errors and not repo.records


def test_mixed_pass_counts():
    h = _hash_for(**_VALID)
    repo = FakeRepo(
        records={
            "u1": RecordView("rec_u1", h, "s1", False),          # noop
            "u2": RecordView("rec_u2", "sha256:old", "s2", False),  # update
            "u3": RecordView("rec_u3", "sha256:z", "s3", False),  # missing → delete
        },
        seen={"u1", "u2", "u3"},
    )
    stats = run_sync(
        [
            _row(2, "u1", **_VALID),                       # unchanged
            _row(3, "u2", title="X", status="Done", priority="Low"),  # updated
            _row(4, None, title="New", status="Backlog", priority="Low"),  # created
            _row(5, None),                                  # empty → ignored
        ],
        repo=repo, resolve_assignee=_resolver, uuid_factory=lambda: "U-NEW",
    )
    assert (stats.created, stats.updated, stats.unchanged, stats.deleted) == (1, 1, 1, 1)
    assert stats.writebacks == [(4, "U-NEW")]
