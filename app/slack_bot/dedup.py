from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProcessedSlackEvent


def claim_event(session: Session, event_id: str) -> bool:
    """Attempt to record an event_id. Returns True if this is the first time we
    see it, False if the same event has already been processed (Slack retry).

    For Postgres we issue an ``INSERT ... ON CONFLICT DO NOTHING RETURNING
    event_id`` and check whether a row came back. Relying on ``rowcount`` for
    this statement is unreliable across driver versions — RETURNING gives us a
    deterministic signal.

    For other dialects (SQLite in tests) we fall back to the try/except
    IntegrityError pattern. ``claim_event`` is called as the very first thing
    inside the handler's transaction, so the rollback on conflict does not
    discard any other uncommitted work.
    """
    if not event_id:
        # Without an event_id we cannot dedup; err on the side of processing.
        return True

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        stmt = (
            pg_insert(ProcessedSlackEvent)
            .values(event_id=event_id)
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(ProcessedSlackEvent.event_id)
        )
        row = session.execute(stmt).fetchone()
        return row is not None

    # SQLite / generic fallback.
    try:
        session.add(ProcessedSlackEvent(event_id=event_id))
        session.flush()
        return True
    except IntegrityError:
        session.rollback()
        return False
