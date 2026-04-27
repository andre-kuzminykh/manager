"""Additional dense unit tests: models, audit logs, migration metadata,
edge cases in dedup / rate limiter / context retriever / orchestrator."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import (
    ActionDraft,
    ActionDraftState,
    AuditLog,
    ContextSnapshot,
    GoogleSheetsSync,
    GoogleTasksSync,
    IntentInference,
    OAuthCredential,
    ProcessedSlackEvent,
    SlackConversation,
    SlackMessage,
    SyncStatus,
)
from app.models.intent import IntentType as IE


# =============================================================================
# Model sanity (schema covers every spec entity)
# =============================================================================


@pytest.mark.parametrize(
    "entity_type",
    [
        SlackConversation,
        SlackMessage,
        ContextSnapshot,
        ProcessedSlackEvent,
        IntentInference,
        ActionDraft,
        GoogleSheetsSync,
        GoogleTasksSync,
        AuditLog,
        OAuthCredential,
    ],
)
def test_spec_entity_exists_in_metadata(entity_type):
    from app.models import Base

    assert entity_type.__tablename__ in Base.metadata.tables


@pytest.mark.parametrize(
    "column",
    [
        "title",
        "description",
        "owner_user_id",
        "owner_display_name",
        "priority",
        "due_date",
        "status",
        "source_conversation_id",
        "source_message_ts",
        "source_thread_ts",
        "source_permalink",
        "context_snapshot_id",
        "created_by_slack_user_id",
        "google_sheets_row_id",
        "google_tasks_id",
        "created_at",
        "updated_at",
    ],
)
def test_task_has_required_column(column):
    from app.models import Base

    assert column in Base.metadata.tables["tasks"].columns


def test_action_draft_state_enum_members():
    assert {m.value for m in ActionDraftState} == {
        "proposed",
        "confirmed",
        "edited",
        "ignored",
        "expired",
        "failed",
    }


def test_sync_status_enum_members():
    assert {m.value for m in SyncStatus} == {"pending", "success", "failed"}


def test_audit_log_roundtrip(session):
    log = AuditLog(category="c", action="a", actor="U1", payload={"k": "v"})
    session.add(log)
    session.flush()
    fetched = session.query(AuditLog).one()
    assert fetched.category == "c"
    assert fetched.payload["k"] == "v"


# =============================================================================
# Dedup edges
# =============================================================================


def test_dedup_rejects_duplicate_across_commits(SessionFactory):
    from app.slack_bot.dedup import claim_event

    with SessionFactory() as s:
        assert claim_event(s, "EvA")
        s.commit()
    with SessionFactory() as s:
        assert not claim_event(s, "EvA")


def test_dedup_accepts_distinct_events(SessionFactory):
    from app.slack_bot.dedup import claim_event

    with SessionFactory() as s:
        for i in range(10):
            assert claim_event(s, f"Ev-{i}")
        s.commit()
    with SessionFactory() as s:
        assert s.query(ProcessedSlackEvent).count() == 10


@pytest.mark.parametrize("event_id", ["", None])
def test_dedup_falsy_event_id_is_allowed(SessionFactory, event_id):
    from app.slack_bot.dedup import claim_event

    with SessionFactory() as s:
        assert claim_event(s, event_id) is True


# =============================================================================
# Rate limiter edges
# =============================================================================


def test_rate_limiter_records_last_sent_time():
    import time

    from app.slack_bot.rate_limiter import RateAwareSlackSender

    class R:
        data = {"ok": True}

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            return R()

    s = RateAwareSlackSender(Cli(), min_interval_seconds=0.0)
    before = time.monotonic()
    s.post_message(channel="C", text="a")
    assert s._last_sent["C"] >= before


def test_rate_limiter_first_call_not_throttled():
    import time

    from app.slack_bot.rate_limiter import RateAwareSlackSender

    class R:
        data = {"ok": True}

    class Cli:
        def chat_postMessage(self, **kw):  # noqa: N802
            return R()

    s = RateAwareSlackSender(Cli(), min_interval_seconds=5.0)
    t0 = time.monotonic()
    s.post_message(channel="NEW", text="x")
    assert (time.monotonic() - t0) < 0.5


# =============================================================================
# Context retriever edges
# =============================================================================


def test_context_retriever_source_ts_exposed_in_snapshot():
    from app.context.retriever import ContextRetriever

    class Cli:
        def conversations_history(self, **kw):
            return {"messages": []}

        def conversations_replies(self, **kw):
            return {"messages": []}

    w = ContextRetriever(Cli(), window_before=3).build(
        conversation_id="C1", source_message={"ts": "3.14", "user": "U1", "text": "src"}
    )
    snap = w.to_snapshot_dict()
    assert snap["source_ts"] == "3.14"


def test_context_retriever_flat_messages_orders_history_then_source():
    from app.context.retriever import ContextRetriever

    class Cli:
        def conversations_history(self, **kw):
            return {
                "messages": [
                    {"ts": "2.0", "user": "U1", "text": "b"},
                    {"ts": "1.0", "user": "U2", "text": "a"},
                ]
            }

        def conversations_replies(self, **kw):
            return {"messages": []}

    w = ContextRetriever(Cli(), window_before=5).build(
        conversation_id="C1", source_message={"ts": "3.0", "user": "U1", "text": "src"}
    )
    texts = [m["text"] for m in w.flat_messages()]
    assert texts == ["a", "b", "src"]


# =============================================================================
# Orchestrator edges
# =============================================================================


def test_orchestrator_persist_context_snapshot_creates_row(session):
    from app.config import Settings
    from app.context.retriever import ContextWindow
    from app.orchestrator import Orchestrator

    orch = Orchestrator(Settings())
    snap = orch.persist_context_snapshot(
        session,
        ContextWindow(
            conversation_id="C1",
            source_ts="1.0",
            thread_ts=None,
            source_message={"ts": "1.0", "text": "x", "user": "U1"},
        ).to_snapshot_dict(),
    )
    assert snap.id is not None
    session.flush()
    assert session.get(ContextSnapshot, snap.id) is not None


def test_orchestrator_decide_passive_with_no_draft_id_medium_returns_none_draft():
    from app.config import Settings
    from app.orchestrator import Orchestrator
    from app.schemas.intent import IntentClassification, IntentType, TaskDraft

    s = Settings()
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.5, task=TaskDraft(title="x")
    )
    d = Orchestrator(s).decide_passive(classification=c, draft_id=None)
    # medium bucket but no draft id provided → soft_prompt with None draft_id
    assert d.action == "soft_prompt"
    assert d.draft_id is None


# =============================================================================
# Handlers — edit flow extra coverage
# =============================================================================


def test_edit_handler_with_unknown_draft_is_noop(
    patched_session_scope, SessionFactory, slack_client
):
    from app.slack_bot.handlers.actions import handle_edit

    handle_edit(
        body={
            "actions": [{"value": "999999"}],
            "trigger_id": "t",
            "message": {"metadata": {}},
        },
        client=slack_client,
        ack=lambda *a, **kw: None,
    )
    assert slack_client.views_opened == []


def test_ignore_handler_is_idempotent(patched_session_scope, SessionFactory, ack):
    from app.slack_bot.handlers.actions import handle_ignore
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "x"})
        s.commit()
        did = d.id

    handle_ignore(body={"actions": [{"value": str(did)}]}, ack=ack)
    handle_ignore(body={"actions": [{"value": str(did)}]}, ack=ack)

    with SessionFactory() as s:
        assert s.get(ActionDraft, did).state == ActionDraftState.ignored


def test_ignore_handler_missing_value_is_noop(patched_session_scope, ack):
    from app.slack_bot.handlers.actions import handle_ignore

    handle_ignore(body={"actions": [{}]}, ack=ack)  # no value
    assert ack.called


def test_confirm_handler_missing_draft_value_is_noop(
    patched_session_scope, slack_client, sender, finalizer_stub, ack
):
    from app.slack_bot.handlers.actions import handle_confirm

    handle_confirm(
        body={"actions": [{}], "channel": {"id": "C1"}, "message": {"metadata": {}}},
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert finalizer_stub.calls == []


# =============================================================================
# Google credential store (DB roundtrip)
# =============================================================================


def test_google_credential_store_roundtrip(session, monkeypatch):
    from cryptography.fernet import Fernet

    from app.sync.google_auth import GoogleCredentialStore, TokenCipher

    key = Fernet.generate_key().decode("ascii")
    cipher = TokenCipher(key=key)
    store = GoogleCredentialStore(cipher)

    rec = store.save(
        session,
        user_key="_service_account",
        access_token="at",
        refresh_token="rt",
        scopes=["s1", "s2"],
        expires_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    session.flush()

    loaded = store.load(session, user_key="_service_account")
    assert loaded is not None
    assert store.decrypt_access_token(loaded) == "at"
    assert store.decrypt_refresh_token(loaded) == "rt"
    assert "s1" in loaded.scopes


def test_google_credential_store_returns_none_on_miss(session):
    from cryptography.fernet import Fernet

    from app.sync.google_auth import GoogleCredentialStore, TokenCipher

    store = GoogleCredentialStore(TokenCipher(key=Fernet.generate_key().decode("ascii")))
    assert store.load(session, user_key="nope") is None


def test_google_credential_store_upserts_over_existing(session):
    from cryptography.fernet import Fernet

    from app.sync.google_auth import GoogleCredentialStore, TokenCipher

    cipher = TokenCipher(key=Fernet.generate_key().decode("ascii"))
    store = GoogleCredentialStore(cipher)

    store.save(
        session,
        user_key="u1",
        access_token="a1",
        refresh_token="r1",
        scopes=["x"],
        expires_at=None,
    )
    session.flush()
    store.save(
        session,
        user_key="u1",
        access_token="a2",
        refresh_token="r2",
        scopes=["y"],
        expires_at=None,
    )
    session.flush()
    assert session.query(OAuthCredential).count() == 1
    loaded = store.load(session, user_key="u1")
    assert store.decrypt_access_token(loaded) == "a2"


# =============================================================================
# Finalize: Sheets sync runs after DB persist (observable via factory call)
# =============================================================================


def test_finalize_invokes_sheets_factory_when_configured(
    patched_session_scope, SessionFactory
):
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from tests.requirements.test_fr_11_12_persistence import _prep

    calls = {"sheets": 0, "tasks": 0}

    class SheetsFake:
        def sync(self, session, task):
            calls["sheets"] += 1

    class TasksFake:
        def sync(self, session, task):
            calls["tasks"] += 1

    with SessionFactory() as s:
        draft, snap = _prep(s)
        s.commit()
        draft_id, snap_id = draft.id, snap.id

    FinalizeService(
        settings=Settings(),
        sheets_service_factory=lambda: SheetsFake(),
        google_tasks_service_factory=lambda: TasksFake(),
    ).finalize_draft(
        draft_id=draft_id,
        source_metadata={
            "conversation_id": "C1",
            "message_ts": "1.0",
            "thread_ts": None,
            "permalink": "p",
            "context_snapshot_id": snap_id,
        },
    )
    assert calls["sheets"] == 1
    assert calls["tasks"] == 1


# =============================================================================
# Success / failure feedback posting
# =============================================================================


def test_confirm_invokes_finalizer_and_does_not_post_channel_feedback(
    patched_session_scope, SessionFactory, slack_client, sender, finalizer_stub, ack
):
    """Since the widget now morphs in place, the Confirm handler no longer
    posts a separate success message in the channel. The finalizer handles
    the UX via chat.update + owner DM (tested elsewhere)."""
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    handle_confirm(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"metadata": {"event_payload": {"metadata": "{}"}}},
        },
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert finalizer_stub.calls, "finalizer must be invoked"
    # No separate 'created' message in the channel — finalizer drives UX.
    assert all(
        "created" not in m.get("text", "").lower() for m in sender.posted
    )


def test_confirm_posts_failure_feedback_when_finalize_raises(
    patched_session_scope, SessionFactory, slack_client, sender, finalizer_stub, ack
):
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    finalizer_stub.raise_error = RuntimeError("db down")
    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    handle_confirm(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"metadata": {"event_payload": {"metadata": "{}"}}},
        },
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert len(sender.posted) == 1
    assert "failed" in sender.posted[0]["text"].lower()


# =============================================================================
# Modal submit — state value extraction edge cases
# =============================================================================


def test_modal_state_extractor_handles_various_element_types():
    from app.slack_bot.handlers.views import _state_value

    values = {
        "B1": {"A1": {"value": "plain"}},
        "B2": {"A2": {"selected_option": {"value": "opt"}}},
        "B3": {"A3": {"selected_date": "2026-06-01"}},
        "B4": {"A4": {"selected_date_time": 1800000000}},
        "B5": {"A5": {}},
    }
    assert _state_value(values, "B1", "A1") == "plain"
    assert _state_value(values, "B2", "A2") == "opt"
    assert _state_value(values, "B3", "A3") == "2026-06-01"
    assert _state_value(values, "B4", "A4") == 1800000000
    assert _state_value(values, "B5", "A5") is None
    assert _state_value(values, "missing", "X") is None


# =============================================================================
# Entrypoint scripts compile (guards CI)
# =============================================================================


@pytest.mark.parametrize(
    "module_path",
    [
        "ops/bootstrap_google_oauth.py",
        "ops/generate_fernet_key.py",
        "ops/entrypoint_with_health.py",
    ],
)
def test_ops_scripts_compile(module_path):
    import py_compile

    py_compile.compile(module_path, doraise=True)
