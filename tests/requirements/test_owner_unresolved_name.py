"""Owner resolution edge-cases (regression for the 'Семен → автор' bug)."""
from __future__ import annotations

from app.config import Settings
from app.models import (
    ActionDraft,
    ActionDraftState,
    ContextSnapshot,
    IntentInference,
    Task,
)
from app.models.intent import IntentType as IE
from app.persistence import create_task_from_draft
from app.services.followup import (
    llm_extract_reply_fields,
    pick_next_missing,
    prompt_for,
)


def _draft(session, payload):
    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="1.0",
        source_message={"ts": "1.0", "text": "x", "user": "U1"},
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
    )
    session.add(d)
    session.flush()
    return d


def test_owner_unresolved_name_does_not_fall_back_to_author(session):
    """User wrote 'Семен' but Семен not in ALLOWED_OWNERS → owner_user_id
    must stay empty so the bot keeps asking, NOT silently default to the
    source author."""
    d = _draft(session, payload={"title": "x", "owner_display_name": "Семен"})
    t = create_task_from_draft(
        session,
        draft=d,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U-author",
    )
    assert t.owner_user_id is None  # NOT the author
    assert t.owner_display_name == "Семен"


def test_owner_falls_back_to_author_when_no_name_at_all(session):
    """If the user never mentioned an owner, fallback to source author is OK."""
    d = _draft(session, payload={"title": "x"})
    t = create_task_from_draft(
        session,
        draft=d,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U-author",
    )
    assert t.owner_user_id == "U-author"


def test_owner_resolved_user_id_is_persisted(session):
    d = _draft(
        session,
        payload={
            "title": "x",
            "owner_user_id": "U-pasha",
            "owner_display_name": "Паша",
        },
    )
    t = create_task_from_draft(
        session,
        draft=d,
        source={},
        context_snapshot_id=None,
        fallback_author_slack_id="U-author",
    )
    assert t.owner_user_id == "U-pasha"
    assert t.owner_display_name == "Паша"


def test_pick_next_missing_keeps_asking_owner_when_only_name_present():
    payload = {"title": "x", "due_date": "2026-05-01", "owner_display_name": "Семен"}
    assert pick_next_missing("create_task", payload) == "owner"


def test_prompt_for_owner_mentions_unresolved_name_and_lists_allowed():
    text = prompt_for(
        "owner",
        payload={"owner_display_name": "Семен"},
        allowed_owners=[
            {"slack_user_id": "U1", "display_name": "Иван"},
            {"slack_user_id": "U2", "display_name": "Анна"},
        ],
    )
    assert "Семен" in text
    assert "Иван" in text
    assert "Анна" in text


def test_prompt_for_owner_default_when_no_unresolved_name():
    text = prompt_for("owner")
    assert "Кому назначаем" in text


class _StubBackend:
    def __init__(self, payload):
        self._payload = payload

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        return self._payload


def test_llm_extract_keeps_display_name_for_unresolved_owner():
    backend = _StubBackend(
        payload={"owner_user_id": "U-bogus", "owner_display_name": "Семен"}
    )
    out = llm_extract_reply_fields(
        backend=backend,
        reply_text="на семена",
        awaiting_field="owner",
        allowed_owners=[{"slack_user_id": "U1", "display_name": "Иван"}],
    )
    assert "owner_user_id" not in out
    assert out["owner_display_name"] == "Семен"


def test_llm_extract_resolves_owner_via_local_matcher_when_only_name():
    backend = _StubBackend(payload={"owner_display_name": "Иван"})
    out = llm_extract_reply_fields(
        backend=backend,
        reply_text="Иван",
        awaiting_field="owner",
        allowed_owners=[{"slack_user_id": "U-ivan", "display_name": "Иван"}],
    )
    assert out["owner_user_id"] == "U-ivan"
