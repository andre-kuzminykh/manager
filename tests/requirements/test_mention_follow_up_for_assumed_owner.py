"""Requirement coverage: FR-CR-04-7 (quiet owner_assumed).

Product decision 2026-04-24 (v3): when the owner slot falls back to the
message author (owner_assumed=True), the bot does NOT pester the user
with a follow-up "кому назначаем?". The task-card label
"(implicit)" communicates the implicit assignment, and the
Edit button lets the user reassign. Same rule in both the @mention
and passive paths.
"""
from __future__ import annotations

from datetime import date

from app.models import ActionDraft, Task
from app.schemas.intent import IntentClassification, IntentType, TaskDraft
from app.slack_bot.handlers.events import _task_payload
from app.services.followup import pick_next_missing
from tests.requirements.conftest import StubClassifier, _make_services


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def post_ephemeral(self, **kw):  # pragma: no cover
        return {"ok": True}


# --------------------------------------------------------------------------- #
# Unit-level: _is_empty treats assumed owner as missing
# --------------------------------------------------------------------------- #


def test_pick_next_missing_accepts_assumed_owner_as_filled():
    """owner_assumed no longer forces a follow-up question — a filled
    owner_user_id (even a fallback to the author) is enough."""
    payload_with_assumed_owner = {
        "title": "t",
        "due_date": "2026-05-01",
        "owner_user_id": "U-author",
        "owner_display_name": None,
        "owner_assumed": True,
    }
    assert pick_next_missing("create_task", payload_with_assumed_owner) is None


def test_task_payload_exposes_owner_assumed_flag():
    """The flag still propagates to the payload so the card can render
    '(implicit)' — we just don't re-ask about it."""
    class _StubTask:
        title = "t"
        description = None
        owner_user_id = "U-author"
        owner_display_name = None
        priority = None
        due_date = date(2026, 5, 1)
        extra = {"owner_assumed": True}

    payload = _task_payload(_StubTask())
    assert payload["owner_assumed"] is True


# --------------------------------------------------------------------------- #
# Handler-level: @mention posts a follow-up about the owner
# --------------------------------------------------------------------------- #


def test_mention_does_not_ask_about_owner_when_owner_assumed(
    patched_session_scope,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """The pipeline returns no owner → classify_and_persist falls back
    to the message author (owner_assumed=True) → the @mention handler
    should NOT post a follow-up question about the owner. Only a
    missing date would trigger one."""
    from app.slack_bot.handlers.events import handle_app_mention

    # Both owner and date are filled (owner via fallback in
    # classify_and_persist), so no follow-up is needed.
    stub = StubClassifier(
        IntentClassification(
            intent=IntentType.create_task,
            confidence=0.9,
            task=TaskDraft(title="подготовить заметки", due_date=date(2026, 5, 1)),
        )
    )
    services = _make_services(slack_client, stub)

    sender = _Sender()
    handle_app_mention(
        event={
            "ts": "500.0",
            "user": "U-author",
            "text": "<@UBOT> надо подготовить заметки к 1 мая",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "owner-assumed-1"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    # Task was created with owner_assumed=True.
    with SessionFactory() as s:
        task = s.query(Task).one()
        assert task.owner_user_id == "U-author"
        assert (task.extra or {}).get("owner_assumed") is True
        draft = s.query(ActionDraft).one()
        # No field is awaiting an answer — date is set, owner is the
        # author fallback and we don't re-ask.
        assert draft.awaiting_field is None

    # No follow-up question about the owner.
    thread_posts = [p for p in sender.posts if p.get("thread_ts") == "500.0"]
    assert not any(
        "assignee" in (p.get("text") or "") for p in thread_posts
    ), [p.get("text") for p in thread_posts]
