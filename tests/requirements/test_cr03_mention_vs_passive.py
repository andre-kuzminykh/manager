"""CR-03 split between the two auto-create flows:

- @mention  → task created immediately, NO admin review, the user sees a
  task-card and a thread follow-up question for missing fields. Replies
  update the Task and refresh the card in place.
- Passive   → task created immediately + admin review (DM + ephemeral) for
  human oversight. No user-facing draft widget.
"""
from __future__ import annotations

from app.models import (
    ActionDraft,
    ActionDraftState,
    AuditLog,
    Task,
    TaskStatus,
)


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}

    def post_ephemeral(self, **kw):
        self.ephemerals.append(kw)
        return {"ok": True}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}


# --------------------------------------------------------------------------- #
# @mention flow: auto-create + NO admin review
# --------------------------------------------------------------------------- #


def test_mention_creates_task_without_admin_review(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
    monkeypatch,
):
    """Even when admins are configured, @mention must NOT trigger the
    admin-review DM / ephemeral — that's reserved for passive flow."""
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin-1,U-admin-2")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    from app.slack_bot.handlers.events import handle_app_mention

    sender = _Sender()
    handle_app_mention(
        event={
            "ts": "1.0",
            "user": "U-author",
            "text": "<@UBOT> надо собрать pitch-deck",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mention-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        # Task auto-created.
        assert s.query(Task).count() == 1
        # NO admin_review audit for the mention path.
        assert (
            s.query(AuditLog)
            .filter(AuditLog.category == "admin_review")
            .count()
            == 0
        )

    # No ephemeral messages = no admin thread comment.
    assert sender.ephemerals == []
    # No DM to either admin — task-card/DM only go to the owner.
    admin_channels = {"U-admin-1", "U-admin-2"}
    assert not any(m["channel"] in admin_channels for m in sender.posts)

    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_mention_posts_task_card_not_draft_widget(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_app_mention

    sender = _Sender()
    handle_app_mention(
        event={
            "ts": "2.0",
            "user": "U-author",
            "text": "<@UBOT> надо сделать X",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mention-2"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    first = sender.posts[0]
    assert first["channel"] == "C1"
    # Task-card starts with *#N* in a plain section, no "Task draft" header.
    first_block = first["blocks"][0]
    assert first_block["type"] == "section"
    assert first_block["text"]["text"].startswith("*#")


def test_mention_follow_up_reply_updates_task_and_refreshes_card(
    patched_session_scope,
    SessionFactory,
    bolt_context,
    slack_client,
    ack,
):
    """Reply in the same thread answering the bot's follow-up must update
    the Task row (not a draft payload) and chat.update the task card in
    place."""
    from app.slack_bot.handlers.events import handle_app_mention, handle_message
    from tests.requirements.conftest import StubClassifier, _make_services
    from app.schemas.intent import IntentClassification, IntentType, TaskDraft

    # Classifier returns a task with no due_date so the bot will ask.
    stub = StubClassifier(
        IntentClassification(
            intent=IntentType.create_task,
            confidence=0.9,
            task=TaskDraft(title="собрать демо"),
        )
    )
    services = _make_services(slack_client, stub)

    sender = _Sender()
    handle_app_mention(
        event={
            "ts": "10.0",
            "user": "U-author",
            "text": "<@UBOT> надо собрать демо",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mention-3"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    # Locate the task that was created.
    with SessionFactory() as s:
        task = s.query(Task).one()
        draft = s.query(ActionDraft).one()
        assert draft.task_id == task.id
        assert draft.awaiting_field == "due_date"
        assert task.due_date is None
        tid = task.id

    # Now the user answers in thread with an ISO date.
    handle_message(
        event={
            "ts": "11.0",
            "thread_ts": "10.0",
            "user": "U-author",
            "text": "2026-05-12",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "mention-3-reply"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        task = s.get(Task, tid)
        assert task.due_date.isoformat() == "2026-05-12"
    # chat.update was called with the refreshed card.
    assert sender.updates, "task card should be refreshed via chat.update"


# --------------------------------------------------------------------------- #
# Passive flow still triggers admin review
# --------------------------------------------------------------------------- #


def test_passive_never_auto_creates_only_offers(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
    monkeypatch,
):
    """Per product decision 2026-04-24: passive path NEVER auto-creates a
    task. It posts a soft-prompt in the thread inviting the user to
    confirm. @mention is the only path that auto-creates."""
    monkeypatch.setenv("ADMIN_SLACK_USER_IDS", "U-admin-1")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    from app.slack_bot.handlers.events import handle_message

    sender = _Sender()
    handle_message(
        event={
            "ts": "5.0",
            "user": "U-author",
            "text": "надо задачу на завтра",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "passive-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        # No task auto-created.
        assert s.query(Task).count() == 0
        # No admin review.
        assert (
            s.query(AuditLog)
            .filter(AuditLog.category == "admin_review")
            .count()
            == 0
        )
    # Admin got NO DM.
    assert not any(m["channel"] == "U-admin-1" for m in sender.posts)
    # User saw a soft-prompt posted in the source thread.
    assert any(
        m["channel"] == "C1" and m["thread_ts"] == "5.0" for m in sender.posts
    )

    get_settings.cache_clear()  # type: ignore[attr-defined]
