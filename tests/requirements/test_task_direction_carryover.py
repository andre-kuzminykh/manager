"""FR-CR-05-206 — a strategic `direction` classified at draft creation
(FR-CR-05-200) must survive into the materialised Task's `extra`, so the
strategic digest / sheet export can filter Slack/Telegram tasks by direction
without re-classifying. Meeting tasks already carry it (FR-CR-05-163); this
closes the draft → Task path.
"""
from __future__ import annotations


def _make_draft(session, *, payload):
    from app.models import ActionDraft, ContextSnapshot, IntentInference
    from app.models.intent import ActionDraftState
    from app.models.intent import IntentType as IE

    snap = ContextSnapshot(
        conversation_id="C1",
        source_ts="42",
        source_message={"ts": "42", "text": "do x", "user": "99"},
        history_before=[],
        thread_messages=[],
    )
    session.add(snap)
    session.flush()
    inf = IntentInference(
        context_snapshot_id=snap.id,
        intent=IE.create_task,
        confidence=0.9,
        invocation_type="passive",
    )
    session.add(inf)
    session.flush()
    draft = ActionDraft(
        inference_id=inf.id,
        intent=IE.create_task,
        state=ActionDraftState.proposed,
        payload=payload,
        slack_message_ts="42",
    )
    session.add(draft)
    session.flush()
    return draft, snap


def test_fr_cr_05_206_direction_carries_into_task_extra(session):
    from app.persistence import create_task_from_draft

    draft, snap = _make_draft(
        session, payload={"title": "Подготовить материалы для инвесторов", "direction": "investors"}
    )
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"kind": "slack", "conversation_id": "C1", "message_ts": "42"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="99",
    )
    assert (t.extra or {}).get("direction") == "investors"


def test_fr_cr_05_206_no_direction_leaves_extra_unset(session):
    """Draft without a direction → no `direction` key (export backfills it)."""
    from app.persistence import create_task_from_draft

    draft, snap = _make_draft(session, payload={"title": "do x"})
    t = create_task_from_draft(
        session,
        draft=draft,
        source={"kind": "slack", "conversation_id": "C1", "message_ts": "42"},
        context_snapshot_id=snap.id,
        fallback_author_slack_id="99",
    )
    assert "direction" not in (t.extra or {})


# --- FR-CR-05-206 — classify direction at finalize for NEW unclassified tasks ---


class _StubBackend:
    """Minimal llm backend: returns a fixed classify JSON for complete_text."""

    def __init__(self, direction: str) -> None:
        self._direction = direction

    def complete_text(self, *, system_prompt, user_prompt, model, temperature):
        return '{"items": [{"task_id": 0, "direction": "%s"}]}' % self._direction


def test_fr_cr_05_206_ensure_direction_sets_when_missing():
    from app.services.task_direction import ensure_direction

    payload = {"title": "Prep deck for investors", "description": ""}
    changed = ensure_direction(payload, llm_backend=_StubBackend("investors"), model="gpt-4o-mini")
    assert changed is True
    assert payload["direction"] == "investors"


def test_fr_cr_05_206_ensure_direction_noop_when_present():
    from app.services.task_direction import ensure_direction

    payload = {"title": "x", "direction": "budget"}
    changed = ensure_direction(payload, llm_backend=_StubBackend("investors"), model="gpt-4o-mini")
    assert changed is False
    assert payload["direction"] == "budget"


def test_fr_cr_05_206_ensure_direction_noop_without_title():
    from app.services.task_direction import ensure_direction

    payload = {"description": "no title here"}
    changed = ensure_direction(payload, llm_backend=_StubBackend("investors"), model="gpt-4o-mini")
    assert changed is False
    assert "direction" not in payload
