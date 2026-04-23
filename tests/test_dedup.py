from app.slack_bot.dedup import claim_event


def test_first_claim_succeeds(sqlite_session):
    assert claim_event(sqlite_session, "Ev1") is True


def test_duplicate_claim_returns_false(sqlite_session):
    assert claim_event(sqlite_session, "Ev1") is True
    sqlite_session.commit()
    assert claim_event(sqlite_session, "Ev1") is False


def test_empty_event_id_treated_as_fresh(sqlite_session):
    assert claim_event(sqlite_session, "") is True
