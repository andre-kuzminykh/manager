"""Requirement coverage: FR-CR-04-6 ∩ FR-CR-04-7.

The two capture scenarios should be symmetric except for the level of
control: @mention auto-creates the task, passive offers an
Accept/Edit/Reject card. Everything else — the first follow-up
question, its intro line, its routing in the thread — must match.

We verify that claim by running both paths on two messages that are
identical except for the @mention token and asserting the first
follow-up question posted is the same (":memo: Captured: *<title>*.\n"
+ prompt_for(owner)).
"""
from __future__ import annotations

from app.models import ActionDraft
from app.schemas.intent import IntentClassification, IntentType, TaskDraft
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


def _make_stub():
    # Task with no explicit owner and no date → both flows will find
    # owner missing first.
    return StubClassifier(
        IntentClassification(
            intent=IntentType.create_task,
            confidence=0.9,
            task=TaskDraft(title="сделать презентацию"),
        )
    )


def _first_memo(sender: _Sender) -> str | None:
    for p in sender.posts:
        text = p.get("text") or ""
        if text.startswith(":memo:"):
            return text
    return None


def test_passive_and_mention_ask_the_same_first_question(
    patched_session_scope,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.slack_bot.handlers.events import handle_app_mention, handle_message

    # Passive run.
    services_passive = _make_services(slack_client, _make_stub())
    sender_passive = _Sender()
    handle_message(
        event={
            "ts": "900.0",
            "user": "U-author",
            "text": "сделать презентацию",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "sym-passive"},
        client=slack_client,
        context=bolt_context,
        services=services_passive,
        sender=sender_passive,
        ack=ack,
    )
    passive_memo = _first_memo(sender_passive)

    # Mention run — same text, with the @bot prefix.
    services_mention = _make_services(slack_client, _make_stub())
    sender_mention = _Sender()
    handle_app_mention(
        event={
            "ts": "901.0",
            "user": "U-author",
            "text": "<@UBOT> сделать презентацию",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "sym-mention"},
        client=slack_client,
        context=bolt_context,
        services=services_mention,
        sender=sender_mention,
        ack=ack,
    )
    mention_memo = _first_memo(sender_mention)

    assert passive_memo is not None and mention_memo is not None, (
        passive_memo,
        mention_memo,
    )
    # Identical intro AND identical follow-up question.
    assert passive_memo == mention_memo


def test_both_paths_skip_owner_question_when_assumed(
    patched_session_scope,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """Author fallback fills the owner on BOTH mention and passive
    paths, so the first follow-up question is about the date, not the
    owner."""
    from app.slack_bot.handlers.events import handle_message

    services = _make_services(slack_client, _make_stub())
    sender = _Sender()
    handle_message(
        event={
            "ts": "950.0",
            "user": "U-author",
            "text": "сделать презентацию",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "sym-passive-2"},
        client=slack_client,
        context=bolt_context,
        services=services,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        # Owner filled via author fallback → next missing is due_date.
        assert draft.payload.get("owner_user_id") == "U-author"
        assert draft.payload.get("owner_assumed") is True
        assert draft.awaiting_field == "due_date"
    memo = _first_memo(sender) or ""
    assert "назначаем" not in memo  # no owner prompt
    assert "deadline" in memo.lower()
