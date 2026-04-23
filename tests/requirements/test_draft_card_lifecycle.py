"""After Confirm or Ignore, the draft card must disappear from the thread.
On finalize failure it stays so the user can retry."""
from __future__ import annotations

import pytest

from app.models import ActionDraft, ActionDraftState
from app.models.intent import IntentType as IE


class _DeletingSender:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.deleted: list[dict] = []

    def post_message(self, **kw):
        self.posted.append(kw)
        return {"ok": True, "ts": "9.9"}

    def delete_message(self, channel: str, ts: str):
        self.deleted.append({"channel": channel, "ts": ts})
        return {"ok": True}


def _body(draft_id: int, *, channel="C1", message_ts="100.0"):
    return {
        "actions": [{"value": str(draft_id)}],
        "channel": {"id": channel},
        "message": {"ts": message_ts, "metadata": {"event_payload": {"metadata": "{}"}}},
        "user": {"id": "U1"},
        "trigger_id": "t",
    }


def test_confirm_deletes_draft_card_on_success(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    sender = _DeletingSender()
    handle_confirm(
        body=_body(did, channel="C1", message_ts="100.0"),
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert sender.deleted == [{"channel": "C1", "ts": "100.0"}]


def test_confirm_keeps_draft_card_on_failure(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    finalizer_stub.raise_error = RuntimeError("db down")
    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    sender = _DeletingSender()
    handle_confirm(
        body=_body(did),
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    # On failure: the warning is posted but the card stays.
    assert sender.deleted == []
    assert any("Failed" in m.get("text", "") for m in sender.posted)


def test_ignore_deletes_draft_card(patched_session_scope, SessionFactory, ack):
    from app.slack_bot.handlers.actions import handle_ignore
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    sender = _DeletingSender()
    handle_ignore(body=_body(did, message_ts="55.5"), ack=ack, sender=sender)

    with SessionFactory() as s:
        assert s.get(ActionDraft, did).state == ActionDraftState.ignored
    assert sender.deleted == [{"channel": "C1", "ts": "55.5"}]


def test_ignore_without_sender_still_marks_ignored(
    patched_session_scope, SessionFactory, ack
):
    """Back-compat: old callsites that don't pass sender still work."""
    from app.slack_bot.handlers.actions import handle_ignore
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    handle_ignore(body=_body(did), ack=ack)
    with SessionFactory() as s:
        assert s.get(ActionDraft, did).state == ActionDraftState.ignored


def test_confirm_handles_missing_channel_or_ts_gracefully(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    """If Slack payload lacks channel/ts, deletion is skipped silently."""
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    sender = _DeletingSender()
    body = {
        "actions": [{"value": str(did)}],
        "channel": {},
        "message": {"metadata": {"event_payload": {"metadata": "{}"}}},
    }
    handle_confirm(
        body=body,
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert sender.deleted == []


def test_delete_failure_does_not_abort_confirm(
    patched_session_scope, SessionFactory, slack_client, finalizer_stub, ack
):
    """If chat.delete fails (bot can't delete someone else's message in DM),
    the confirm success message has already been posted, and we log and move
    on rather than raising."""
    from app.slack_bot.handlers.actions import handle_confirm
    from tests.test_persistence import _make_draft

    class FailingDeleter:
        def __init__(self):
            self.posted = []

        def post_message(self, **kw):
            self.posted.append(kw)
            return {"ok": True, "ts": "0"}

        def delete_message(self, channel, ts):
            raise RuntimeError("not allowed")

    with SessionFactory() as s:
        d = _make_draft(s, intent=IE.create_task, payload={"title": "t"})
        s.commit()
        did = d.id

    sender = FailingDeleter()
    # Should not raise.
    handle_confirm(
        body=_body(did),
        client=slack_client,
        services=None,
        finalizer=finalizer_stub,
        sender=sender,
        ack=ack,
    )
    assert any("created" in m.get("text", "").lower() for m in sender.posted)
