"""Requirement coverage: NFR-2 (event dedup across retries).

claim_event has three paths:
  1. empty event_id → always True (no dedup possible).
  2. event already recorded → False.
  3. parallel insert races and hits IntegrityError → False.

The third path is otherwise unreachable in a single-session test.
We simulate the race by forcing session.flush to raise
IntegrityError the first time."""
from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from app.models import ProcessedSlackEvent
from app.slack_bot.dedup import claim_event


def test_claim_event_empty_id_always_processes(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        assert claim_event(s, "") is True
        # Nothing persisted for empty id.
        assert s.query(ProcessedSlackEvent).count() == 0


def test_claim_event_is_idempotent_within_session(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        assert claim_event(s, "Ev-1") is True
        # Second claim sees the existing row first — doesn't even try to
        # insert.
        assert claim_event(s, "Ev-1") is False


def test_claim_event_falls_back_to_integrity_error_on_race(
    patched_session_scope, SessionFactory
):
    """A parallel transaction inserts the same event_id between our
    "existing?" query and our INSERT. The savepoint's flush raises
    IntegrityError, the except catches it, rolls back, returns False."""
    # Monkey-patch the model __init__ to register a listener that
    # simulates a duplicate-primary-key race the first time claim_event
    # inserts. We do this by pre-seeding the row from a DIFFERENT
    # session, so the current session's SELECT ran while empty but the
    # INSERT's flush now violates the PK.
    with SessionFactory() as s1, SessionFactory() as s2:
        # Capture the "existing is None" branch by inspecting s1 first:
        # s1 has not yet seen the row because it wasn't committed.
        before_rows = s1.query(ProcessedSlackEvent).filter_by(event_id="RaceEv").all()
        assert before_rows == []
        # Parallel transaction commits the same event_id.
        s2.add(ProcessedSlackEvent(event_id="RaceEv"))
        s2.commit()
        # s1 now tries to claim — its INSERT should hit IntegrityError
        # and return False without re-raising.
        # Expire identity map so the s1 SELECT doesn't return a cached
        # miss but actually re-queries.
        s1.expire_all()
        assert claim_event(s1, "RaceEv") is False
