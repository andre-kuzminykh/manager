"""Concrete Postgres SyncRepo over the gs_* models (spec §14, §18.2).

All mutations go through the caller's transaction (the runner wraps a single
sync run in one commit — NFR-GS-006/015). Writes ONLY gs_* tables.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sheet_sync import (
    GsRecord,
    GsRecordState,
    GsSheetSnapshot,
    GsSyncError,
)
from app.sheet_sync.engine import RecordView


class SqlSyncRepo:
    def __init__(
        self, session: Session, *, integration_id: str, sync_run_id: str, source: str = "sheet"
    ) -> None:
        self._s = session
        self._iid = integration_id
        self._run = sync_run_id
        self._source = source

    # -- reads -----------------------------------------------------------
    def previously_seen_keys(self) -> set[str]:
        rows = self._s.execute(
            select(GsRecord.business_key)
            .where(GsRecord.integration_id == self._iid)
            .where(GsRecord.deleted_at.is_(None))
        ).scalars().all()
        return set(rows)

    def find_record(self, business_key: str) -> RecordView | None:
        rec = self._s.execute(
            select(GsRecord)
            .where(GsRecord.integration_id == self._iid)
            .where(GsRecord.business_key == business_key)
        ).scalar_one_or_none()
        if rec is None:
            return None
        cur_hash = None
        if rec.current_state_id:
            cur_hash = self._s.execute(
                select(GsRecordState.payload_hash).where(GsRecordState.id == rec.current_state_id)
            ).scalar_one_or_none()
        return RecordView(
            record_id=rec.id,
            current_payload_hash=cur_hash,
            current_state_id=rec.current_state_id,
            deleted=rec.deleted_at is not None,
        )

    # -- mutations -------------------------------------------------------
    def _add_state(
        self, *, record_id: str, event_type: str, payload: dict, payload_hash: str,
        previous_state_id: str | None,
    ) -> str:
        st = GsRecordState(
            record_id=record_id, event_type=event_type, source=self._source,
            payload=payload, payload_hash=payload_hash,
            previous_state_id=previous_state_id, sync_run_id=self._run,
        )
        self._s.add(st)
        self._s.flush()
        return st.id

    def create_record(self, *, business_key, payload, payload_hash, signature) -> str:
        rec = GsRecord(integration_id=self._iid, entity_type="task", business_key=business_key)
        self._s.add(rec)
        self._s.flush()
        sid = self._add_state(
            record_id=rec.id, event_type="created_from_sheet",
            payload=payload, payload_hash=payload_hash, previous_state_id=None,
        )
        rec.current_state_id = sid
        self._s.flush()
        return rec.id

    def update_record(self, *, record: RecordView, payload, payload_hash) -> None:
        sid = self._add_state(
            record_id=record.record_id, event_type="updated_from_sheet",
            payload=payload, payload_hash=payload_hash,
            previous_state_id=record.current_state_id,
        )
        self._set_current(record.record_id, sid)

    def restore_record(self, *, record: RecordView, payload, payload_hash) -> None:
        sid = self._add_state(
            record_id=record.record_id, event_type="restored_from_sheet",
            payload=payload, payload_hash=payload_hash,
            previous_state_id=record.current_state_id,
        )
        rec = self._s.get(GsRecord, record.record_id)
        if rec is not None:
            rec.deleted_at = None
            rec.current_state_id = sid
            rec.updated_at = datetime.now(timezone.utc)
        self._s.flush()

    def soft_delete(self, *, business_key: str) -> None:
        rec = self._s.execute(
            select(GsRecord)
            .where(GsRecord.integration_id == self._iid)
            .where(GsRecord.business_key == business_key)
        ).scalar_one_or_none()
        if rec is None or rec.deleted_at is not None:
            return
        # carry the last known payload forward into the deleted state
        last_payload, last_hash = {}, "sha256:deleted"
        if rec.current_state_id:
            cur = self._s.get(GsRecordState, rec.current_state_id)
            if cur is not None:
                last_payload, last_hash = cur.payload, cur.payload_hash
        sid = self._add_state(
            record_id=rec.id, event_type="deleted_from_sheet",
            payload=last_payload, payload_hash=last_hash,
            previous_state_id=rec.current_state_id,
        )
        rec.current_state_id = sid
        rec.deleted_at = datetime.now(timezone.utc)
        rec.updated_at = datetime.now(timezone.utc)
        self._s.flush()

    def _set_current(self, record_id: str, state_id: str) -> None:
        rec = self._s.get(GsRecord, record_id)
        if rec is not None:
            rec.current_state_id = state_id
            rec.updated_at = datetime.now(timezone.utc)
        self._s.flush()

    def save_error(self, *, row_number, business_key, error_type, message, raw_row) -> None:
        self._s.add(GsSyncError(
            sync_run_id=self._run, integration_id=self._iid, row_number=row_number,
            business_key=business_key, error_type=error_type, message=message, raw_row=raw_row,
        ))
        self._s.flush()

    def save_snapshot(self, *, business_key, row_number, row_hash, payload) -> None:
        self._s.add(GsSheetSnapshot(
            integration_id=self._iid, sync_run_id=self._run, business_key=business_key,
            row_number=row_number, row_hash=row_hash, normalized_payload=payload,
        ))
        self._s.flush()


__all__ = ["SqlSyncRepo"]
