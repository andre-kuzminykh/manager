"""Requirement coverage: FR-CR-04-7 (mention follow-up parity for
assumed owners).

@mention should ask "кому назначаем?" in the thread whenever the
resulting Task's owner is only a fallback to the message author
(owner_assumed=True) — matching passive-path parity.

Previously pick_next_missing saw owner_user_id set to the author and
skipped the question, leaving the user stuck with a "(предположительно
ты)" label they had no obvious way to correct via chat.
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


def test_pick_next_missing_treats_assumed_owner_as_missing():
    payload_with_real_owner = {
        "title": "t",
        "due_date": "2026-05-01",
        "owner_user_id": "U-ivan",
        "owner_display_name": "Ivan",
        "owner_assumed": False,
    }
    assert pick_next_missing("create_task", payload_with_real_owner) is None

    payload_with_assumed_owner = {
        "title": "t",
        "due_date": "2026-05-01",
        "owner_user_id": "U-author",  # fallback to author
        "owner_display_name": None,
        "owner_assumed": True,
    }
    assert pick_next_missing("create_task", payload_with_assumed_owner) == "owner"


def test_task_payload_exposes_owner_assumed_flag():
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


def test_mention_asks_about_owner_when_owner_is_assumed(
    patched_session_scope,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """LLM returns a task with no explicit owner. Persistence falls back
    to the message author + owner_assumed=True. The @mention handler
    must ask the author who to actually assign in the thread."""
    from app.slack_bot.handlers.events import handle_app_mention

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
        assert draft.awaiting_field == "owner"

    # The follow-up question text went into the source thread.
    thread_posts = [p for p in sender.posts if p.get("thread_ts") == "500.0"]
    # At least one post should be the owner question (starts with
    # ":memo:" intro or mentions "Кому назначаем").
    assert any(
        "назначаем" in (p.get("text") or "") for p in thread_posts
    ), [p.get("text") for p in thread_posts]
