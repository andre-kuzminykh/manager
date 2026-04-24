"""Passive path UX (product decision 2026-04-24):

- Passive never auto-creates.
- Instead it posts a PRE-FILLED draft card (title / owner / priority /
  due_date) with three buttons: Accept, Edit, Reject.
- Accept → task created. If anything is missing, the bot asks a
  follow-up in the thread.
- Reject → draft ignored, widget removed.
- Edit → opens the task modal prefilled.
"""
from __future__ import annotations

from app.models import ActionDraft, ActionDraftState, Task
from app.slack_bot import blocks as bk


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


def test_passive_posts_prefilled_draft_card_with_three_buttons(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_message

    sender = _Sender()
    handle_message(
        event={
            "ts": "100.0",
            "user": "U-author",
            "text": "надо подготовить питчдек",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "passive-prefill-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    assert len(sender.posts) == 1
    posted = sender.posts[0]
    assert posted["channel"] == "C1"
    assert posted["thread_ts"] == "100.0"
    # Collect every button action_id present on the card.
    ids = [
        el["action_id"]
        for b in posted["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_CONFIRM in ids
    assert bk.ACTION_EDIT in ids
    assert bk.ACTION_IGNORE in ids
    # Button labels are Accept / Edit / Reject.
    labels = {
        el["action_id"]: el["text"]["text"]
        for b in posted["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
    }
    assert labels[bk.ACTION_CONFIRM] == "Accept"
    assert labels[bk.ACTION_EDIT] == "Edit"
    assert labels[bk.ACTION_IGNORE] == "Reject"
    # The card is pre-filled — a Title section with the task title is
    # present (draft_card renders one section with the "Title" field).
    rendered = str(posted["blocks"])
    assert "Prepare report" in rendered or "Title" in rendered


def test_passive_draft_card_widget_ts_is_saved_for_morph(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_message

    sender = _Sender()
    handle_message(
        event={
            "ts": "200.0",
            "user": "U-author",
            "text": "надо сделать X",
            "channel": "C-pass",
            "channel_type": "channel",
        },
        body={"event_id": "passive-morph-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        # The widget coordinates were stored so finalize_draft can morph
        # the same message into a task card on Accept.
        assert draft.card_channel == "C-pass"
        assert draft.card_ts == "1.0"


def test_accept_on_draft_card_creates_task_and_asks_follow_up(
    patched_session_scope,
    services_task,
    bolt_context,
    slack_client,
    SessionFactory,
    ack,
    monkeypatch,
):
    """The stub task classifier in services_task returns a TaskDraft with
    a title but no due_date. After Accept, the bot should create the
    task and ask a follow-up in the thread."""
    from app.config import Settings
    from app.orchestrator.finalize import FinalizeService
    from app.slack_bot.handlers.actions import handle_confirm
    from app.slack_bot.handlers.events import handle_message

    sender = _Sender()
    handle_message(
        event={
            "ts": "500.0",
            "user": "U-author",
            "text": "надо сделать отчёт",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "passive-accept-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        tid_before = draft.task_id
        draft_id = draft.id
    assert tid_before is None  # passive didn't auto-create

    # Simulate the user clicking Accept on the posted draft card.
    handle_confirm(
        body={
            "actions": [{"value": str(draft_id)}],
            "channel": {"id": "C1"},
            "message": {
                "ts": "1.0",
                "metadata": {
                    "event_type": "draft",
                    "event_payload": {
                        "metadata": sender.posts[0]["metadata"]["event_payload"][
                            "metadata"
                        ]
                    },
                },
            },
            "user": {"id": "U-author"},
        },
        client=slack_client,
        services=services_task,
        finalizer=FinalizeService(settings=Settings(), sender=sender),
        sender=sender,
        ack=ack,
    )

    with SessionFactory() as s:
        task = s.query(Task).one()
        draft = s.get(ActionDraft, draft_id)
        assert draft.state == ActionDraftState.confirmed
        assert draft.task_id == task.id
    # A follow-up question appeared in the same thread (passive parity
    # with @mention).
    follow_ups = [p for p in sender.posts if p["channel"] == "C1" and p.get("text", "").startswith(":memo:")]
    assert follow_ups, "Accept must ask a follow-up when fields are missing"
