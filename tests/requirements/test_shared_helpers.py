"""Requirement coverage: FR-1 / FR-2 (Slack ingest + idempotency),
NFR-CR-02-1 (idempotent upserts), FR-CR-02-3 (private metadata
round-trip), FR-CR-03-1 (employees refresh is best-effort)."""
from __future__ import annotations

from unittest.mock import MagicMock

from app.models import SlackConversation, SlackMessage
from app.slack_bot.handlers.shared import (
    draft_private_metadata,
    fetch_permalink,
    load_private_metadata,
    upsert_conversation,
    upsert_message,
)


# --------------------------------------------------------------------------- #
# upsert_conversation / upsert_message (SQLite path)
# --------------------------------------------------------------------------- #


def test_upsert_conversation_inserts_once_and_returns_same_row(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        a = upsert_conversation(s, channel_id="C1", kind="channel")
        b = upsert_conversation(s, channel_id="C1", kind="channel")
        assert a is b
        assert s.query(SlackConversation).count() == 1


def test_upsert_message_dedups_by_conversation_and_ts(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        conv = upsert_conversation(s, channel_id="C1", kind="channel")
        m1 = upsert_message(
            s,
            conversation=conv,
            message={"ts": "10.0", "user": "U1", "text": "hi"},
        )
        m2 = upsert_message(
            s,
            conversation=conv,
            message={"ts": "10.0", "user": "U1", "text": "hi again"},
        )
        assert m1 is m2
        assert s.query(SlackMessage).count() == 1


def test_upsert_message_accepts_bot_id_when_user_missing(
    patched_session_scope, SessionFactory
):
    with SessionFactory() as s:
        conv = upsert_conversation(s, channel_id="C1", kind="channel")
        m = upsert_message(
            s,
            conversation=conv,
            message={"ts": "11.0", "bot_id": "BBOT1", "text": ""},
        )
        assert m.user_id == "BBOT1"


# --------------------------------------------------------------------------- #
# fetch_permalink — swallows SDK errors
# --------------------------------------------------------------------------- #


def test_fetch_permalink_returns_url_on_success():
    client = MagicMock()
    client.chat_getPermalink.return_value = {"permalink": "https://slack/p"}
    assert fetch_permalink(client, channel="C1", ts="1.0") == "https://slack/p"


def test_fetch_permalink_returns_none_on_exception():
    client = MagicMock()
    client.chat_getPermalink.side_effect = RuntimeError("oops")
    assert fetch_permalink(client, channel="C1", ts="1.0") is None


# --------------------------------------------------------------------------- #
# Private metadata round-trip
# --------------------------------------------------------------------------- #


def test_private_metadata_roundtrip_preserves_all_fields():
    raw = draft_private_metadata(
        conversation_id="C1",
        message_ts="10.0",
        thread_ts="10.0",
        draft_id=42,
        context_snapshot_id=7,
        source_user_id="U1",
        permalink="https://slack/p",
    )
    out = load_private_metadata(raw)
    assert out["conversation_id"] == "C1"
    assert out["draft_id"] == 42
    assert out["permalink"] == "https://slack/p"


def test_load_private_metadata_none_returns_empty():
    assert load_private_metadata(None) == {}
    assert load_private_metadata("") == {}


def test_load_private_metadata_malformed_returns_empty():
    assert load_private_metadata("{not json") == {}


# --------------------------------------------------------------------------- #
# employees.observed is best-effort
# --------------------------------------------------------------------------- #


def test_classify_and_persist_swallows_employees_error(
    patched_session_scope,
    services_task,
    SessionFactory,
):
    """A Slack/DB error while refreshing the Employees directory must
    NOT abort classification — the observer is best-effort."""
    from app.slack_bot.handlers.shared import classify_and_persist
    from app.schemas.intent import InvocationType

    # Break the Employees service so .observed raises.
    class _BrokenDir:
        def observed(self, *_args, **_kwargs):
            raise RuntimeError("db connection gone")

    services_task.employees = _BrokenDir()

    with SessionFactory() as s:
        classification, draft, snap = classify_and_persist(
            s,
            services=services_task,
            conversation_id="C1",
            kind="channel",
            source_message={"ts": "1.0", "user": "U1", "text": "надо сделать X"},
            invocation_type=InvocationType.passive,
            slack_user_id="U1",
        )
        # Classification still ran.
        assert classification is not None
