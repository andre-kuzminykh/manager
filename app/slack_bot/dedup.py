from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProcessedSlackEvent


def claim_event(session: Session, event_id: str) -> bool:
    """Attempt to record ``event_id``.

    Returns True the first time we see the id, False on subsequent retries.

    Implementation note — we deliberately avoid ``INSERT ... ON CONFLICT DO
    NOTHING`` here: both the ``rowcount`` and ``RETURNING`` signals have
    proven finicky across psycopg versions. A plain INSERT + catch
    IntegrityError works identically on SQLite and PostgreSQL, and since
    ``claim_event`` is the first statement in the handler's transaction, a
    rollback on conflict does not discard any other work.
    """
    if not event_id:
        # No event_id → cannot dedup; process the event.
        return True

    # Short-circuit for events we've already recorded in the current session.
    existing = (
        session.query(ProcessedSlackEvent).filter_by(event_id=event_id).one_or_none()
    )
    if existing is not None:
        return False

    savepoint = session.begin_nested()
    try:
        session.add(ProcessedSlackEvent(event_id=event_id))
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        return False
    else:
        savepoint.commit()
        return True
