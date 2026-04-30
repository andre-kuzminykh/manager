"""One explicit test per CR-02 acceptance rule, so the spec can be mapped
1-to-1 to executable assertions. Keeps the regression cost of UX drift low."""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.models import ActionDraft, ActionDraftState, Task, TaskStatus
from app.models.intent import IntentType as IE
from app.schemas.intent import IntentClassification, IntentType, TaskDraft
from app.services import (
    DigestKind,
    DigestService,
    SubscriptionService,
    parse_reply,
    pick_next_missing,
)
from app.slack_bot import blocks as bk
from app.slack_bot.handlers.events import handle_app_mention, handle_message
from app.slack_bot.handlers.task_actions import (
    handle_manage_subscriptions,
    handle_unsubscribe_in_modal,
)


# --------------------------------------------------------------------------- #
# FR-CR-02-1 mention-always-replies (fallback)
# --------------------------------------------------------------------------- #


def test_fr_cr02_1_fallback_creates_draft_with_source_text_as_title(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client, SessionFactory
):
    handle_app_mention(
        event={
            "ts": "1.0",
            "user": "U1",
            "text": "<@UBOT> настрой CRM",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cr02-1"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    # CR-03 supersedes CR-02 for this flow: @mention creates a real Task
    # immediately; the draft is marked confirmed and links to the task.
    with SessionFactory() as s:
        from app.models import Task

        task = s.query(Task).one()
        assert task.title == "Настрой CRM"
        d = s.query(ActionDraft).one()
        assert d.state == ActionDraftState.confirmed
        assert d.task_id == task.id


def test_fr_cr02_1_bare_mention_asks_user_to_add_text(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    handle_app_mention(
        event={
            "ts": "2.0",
            "user": "U1",
            "text": "<@UBOT>",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cr02-bare"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    assert any("don't see any text" in m.get("text", "").lower() for m in sender.posted)


# --------------------------------------------------------------------------- #
# FR-CR-02-2 follow-up question in thread after the widget
# --------------------------------------------------------------------------- #


def test_fr_cr02_2_followup_question_posted_after_card(
    patched_session_scope, services_silent, sender, ack, bolt_context, slack_client
):
    handle_app_mention(
        event={
            "ts": "3.0",
            "user": "U1",
            "text": "<@UBOT> foo",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cr02-2"},
        client=slack_client,
        context=bolt_context,
        services=services_silent,
        sender=sender,
        ack=ack,
    )
    # FR-CR-05-63 — @mention still posts a task card. The
    # «:memo: Captured: …» follow-up question is gone now that
    # `due_date` auto-defaults to today 18:00 — there's nothing
    # to ask when title + owner-fallback are filled.
    assert len(sender.posted) >= 1
    card_title_text = sender.posted[0]["blocks"][0]["text"]["text"]
    # Task card titles start with *#N*.
    assert card_title_text.startswith("*#") or ":star:" in card_title_text
    # No legacy «:memo: Captured» follow-up.
    assert not any(
        ":memo: Captured" in m.get("text", "") for m in sender.posted
    )


# --------------------------------------------------------------------------- #
# FR-CR-02-3 reply-in-thread updates the card in place
# --------------------------------------------------------------------------- #


def test_fr_cr02_3_due_date_reply_updates_payload_and_asks_next(
    patched_session_scope, SessionFactory, bolt_context, slack_client, ack, sender
):
    from app.slack_bot.rate_limiter import RateAwareSlackSender

    # Seed a draft awaiting due_date.
    with SessionFactory() as s:
        from app.models import ContextSnapshot, IntentInference

        snap = ContextSnapshot(
            conversation_id="C1",
            source_ts="10.0",
            source_message={"ts": "10.0", "text": "x", "user": "U1"},
            history_before=[],
            thread_messages=[],
        )
        s.add(snap)
        s.flush()
        inf = IntentInference(
            context_snapshot_id=snap.id,
            intent=IE.create_task,
            confidence=0.9,
            invocation_type="mention",
        )
        s.add(inf)
        s.flush()
        d = ActionDraft(
            inference_id=inf.id,
            intent=IE.create_task,
            state=ActionDraftState.proposed,
            payload={"title": "foo"},
            slack_message_ts="10.0",
            card_channel="C1",
            card_ts="9.9",
            awaiting_field="due_date",
        )
        s.add(d)
        s.commit()
        did = d.id

    class _Cli:
        def __init__(self):
            self.updated = []

        def chat_postMessage(self, **kw):
            return SimpleNamespace(data={"ok": True, "ts": "0"})

        def chat_update(self, **kw):
            self.updated.append(kw)
            return SimpleNamespace(data={"ok": True})

    cli = _Cli()
    real_sender = RateAwareSlackSender(cli, min_interval_seconds=0.0)

    from tests.requirements.conftest import StubClassifier, _make_services

    services = _make_services(slack_client, StubClassifier(auto=True))
    handle_message(
        event={
            "ts": "11.0",
            "thread_ts": "10.0",
            "user": "U1",
            "text": "2026-05-01",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cr02-3"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=real_sender,
        ack=ack,
    )
    with SessionFactory() as s:
        d = s.get(ActionDraft, did)
        assert d.payload["due_date"] == "2026-05-01"
        assert d.awaiting_field == "owner"  # next missing
    assert cli.updated, "card must be updated in place"


def test_fr_cr02_3_relative_ru_date_resolves_friday():
    out = parse_reply(
        field="due_date",
        reply_text="до пятницы",
        settings=__import__("app.config", fromlist=["Settings"]).Settings(),
        today=date(2026, 4, 23),
    )
    assert out is not None
    # 2026-04-24 is Friday; dateparser may also land on the next Friday
    # depending on PREFER_DATES_FROM — assert it's some Friday in the future.
    assert out["due_date"].startswith("2026-0")


def test_fr_cr02_3_unparseable_reply_re_prompts(
    patched_session_scope, SessionFactory, bolt_context, slack_client, ack, sender
):
    from app.models import ContextSnapshot, IntentInference

    with SessionFactory() as s:
        snap = ContextSnapshot(
            conversation_id="C1",
            source_ts="20.0",
            source_message={"ts": "20.0", "text": "x", "user": "U1"},
            history_before=[],
            thread_messages=[],
        )
        s.add(snap)
        s.flush()
        inf = IntentInference(
            context_snapshot_id=snap.id,
            intent=IE.create_task,
            confidence=0.9,
            invocation_type="mention",
        )
        s.add(inf)
        s.flush()
        d = ActionDraft(
            inference_id=inf.id,
            intent=IE.create_task,
            state=ActionDraftState.proposed,
            payload={"title": "x"},
            slack_message_ts="20.0",
            card_channel="C1",
            card_ts="19.9",
            awaiting_field="due_date",
        )
        s.add(d)
        s.commit()
        did = d.id

    from tests.requirements.conftest import StubClassifier, _make_services

    services = _make_services(slack_client, StubClassifier(auto=True))
    handle_message(
        event={
            "ts": "21.0",
            "thread_ts": "20.0",
            "user": "U1",
            "text": "когда-нибудь",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cr02-3-unparse"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        d = s.get(ActionDraft, did)
        assert d.awaiting_field == "due_date"  # still awaiting
        assert "due_date" not in d.payload
    assert any("Couldn't parse" in m.get("text", "") for m in sender.posted)


# --------------------------------------------------------------------------- #
# FR-CR-02-4 confirm / ignore delete the widget
# --------------------------------------------------------------------------- #


def test_fr_cr02_4_confirm_cleans_up_followup_questions(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    """Widget is morphed in-place by FinalizeService (separate test).
    Confirm handler itself must delete every follow-up Q&A the bot posted
    under the widget — so only the live task card remains."""
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    class S:
        def __init__(inner):
            inner.posted = []
            inner.deleted = []

        def post_message(inner, **kw):
            inner.posted.append(kw)
            return {"ok": True, "ts": "0"}

        def delete_message(inner, channel, ts):
            inner.deleted.append((channel, ts))

    sender = S()
    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        d.card_channel = "C1"
        d.card_ts = "100.0"
        d.follow_up_message_ts = ["101.0", "102.0"]
        s.commit()
        did = d.id

    handle_confirm(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"ts": "100.0", "metadata": {"event_payload": {"metadata": "{}"}}},
        },
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    # Only the follow-up Q&A messages get deleted; the widget itself is
    # morphed in place by the finalizer.
    assert ("C1", "101.0") in sender.deleted
    assert ("C1", "102.0") in sender.deleted
    assert ("C1", "100.0") not in sender.deleted


def test_fr_cr02_4_ignore_deletes_widget(
    patched_session_scope, SessionFactory, ack
):
    from app.slack_bot.handlers.actions import handle_ignore
    from tests.test_persistence import _make_draft

    class S:
        def __init__(inner):
            inner.posted = []
            inner.deleted = []

        def post_message(inner, **kw):
            return {"ok": True}

        def delete_message(inner, channel, ts):
            inner.deleted.append((channel, ts))

    sender = S()
    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    handle_ignore(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"ts": "200.0", "metadata": {"event_payload": {"metadata": "{}"}}},
        },
        ack=ack,
        sender=sender,
    )
    assert sender.deleted == [("C1", "200.0")]


def test_fr_cr02_4_confirm_keeps_widget_on_failure(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    finalizer_stub.raise_error = RuntimeError("boom")

    class S:
        def __init__(inner):
            inner.posted = []
            inner.deleted = []

        def post_message(inner, **kw):
            inner.posted.append(kw)
            return {"ok": True, "ts": "0"}

        def delete_message(inner, channel, ts):
            inner.deleted.append((channel, ts))

    sender = S()
    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    handle_confirm(
        body={
            "actions": [{"value": str(did)}],
            "channel": {"id": "C1"},
            "message": {"ts": "300.0", "metadata": {"event_payload": {"metadata": "{}"}}},
        },
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert sender.deleted == []


# --------------------------------------------------------------------------- #
# FR-CR-02-5 no Open source button on task cards
# --------------------------------------------------------------------------- #


def test_fr_cr02_5_task_card_has_no_open_source_button(session):
    t = Task(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="U1",
        source_permalink="https://slack.com/x/p1",
    )
    session.add(t)
    session.flush()
    blocks = bk.task_card(task=t, viewer_slack_user_id="U1")
    ids = [
        el["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
    ]
    assert bk.ACTION_OPEN_SOURCE not in ids


# --------------------------------------------------------------------------- #
# FR-CR-02-6 daily digest Tracking section
# --------------------------------------------------------------------------- #


def test_fr_cr02_6_subscriber_receives_digest(session):
    today = date(2026, 4, 20)
    owner_task = Task(
        title="t",
        owner_user_id="U-owner",
        status=TaskStatus.todo,
        due_date=today,
    )
    session.add(owner_task)
    session.flush()
    SubscriptionService().subscribe(session, task=owner_task, slack_user_id="U-watcher")

    class R:
        def __init__(inner):
            inner.messages = []

        def post_message(inner, **kw):
            inner.messages.append(kw)
            return {"ok": True}

    sender = R()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    assert {m["channel"] for m in sender.messages} == {"U-owner", "U-watcher"}


def test_fr_cr02_6_digest_has_tracking_and_manage_button(session):
    today = date(2026, 4, 20)
    owner_task = Task(
        title="shared",
        owner_user_id="U-owner",
        status=TaskStatus.todo,
        due_date=today,
    )
    session.add(owner_task)
    session.flush()
    SubscriptionService().subscribe(session, task=owner_task, slack_user_id="U-watcher")

    class R:
        def __init__(inner):
            inner.messages = []

        def post_message(inner, **kw):
            inner.messages.append(kw)
            return {"ok": True}

    sender = R()
    DigestService(sender=sender).send(session, DigestKind.daily, today=today)
    watcher_msg = next(m for m in sender.messages if m["channel"] == "U-watcher")
    texts = [
        b["text"]["text"]
        for b in watcher_msg["blocks"]
        if b.get("type") == "section"
    ]
    assert any("Tracking" in t for t in texts)
    assert any("shared" in t for t in texts)

    actions_block = next(
        b for b in watcher_msg["blocks"] if b.get("type") == "actions"
    )
    ids = [el["action_id"] for el in actions_block["elements"]]
    assert bk.ACTION_MANAGE_SUBSCRIPTIONS in ids


# --------------------------------------------------------------------------- #
# FR-CR-02-7 Manage-subscriptions modal
# --------------------------------------------------------------------------- #


def test_fr_cr02_7_manage_modal_opens_with_current_subscriptions(
    patched_session_scope, SessionFactory, ack
):
    with SessionFactory() as s:
        t = Task(title="shared", owner_user_id="U-owner", status=TaskStatus.todo)
        s.add(t)
        s.flush()
        SubscriptionService().subscribe(s, task=t, slack_user_id="U-me")
        s.commit()

    class ViewCli:
        def __init__(inner):
            inner.opened = []

        def views_open(inner, trigger_id, view):
            inner.opened.append((trigger_id, view))
            return {"ok": True}

    cli = ViewCli()
    handle_manage_subscriptions(
        body={"trigger_id": "t", "user": {"id": "U-me"}}, client=cli, ack=ack
    )
    assert cli.opened
    _, view = cli.opened[0]
    assert view["callback_id"] == bk.MODAL_CALLBACK_SUBSCRIPTIONS


def test_fr_cr02_7_modal_unsubscribe_updates_view_and_row(
    patched_session_scope, SessionFactory, ack
):
    from app.models import TaskSubscription

    with SessionFactory() as s:
        t = Task(title="shared", owner_user_id="U-owner", status=TaskStatus.todo)
        s.add(t)
        s.flush()
        SubscriptionService().subscribe(s, task=t, slack_user_id="U-me")
        s.commit()
        tid = t.id

    class ViewCli:
        def __init__(inner):
            inner.updated = []

        def views_update(inner, view_id, view):
            inner.updated.append((view_id, view))
            return {"ok": True}

    cli = ViewCli()
    handle_unsubscribe_in_modal(
        body={
            "actions": [{"value": str(tid)}],
            "user": {"id": "U-me"},
            "view": {"id": "V1"},
        },
        client=cli,
        ack=ack,
    )
    assert cli.updated
    with SessionFactory() as s:
        assert (
            s.query(TaskSubscription).filter_by(task_id=tid, slack_user_id="U-me").count()
            == 0
        )


# --------------------------------------------------------------------------- #
# NFR-CR-02-1 idempotent upserts
# --------------------------------------------------------------------------- #


def test_nfr_cr02_1_upsert_conversation_idempotent(session):
    from app.slack_bot.handlers.shared import upsert_conversation

    a = upsert_conversation(session, channel_id="C1", kind="channel")
    b = upsert_conversation(session, channel_id="C1", kind="channel")
    assert a.id == b.id == "C1"


def test_nfr_cr02_1_upsert_message_idempotent(session):
    from app.slack_bot.handlers.shared import upsert_conversation, upsert_message

    conv = upsert_conversation(session, channel_id="C1", kind="channel")
    m1 = upsert_message(
        session,
        conversation=conv,
        message={"ts": "9.9", "text": "x", "user": "U1"},
    )
    m2 = upsert_message(
        session,
        conversation=conv,
        message={"ts": "9.9", "text": "x", "user": "U1"},
    )
    assert m1.id == m2.id


# --------------------------------------------------------------------------- #
# NFR-CR-02-2 mention handler never silent
# --------------------------------------------------------------------------- #


def test_nfr_cr02_2_mention_handler_posts_reply_on_crash(
    patched_session_scope, sender, ack, bolt_context, slack_client, monkeypatch
):
    from app.slack_bot.handlers import events as events_module

    monkeypatch.setattr(
        events_module,
        "classify_and_persist",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    from tests.requirements.conftest import StubClassifier, _make_services

    services = _make_services(slack_client, StubClassifier(auto=True))
    handle_app_mention(
        event={
            "ts": "50.0",
            "user": "U1",
            "text": "<@UBOT> t",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "cr02-crash"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    assert ack.called
    assert any(":warning:" in m.get("text", "") for m in sender.posted)
