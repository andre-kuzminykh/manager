from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProcessedSlackEvent


def claim_event(session: Session, event_id: str) -> bool:
    """Attempt to record an event_id. Returns True if this is the first time we see it,
    False if the same event has already been processed (Slack retry)."""

    if not event_id:
        # Without an event_id we cannot dedup; err on the side of processing.
        return True

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        stmt = insert(ProcessedSlackEvent).values(event_id=event_id).on_conflict_do_nothing()
        result = session.execute(stmt)
        return result.rowcount > 0

    # Fallback for sqlite / tests.
    try:
        session.add(ProcessedSlackEvent(event_id=event_id))
        session.flush()
        return True
    except IntegrityError:
        session.rollback()
        return False
