"""Multi-field thread replies like 'на пашу до завтра' must land both owner
AND due_date in one go. Implemented via LLM extractor with deterministic
per-field fallback."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.config import Settings
from app.models import ActionDraft, ActionDraftState, ContextSnapshot, IntentInference
from app.models.intent import IntentType as IE
from app.services.followup import (
    REPLY_TOOL_NAME,
    _build_reply_user_prompt,
    llm_extract_reply_fields,
)


# --------------------------------------------------------------------------- #
# Backend generic call_tool helper
# --------------------------------------------------------------------------- #


def test_call_tool_refactor_preserves_intent_extract_behaviour():
    """extract_intent should delegate to call_tool with the intent schema."""
    from app.intent.llm_backends import AnthropicBackend, INTENT_TOOL_NAME

    class _Stub:
        def __init__(inner):
            inner.calls = []

            class M:
                def __init__(outer, parent):
                    outer._parent = parent

                def create(outer, **kw):
                    outer._parent.calls.append(kw)
                    return SimpleNamespace(
                        content=[
                            SimpleNamespace(
                                type="tool_use",
                                input={"intent": "no_action", "confidence": 0.0},
                            )
                        ]
                    )

            inner.messages = M(inner)

    stub = _Stub()
    AnthropicBackend(stub, "claude-x").extract_intent(user_prompt="hello")
    assert stub.calls[0]["tools"][0]["name"] == INTENT_TOOL_NAME


# --------------------------------------------------------------------------- #
# llm_extract_reply_fields direct
# --------------------------------------------------------------------------- #


class _StubBackend:
    """call_tool returns a fixed dict for testing the extractor wiring."""

    def __init__(self, payload: dict | None, raise_exc: Exception | None = None):
        self._payload = payload
        self._raise = raise_exc
        self.last_call: dict | None = None

    def extract_intent(self, *, user_prompt):  # pragma: no cover — unused
        raise NotImplementedError

    def call_tool(self, **kw):
        self.last_call = kw
        if self._raise is not None:
            raise self._raise
        return self._payload


def test_llm_extract_returns_parsed_fields():
    backend = _StubBackend(
        payload={
            "owner_user_id": "U-pasha",
            "owner_display_name": "Паша",
            "due_date": "2026-04-24",
        }
    )
    out = llm_extract_reply_fields(
        backend=backend,
        reply_text="на пашу до завтра",
        awaiting_field="due_date",
        allowed_owners=[{"slack_user_id": "U-pasha", "display_name": "Паша"}],
        today=date(2026, 4, 23),
    )
    assert out["owner_user_id"] == "U-pasha"
    assert out["owner_display_name"] == "Паша"
    assert out["due_date"] == "2026-04-24"


def test_llm_extract_drops_unrecognised_owner_id_keeps_display_name():
    """We strip the bogus user id but KEEP the name so the bot can re-ask
    the user with a 'не нашёл "Evil" в списке' hint."""
    backend = _StubBackend(
        payload={"owner_user_id": "U-evil", "owner_display_name": "Evil"}
    )
    out = llm_extract_reply_fields(
        backend=backend,
        reply_text="evil should own this",
        awaiting_field="owner",
        allowed_owners=[{"slack_user_id": "U-alice", "display_name": "Alice"}],
    )
    assert "owner_user_id" not in out
    assert out["owner_display_name"] == "Evil"


def test_llm_extract_resolves_display_name_via_local_matcher():
    """If LLM returned only display_name, fall back to resolve_owner_hint
    so plain 'Alice' against the allowed list still lands a slack id."""
    backend = _StubBackend(payload={"owner_display_name": "Alice"})
    out = llm_extract_reply_fields(
        backend=backend,
        reply_text="Alice",
        awaiting_field="owner",
        allowed_owners=[{"slack_user_id": "U-alice", "display_name": "Alice"}],
    )
    assert out["owner_user_id"] == "U-alice"
    assert out["owner_display_name"] == "Alice"


def test_llm_extract_strips_empty_values():
    backend = _StubBackend(
        payload={"title": "", "due_date": None, "description": "real"}
    )
    out = llm_extract_reply_fields(
        backend=backend,
        reply_text="ok",
        awaiting_field="due_date",
        allowed_owners=[],
    )
    assert out == {"description": "real"}


def test_llm_extract_none_backend_returns_empty():
    assert (
        llm_extract_reply_fields(
            backend=None,
            reply_text="x",
            awaiting_field="due_date",
            allowed_owners=[],
        )
        == {}
    )


def test_llm_extract_empty_text_returns_empty():
    backend = _StubBackend(payload={"due_date": "2026-01-01"})
    assert (
        llm_extract_reply_fields(
            backend=backend,
            reply_text="   ",
            awaiting_field="due_date",
            allowed_owners=[],
        )
        == {}
    )


def test_llm_extract_swallows_backend_exceptions():
    backend = _StubBackend(payload=None, raise_exc=RuntimeError("boom"))
    out = llm_extract_reply_fields(
        backend=backend,
        reply_text="на пашу до завтра",
        awaiting_field="due_date",
        allowed_owners=[],
    )
    assert out == {}


def test_llm_extract_prompt_includes_allowed_list_and_current_date():
    backend = _StubBackend(payload={"due_date": "2026-04-25"})
    llm_extract_reply_fields(
        backend=backend,
        reply_text="до пятницы",
        awaiting_field="due_date",
        allowed_owners=[{"slack_user_id": "U1", "display_name": "Ivan"}],
        today=date(2026, 4, 23),
    )
    user_prompt = backend.last_call["user_prompt"]
    assert "current_date: 2026-04-23" in user_prompt
    assert "awaiting_field: due_date" in user_prompt
    assert "Ivan (U1)" in user_prompt
    assert backend.last_call["tool_name"] == REPLY_TOOL_NAME


# --------------------------------------------------------------------------- #
# End-to-end follow-up reply with multi-field LLM extraction
# --------------------------------------------------------------------------- #


def _seed_draft(session, payload, *, awaiting="due_date", thread_ts="10.0"):
    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts=thread_ts,
        source_message={"ts": thread_ts, "text": "x", "user": "U1"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="mention",
    )
    session.add(inf)
    session.flush()
    d = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload=payload,
        slack_message_ts=thread_ts,
        card_channel="C1",
        card_ts="9.9",
        awaiting_field=awaiting,
    )
    session.add(d)
    session.flush()
    return d


class _UpdateCli:
    def __init__(self):
        self.updated = []
        self.posted = []

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return SimpleNamespace(data={"ok": True, "ts": "0"})

    def chat_update(self, **kw):
        self.updated.append(kw)
        return SimpleNamespace(data={"ok": True})


def test_multi_field_reply_fills_owner_and_due_via_llm(
    patched_session_scope,
    SessionFactory,
    slack_client,
    bolt_context,
    ack,
    monkeypatch,
):
    """'на пашу до завтра' answered to 'Какой дедлайн?' should land BOTH
    owner and due_date and clear awaiting_field (everything is filled)."""
    from app.slack_bot.handlers.events import handle_message
    from app.slack_bot.rate_limiter import RateAwareSlackSender
    from tests.requirements.conftest import StubClassifier, _make_services

    # allowed owners
    monkeypatch.setenv(
        "ALLOWED_OWNERS", '[{"slack_user_id":"U-pasha","display_name":"Паша"}]'
    )
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    # seed draft
    with SessionFactory() as s:
        d = _seed_draft(s, payload={"title": "t"})
        s.commit()
        did = d.id

    # classifier with a stub backend returning the multi-field dict
    stub_classifier = StubClassifier(auto=True)
    stub_classifier._backend = _StubBackend(  # type: ignore[attr-defined]
        payload={
            "owner_user_id": "U-pasha",
            "owner_display_name": "Паша",
            "due_date": "2026-04-24",
        }
    )

    # patch IntentClassifier.backend property for this instance
    class _Pass(StubClassifier):
        @property
        def backend(self):
            return _StubBackend(
                payload={
                    "owner_user_id": "U-pasha",
                    "owner_display_name": "Паша",
                    "due_date": "2026-04-24",
                }
            )

    services = _make_services(slack_client, _Pass(auto=True))

    cli = _UpdateCli()
    sender = RateAwareSlackSender(cli, min_interval_seconds=0.0)

    handle_message(
        event={
            "ts": "11.0",
            "thread_ts": "10.0",
            "user": "U-author",
            "text": "на пашу до завтра",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "multi-1"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )

    with SessionFactory() as s:
        d = s.get(ActionDraft, did)
        assert d.payload["owner_user_id"] == "U-pasha"
        assert d.payload["due_date"] == "2026-04-24"
        assert d.awaiting_field is None  # all required fields filled
    assert cli.updated, "card should be chat.update'd"
    # And a summary text was posted in the thread (ready to confirm).
    assert any(
        "Все поля собрал" in m.get("text", "") for m in cli.posted
    )

    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_fallback_parse_reply_still_works_when_backend_absent(
    patched_session_scope, SessionFactory, slack_client, bolt_context, ack
):
    """Without a backend (local dev / no LLM), the deterministic
    parse_reply path must still fill the awaited field."""
    from app.slack_bot.handlers.events import handle_message
    from app.slack_bot.rate_limiter import RateAwareSlackSender
    from tests.requirements.conftest import StubClassifier, _make_services

    with SessionFactory() as s:
        d = _seed_draft(s, payload={"title": "t"})
        s.commit()
        did = d.id

    # no backend on the stub (default None)
    services = _make_services(slack_client, StubClassifier(auto=True))

    cli = _UpdateCli()
    sender = RateAwareSlackSender(cli, min_interval_seconds=0.0)

    handle_message(
        event={
            "ts": "12.0",
            "thread_ts": "10.0",
            "user": "U-author",
            "text": "2026-04-24",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "fallback-1"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )

    with SessionFactory() as s:
        d = s.get(ActionDraft, did)
        assert d.payload["due_date"] == "2026-04-24"
        # owner is still missing → next question is 'owner'
        assert d.awaiting_field == "owner"


def test_build_reply_user_prompt_shape():
    prompt = _build_reply_user_prompt(
        reply_text="на пашу до завтра",
        awaiting_field="due_date",
        allowed_owners=[{"slack_user_id": "U1", "display_name": "Ivan"}],
        today=date(2026, 4, 23),
    )
    assert "user_reply:\nна пашу до завтра" in prompt
    assert "allowed_owners:" in prompt
    assert "Ivan (U1)" in prompt
    assert "awaiting_field: due_date" in prompt


def test_build_reply_user_prompt_empty_owners():
    prompt = _build_reply_user_prompt(
        reply_text="foo",
        awaiting_field=None,
        allowed_owners=[],
        today=date(2026, 4, 23),
    )
    assert "(empty)" in prompt
    assert "awaiting_field: any" in prompt
