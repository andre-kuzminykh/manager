"""Requirement coverage: FR-CR-04-6 (offer-first passive UX with
Accept / Edit / Reject + immediate follow-up).

Passive path UX (product decision 2026-04-24):

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
        self.deletes: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def delete_message(self, **kw):
        self.deletes.append(kw)
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
    # Two posts: the draft card + a follow-up question in the thread
    # asking for the missing field (services_task stub leaves due_date
    # null).
    assert len(sender.posts) == 2
    posted = sender.posts[0]
    assert posted["channel"] == "C1"
    assert posted["thread_ts"] == "100.0"
    follow_up = sender.posts[1]
    assert follow_up["channel"] == "C1"
    assert follow_up["thread_ts"] == "100.0"
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


def test_passive_immediately_asks_for_missing_field_in_thread(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """Right after the draft card is posted, the bot posts a follow-up
    question in the same thread asking for the missing field (so the
    user can answer in chat without opening Edit)."""
    from app.slack_bot.handlers.events import handle_message

    sender = _Sender()
    handle_message(
        event={
            "ts": "300.0",
            "user": "U-author",
            "text": "надо сделать отчёт",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "passive-ask-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    # Second post is the follow-up question in the same thread.
    assert len(sender.posts) == 2
    follow = sender.posts[1]
    assert follow["thread_ts"] == "300.0"
    assert follow["channel"] == "C1"
    # The draft row remembers which field it is awaiting.
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        assert draft.awaiting_field in {"owner", "due_date", "description", "effort"}


def test_passive_thread_reply_fills_field_and_updates_draft(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
    monkeypatch,
):
    monkeypatch.setenv(
        "ALLOWED_OWNERS",
        '[{"slack_user_id":"UIVAN0001","display_name":"Иван"}]',
    )
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    """User answers the follow-up in thread → _handle_followup_reply
    updates draft.payload and refreshes the card in place. When all
    user-visible fields are filled, the "push Accept" ack is posted."""
    from app.slack_bot.handlers.events import handle_message

    sender = _Sender()
    handle_message(
        event={
            "ts": "400.0",
            "user": "U-author",
            "text": "надо сделать отчёт",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "passive-reply-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        awaiting = draft.awaiting_field
    # The stub classifier returns a TaskDraft with owner_display_name
    # "@alice" — we no longer override that with the author, so the
    # bot asks the user about the owner first.
    assert awaiting == "owner"
    assert draft.payload.get("owner_user_id") is None

    # The user replies in the thread with the same Slack mention from
    # the allowed list — resolve_owner_hint promotes it to a real id.
    handle_message(
        event={
            "ts": "401.0",
            "thread_ts": "400.0",
            "user": "U-author",
            "text": "<@UIVAN0001>",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "passive-reply-2"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        assert draft.payload.get("owner_user_id") == "UIVAN0001"
    # The widget was refreshed via chat.update (follow-up reply path).
    assert any(u.get("ts") for u in sender.updates)


def test_thread_reply_deletes_prior_followup_question(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
    monkeypatch,
):
    """When the user answers the bot's ':memo: Кому назначаем?…' in the
    thread, that question is deleted before the next ack lands, so the
    thread doesn't accumulate a chain of stale bot questions."""
    monkeypatch.setenv(
        "ALLOWED_OWNERS",
        '[{"slack_user_id":"UIVAN0001","display_name":"Иван"}]',
    )
    from app.config import get_settings
    from app.slack_bot.handlers.events import handle_message

    get_settings.cache_clear()  # type: ignore[attr-defined]

    sender = _Sender()
    handle_message(
        event={
            "ts": "600.0",
            "user": "U-author",
            "text": "надо сделать отчёт",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cleanup-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    # posts[0] = draft card; posts[1] = memo (Кому назначаем?).
    # _Sender returns sequential ts "1.0", "2.0", ... — so the memo ts is "2.0".
    assert len(sender.posts) == 2, sender.posts
    memo_ts = "2.0"

    # The stub classifier emits "@alice" as owner_display_name without
    # a Slack id, so the memo asked about the owner. Reply with a
    # mention from the allowed list.
    handle_message(
        event={
            "ts": "601.0",
            "thread_ts": "600.0",
            "user": "U-author",
            "text": "<@UIVAN0001>",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cleanup-2"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    # The memo was deleted before the new ack was posted.
    assert any(d.get("ts") == memo_ts for d in sender.deletes), sender.deletes


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
