from unittest.mock import MagicMock

from app.slack_bot.dedup import claim_event


def test_first_claim_succeeds(sqlite_session):
    assert claim_event(sqlite_session, "Ev1") is True


def test_duplicate_claim_returns_false(sqlite_session):
    assert claim_event(sqlite_session, "Ev1") is True
    sqlite_session.commit()
    assert claim_event(sqlite_session, "Ev1") is False


def test_empty_event_id_treated_as_fresh(sqlite_session):
    assert claim_event(sqlite_session, "") is True


# The production path uses a Postgres-specific INSERT ... ON CONFLICT DO
# NOTHING RETURNING. Emulate both outcomes via a mocked Session so the
# behaviour is nailed down even without a live Postgres.


def _pg_session(row_returned):
    session = MagicMock()
    session.bind = MagicMock()
    session.bind.dialect.name = "postgresql"
    execute_result = MagicMock()
    execute_result.fetchone.return_value = (row_returned,) if row_returned else None
    session.execute.return_value = execute_result
    return session


def test_postgres_branch_returns_true_when_insert_happens():
    session = _pg_session(row_returned="EvX")
    assert claim_event(session, "EvX") is True


def test_postgres_branch_returns_false_when_conflict():
    session = _pg_session(row_returned=None)
    assert claim_event(session, "EvX") is False


def test_postgres_branch_uses_returning_clause_not_rowcount():
    """Regression: earlier version relied on result.rowcount > 0 which is
    unreliable across psycopg versions for ON CONFLICT DO NOTHING."""
    session = _pg_session(row_returned="EvX")
    claim_event(session, "EvX")
    # fetchone must be called; rowcount must NOT be consulted.
    session.execute.return_value.fetchone.assert_called_once()
    # Accessing rowcount would create a new mock attribute; verify it was not read.
    assert "rowcount" not in session.execute.return_value._mock_children
