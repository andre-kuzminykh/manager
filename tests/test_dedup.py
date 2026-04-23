from app.models import ProcessedSlackEvent
from app.slack_bot.dedup import claim_event


def test_first_claim_succeeds(sqlite_session):
    assert claim_event(sqlite_session, "Ev1") is True


def test_duplicate_claim_returns_false(sqlite_session):
    assert claim_event(sqlite_session, "Ev1") is True
    sqlite_session.commit()
    assert claim_event(sqlite_session, "Ev1") is False


def test_empty_event_id_treated_as_fresh(sqlite_session):
    assert claim_event(sqlite_session, "") is True


def test_duplicate_within_same_uncommitted_session(sqlite_session):
    assert claim_event(sqlite_session, "Ev2") is True
    assert claim_event(sqlite_session, "Ev2") is False


def test_many_distinct_events_all_succeed(sqlite_session):
    for i in range(25):
        assert claim_event(sqlite_session, f"Ev{i}") is True
    sqlite_session.commit()
    assert sqlite_session.query(ProcessedSlackEvent).count() == 25


def test_rollback_on_conflict_does_not_leak_row(sqlite_session):
    """Conflict path must not leave a phantom row behind."""
    assert claim_event(sqlite_session, "Ev3") is True
    sqlite_session.commit()
    # Second attempt → conflict → savepoint rolls back internally.
    assert claim_event(sqlite_session, "Ev3") is False
    # Still exactly one row.
    assert (
        sqlite_session.query(ProcessedSlackEvent).filter_by(event_id="Ev3").count() == 1
    )
