"""Versioned sync engine — pure orchestration (spec §14.3).

Identity for tasks = internal row uuid carried on the Sheet row via
DeveloperMetadata (spec §20.5, operator choice). The Sheets layer hands each
row a `row_uuid` (or None for a brand-new row); the engine never reads/writes
the Sheet itself — it talks to a `SyncRepo`. This keeps the diff/versioning
logic DB-free and unit-testable; `app.sheet_sync.repo.SqlSyncRepo` is the
concrete Postgres implementation.

Guarantees: idempotent (same hash → no new state, NFR-GS-009), validation
errors never mutate state (FR-GS-027/PR-005), append-only history (PR-002),
soft-delete only (PR-004).
"""
from __future__ import annotations

import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.sheet_sync.validation import AssigneeResolver, validate_task_row


@dataclass
class RecordView:
    record_id: str
    current_payload_hash: str | None
    current_state_id: str | None
    deleted: bool


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: int = 0
    # (row_number, new_business_key) for rows that need a DeveloperMetadata stamp
    writebacks: list[tuple[int, str]] = field(default_factory=list)


class SyncRepo(Protocol):
    def previously_seen_keys(self) -> set[str]: ...
    def find_record(self, business_key: str) -> RecordView | None: ...
    def create_record(
        self, *, business_key: str, payload: dict, payload_hash: str, signature: str | None
    ) -> str: ...
    def update_record(
        self, *, record: RecordView, payload: dict, payload_hash: str
    ) -> None: ...
    def restore_record(
        self, *, record: RecordView, payload: dict, payload_hash: str
    ) -> None: ...
    def soft_delete(self, *, business_key: str) -> None: ...
    def save_error(
        self, *, row_number: int | None, business_key: str | None,
        error_type: str, message: str, raw_row: dict | None,
    ) -> None: ...
    def save_snapshot(
        self, *, business_key: str, row_number: int | None, row_hash: str, payload: dict
    ) -> None: ...


def _new_uuid() -> str:
    return str(_uuid.uuid4())


def run_sync(
    rows: list[dict],
    *,
    repo: SyncRepo,
    resolve_assignee: AssigneeResolver,
    tz: str = "UTC",
    uuid_factory: Callable[[], str] = _new_uuid,
) -> SyncStats:
    """Apply one sync pass.

    `rows`: ``[{"row_number": int, "fields": {field: value}, "row_uuid": str|None}]``
    (row_uuid = the DeveloperMetadata id, or None for a brand-new row).
    """
    stats = SyncStats()
    present: set[str] = set()

    for row in rows:
        res = validate_task_row(row["fields"], resolve_assignee=resolve_assignee, tz=tz)

        if res.is_empty:
            # Fully empty row: ignore (FR-GS-021). If it previously had an id,
            # its key simply won't be in `present` → soft-deleted below.
            continue

        if res.errors:
            stats.errors += 1
            first = res.errors[0]
            repo.save_error(
                row_number=row.get("row_number"),
                business_key=row.get("row_uuid"),
                error_type=first["error_type"],
                message="; ".join(e["message"] for e in res.errors),
                raw_row=row.get("fields"),
            )
            continue

        # Valid row → resolve identity.
        business_key = row.get("row_uuid")
        if not business_key:
            # brand-new row — assign uuid, stamp back via DeveloperMetadata
            business_key = uuid_factory()
            stats.writebacks.append((row["row_number"], business_key))

        present.add(business_key)
        record = repo.find_record(business_key)

        if record is None:
            repo.create_record(
                business_key=business_key,
                payload=res.payload,
                payload_hash=res.payload_hash,
                signature=res.signature,
            )
            stats.created += 1
            repo.save_snapshot(
                business_key=business_key, row_number=row["row_number"],
                row_hash=res.payload_hash, payload=res.payload,
            )
            continue

        if record.deleted:
            repo.restore_record(record=record, payload=res.payload, payload_hash=res.payload_hash)
            stats.updated += 1
        elif record.current_payload_hash == res.payload_hash:
            stats.unchanged += 1
        else:
            repo.update_record(record=record, payload=res.payload, payload_hash=res.payload_hash)
            stats.updated += 1

        repo.save_snapshot(
            business_key=business_key, row_number=row["row_number"],
            row_hash=res.payload_hash, payload=res.payload,
        )

    # Soft-delete previously-known keys absent from this scan (FR-GS-019/020).
    for key in repo.previously_seen_keys() - present:
        repo.soft_delete(business_key=key)
        stats.deleted += 1

    return stats


__all__ = ["RecordView", "SyncStats", "SyncRepo", "run_sync"]
