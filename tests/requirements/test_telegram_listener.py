"""FR-CR-04-27 — live Telegram Bot API listener.

Tests cover:

- `parse_update` correctly extracts a `TelegramSourceMessage` from
  every shape the Bot API uses (`message`, `edited_message`,
  `channel_post`, `edited_channel_post`).
- Service updates (no message field) are dropped via `parse_update`.
- A listener tick processes returned updates, advances the singleton
  offset, and writes ProcessedTelegramMessage / Task rows.
- A second tick with the same offset is a no-op.
- A failure inside `process_one` is counted as `errors` and doesn't
  abort the batch.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.intent import IntentClassifier
from app.models import (
    ProcessedTelegramMessage,
    Task,
    TaskSourceKind,
    TelegramListenerState,
)
from app.orchestrator import Orchestrator
from app.schemas.intent import (
    IntentClassification,
    IntentType,
    InvocationType,
    TaskDraft,
)
from app.telegram_bot.listener import (
    ListenerReport,
    TelegramListener,
    parse_update,
)
from app.telegram_ingest.reader import TelegramSourceMessage
from app.telegram_ingest.service import TelegramIngestService


# --------------------------------------------------------------------------- #
# parse_update
# --------------------------------------------------------------------------- #


def test_parse_update_extracts_message_fields():
    out = parse_update(
        {
            "update_id": 100,
            "message": {
                "message_id": 7,
                "from": {"id": 42, "username": "andre"},
                "chat": {"id": -1001234567890, "type": "supergroup", "title": "T"},
                "date": 1714294800,
                "text": "к завтра подготовить презу",
                "reply_to_message": {"message_id": 4},
            },
        }
    )
    assert out is not None
    assert out.chat_id == -1001234567890
    assert out.message_id == 7
    assert out.user_id == 42
    assert out.user_name == "@andre"
    assert out.text == "к завтра подготовить презу"
    assert out.reply_to == 4
    assert out.chat_title == "T"
    assert isinstance(out.sent_at, datetime)


def test_parse_update_handles_edited_message():
    out = parse_update(
        {
            "update_id": 101,
            "edited_message": {
                "message_id": 9,
                "chat": {"id": 5, "type": "private"},
                "from": {"id": 1, "first_name": "X"},
                "text": "fixed",
                "date": 0,
            },
        }
    )
    assert out is not None
    assert out.message_id == 9
    assert out.text == "fixed"
    assert out.user_name == "X"


def test_parse_update_uses_caption_when_no_text():
    out = parse_update(
        {
            "update_id": 102,
            "message": {
                "message_id": 1,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1},
                "caption": "photo with caption",
                "date": 0,
            },
        }
    )
    assert out is not None
    assert out.text == "photo with caption"


def test_parse_update_returns_none_for_service_updates():
    assert parse_update({"update_id": 1, "callback_query": {}}) is None
    assert parse_update({"update_id": 2, "my_chat_member": {}}) is None
    assert parse_update({"update_id": 3}) is None


def test_parse_update_combines_first_and_last_name():
    out = parse_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "first_name": "Andre", "last_name": "K."},
                "text": "x",
                "date": 0,
            },
        }
    )
    assert out is not None
    assert out.user_name == "Andre K."


# --------------------------------------------------------------------------- #
# Listener tick
# --------------------------------------------------------------------------- #


class _StubClassifier:
    def __init__(self, classification: IntentClassification) -> None:
        self._c = classification

    def classify(self, *, context, invocation_type, known_employees=None):
        return self._c


def _make_ingest(classification: IntentClassification) -> TelegramIngestService:
    from app.config import Settings

    return TelegramIngestService(
        classifier=_StubClassifier(classification),
        orchestrator=Orchestrator(Settings()),
    )


def _make_listener(
    classification: IntentClassification,
    updates_per_call: list[list[dict]],
) -> TelegramListener:
    """Builds a listener whose `_fetch_updates` returns the next list
    from `updates_per_call` on each call (then `[]` forever)."""
    listener = TelegramListener(
        token="123:abc", ingest=_make_ingest(classification)
    )
    state = {"calls": 0}

    def fake_fetch(*, offset: int) -> list[dict]:
        idx = state["calls"]
        state["calls"] += 1
        if idx < len(updates_per_call):
            return updates_per_call[idx]
        return []

    listener._fetch_updates = fake_fetch  # type: ignore[method-assign]
    return listener


def test_listener_tick_processes_updates_and_advances_offset(
    patched_session_scope, SessionFactory
):
    """Private-chat (DM) messages take the immediate-create path:
    classify → Task → live card. (Group messages now go through the
    confirm-first draft flow — covered by a separate test below.)"""
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="x"),
        reasoning="r",
    )
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                {
                    "update_id": 100,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 1},
                        "text": "prepare a report",
                        "date": 0,
                    },
                },
                {
                    "update_id": 101,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 1},
                        "text": "another task",
                        "date": 0,
                    },
                },
            ]
        ],
    )

    report = listener.tick()
    assert report.updates_seen == 2
    assert report.messages_processed == 2
    assert report.tasks_created == 2

    with SessionFactory() as s:
        state = s.get(TelegramListenerState, 1)
        assert state is not None
        assert state.last_update_id == 101
        assert s.query(Task).count() == 2
        rows = s.query(ProcessedTelegramMessage).all()
        assert {(r.chat_id, r.message_id) for r in rows} == {(7, 1), (7, 2)}
        # Both tasks are flagged as Telegram-sourced.
        for t in s.query(Task).all():
            assert t.source_kind == TaskSourceKind.telegram


def test_listener_tick_handles_no_action_classification(
    patched_session_scope, SessionFactory
):
    classification = IntentClassification(
        intent=IntentType.no_action,
        confidence=0.1,
        reasoning="not a task",
    )
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                {
                    "update_id": 200,
                    "message": {
                        "message_id": 5,
                        "chat": {"id": 9, "type": "private"},
                        "from": {"id": 1},
                        "text": "thanks!",
                        "date": 0,
                    },
                }
            ]
        ],
    )

    report = listener.tick()
    assert report.no_action == 1
    assert report.tasks_created == 0
    with SessionFactory() as s:
        assert s.query(Task).count() == 0
        # Bookmark is still written so the same update never comes back.
        assert s.get(ProcessedTelegramMessage, (9, 5)) is not None


def test_listener_tick_skips_non_message_updates(patched_session_scope):
    classification = IntentClassification(
        intent=IntentType.no_action, confidence=0.0
    )
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                # Service updates that aren't messages and aren't
                # callback_queries — both skipped.
                {"update_id": 301, "my_chat_member": {}},
                {"update_id": 302, "chat_member": {}},
            ]
        ],
    )
    report = listener.tick()
    assert report.skipped_non_message == 2
    assert report.messages_processed == 0
    assert report.callbacks_handled == 0


def test_listener_tick_routes_callback_query_to_handler(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-04-28: a button press lands as `callback_query` on
    `getUpdates`. The listener must dispatch it to the matching
    handler (here: Start), persist the offset, and not count it as
    a skipped message."""
    from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus

    # Seed a task we can Start.
    with SessionFactory() as s:
        t = Task(
            title="x",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
            owner_user_id="55555",
            source_kind=TaskSourceKind.telegram,
            card_channel="-1001",
            card_ts="42",
        )
        s.add(t)
        s.commit()
        tid = t.id

    listener = _make_listener(
        IntentClassification(intent=IntentType.no_action, confidence=0.0),
        updates_per_call=[
            [
                {
                    "update_id": 500,
                    "callback_query": {
                        "id": "cb-1",
                        "from": {"id": 55555},
                        "data": f"start:{tid}",
                        "message": {
                            "message_id": 42,
                            "chat": {"id": -1001, "type": "supergroup"},
                        },
                    },
                }
            ]
        ],
    )
    # Stub the outbound sender so the listener doesn't actually
    # try to call api.telegram.org during the test.
    listener._sender.send_message = lambda **kw: {"message_id": 99}  # type: ignore
    listener._sender.update_message = lambda **kw: {}  # type: ignore
    listener._sender.answer_callback_query = lambda **kw: {}  # type: ignore

    report = listener.tick()
    assert report.callbacks_handled == 1
    assert report.skipped_non_message == 0

    with SessionFactory() as s:
        task = s.get(Task, tid)
        assert task.status == TaskStatus.in_progress


def test_listener_second_tick_with_same_offset_is_a_noop(
    patched_session_scope, SessionFactory
):
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="x"),
    )
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                {
                    "update_id": 400,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 1},
                        "text": "prepare a deck",
                        "date": 0,
                    },
                }
            ]
            # Subsequent calls return [] (long-poll timeout)
        ],
    )
    listener.tick()
    second = listener.tick()
    assert second.updates_seen == 0
    with SessionFactory() as s:
        assert s.query(Task).count() == 1


def test_listener_at_mention_in_group_skips_confirm_widget(
    patched_session_scope, SessionFactory
):
    """FR-CR-04-32 ext: an explicit @-mention in a group message
    bypasses the confirm widget — intent is unambiguous, so the
    listener creates the Task immediately like in a private DM."""
    from app.models import Task

    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.95,
        task=TaskDraft(title="prepare deck"),
    )
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                {
                    "update_id": 920,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": -7007, "type": "supergroup"},
                        "from": {"id": 555, "username": "andre"},
                        "text": "@petya подготовь презу к завтра",
                        "date": 0,
                    },
                }
            ]
        ],
    )
    listener._sender.send_message = lambda **kw: {"message_id": 1}  # type: ignore
    listener._sender.update_message = lambda **kw: {}  # type: ignore

    report = listener.tick()
    # Immediate-create — Task created, NOT a draft.
    assert report.tasks_created == 1
    assert report.drafts_proposed == 0
    with SessionFactory() as s:
        assert s.query(Task).count() == 1


def test_listener_routes_group_messages_to_draft_flow(
    patched_session_scope, SessionFactory
):
    """FR-CR-04-32: a task-shaped message in a supergroup creates an
    ActionDraft (state=proposed) and DMs a confirm widget — it does
    NOT create a Task immediately. The user must Accept first."""
    from app.models import ActionDraft, ActionDraftState

    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="prepare deck"),
        reasoning="r",
    )
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                {
                    "update_id": 700,
                    "message": {
                        "message_id": 1,
                        "chat": {
                            "id": -1001,
                            "type": "supergroup",
                            "title": "Team",
                        },
                        "from": {"id": 222, "username": "andre"},
                        "text": "к завтра подготовить презу",
                        "date": 0,
                    },
                }
            ]
        ],
    )
    sent: list[dict] = []
    listener._sender.forward_message = lambda **kw: sent.append({"forward": kw}) or {"message_id": 90}  # type: ignore
    listener._sender.send_message = lambda **kw: sent.append({"send": kw}) or {"message_id": 91}  # type: ignore

    report = listener.tick()
    assert report.drafts_proposed == 1
    assert report.tasks_created == 0

    with SessionFactory() as s:
        # No Task yet — only an ActionDraft in the proposed state.
        assert s.query(Task).count() == 0
        drafts = s.query(ActionDraft).all()
        assert len(drafts) == 1
        d = drafts[0]
        assert d.state == ActionDraftState.proposed
        # The widget locations were stored on the draft for later
        # editing on Accept / Reject.
        widgets = (d.payload or {}).get("_widgets") or []
        assert len(widgets) == 1
        # The pending source dict is also retained so the Accept
        # handler can finalise the draft into a real Task.
        assert (d.payload or {}).get("_pending", {}).get("source_chat_id") == -1001

    # FR-CR-05-10: only sendMessage now — the widget itself carries
    # the LLM-generated context summary in its description, so we
    # no longer split into a separate forwardMessage + widget.
    kinds = [list(x.keys())[0] for x in sent]
    assert "send" in kinds
    assert "forward" not in kinds


def test_listener_confirm_button_finalises_draft_into_task(
    patched_session_scope, SessionFactory
):
    """Clicking ✅ Accept on a draft widget creates the Task and
    edits each widget DM into the regular task card."""
    from app.models import ActionDraft, ActionDraftState

    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="prepare deck"),
    )
    # Step 1 — feed a group message so the listener creates a draft.
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                {
                    "update_id": 800,
                    "message": {
                        "message_id": 5,
                        "chat": {"id": -2002, "type": "supergroup"},
                        "from": {"id": 333},
                        "text": "сделать слайды к завтра",
                        "date": 0,
                    },
                }
            ],
            # Step 2 — feed a callback_query (Accept). We patch the
            # entity_id to the actual draft_id once we know it.
            [],
        ],
    )
    listener._sender.forward_message = lambda **kw: {"message_id": 1}  # type: ignore
    listener._sender.send_message = lambda **kw: {"message_id": 2}  # type: ignore
    edits: list[dict] = []
    listener._sender.update_message = lambda **kw: edits.append(kw) or {}  # type: ignore
    listener._sender.answer_callback_query = lambda **kw: {}  # type: ignore

    listener.tick()
    with SessionFactory() as s:
        draft_id = s.query(ActionDraft).one().id

    # Replace the second-call updates with a confirm callback for the
    # actual draft id.
    cb_state = {"served": False}

    def fetch_cb(*, offset):
        if cb_state["served"]:
            return []
        cb_state["served"] = True
        return [
            {
                "update_id": 801,
                "callback_query": {
                    "id": "cb-c",
                    "from": {"id": 333},
                    "data": f"confirm:{draft_id}",
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 333, "type": "private"},
                    },
                },
            }
        ]

    listener._fetch_updates = fetch_cb  # type: ignore[method-assign]
    listener.tick()

    with SessionFactory() as s:
        # Now there's a Task and the draft is confirmed.
        tasks = s.query(Task).all()
        assert len(tasks) == 1
        assert tasks[0].title == "prepare deck"
        assert tasks[0].source_kind == TaskSourceKind.telegram
        d = s.get(ActionDraft, draft_id)
        assert d.state == ActionDraftState.confirmed
        assert d.task_id == tasks[0].id

    # Crucial: the widget DM was edited in place into the regular
    # task card. If `handle_confirm_draft` accidentally clears the
    # widget locations off `draft.payload` before we can replace
    # them, this `update_message` call is silently skipped — Accept
    # would appear to do nothing in the UI.
    assert any(
        e.get("chat_id") == 333 and e.get("message_id") == 2 for e in edits
    ), f"expected widget edit on (333, 2), got {edits}"


def test_listener_reject_button_marks_draft_ignored(
    patched_session_scope, SessionFactory
):
    """Clicking ✖ Reject leaves no Task and flips the draft to
    ignored."""
    from app.models import ActionDraft, ActionDraftState

    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="x"),
    )
    listener = _make_listener(
        classification,
        updates_per_call=[
            [
                {
                    "update_id": 900,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": -3003, "type": "supergroup"},
                        "from": {"id": 444},
                        "text": "task to reject",
                        "date": 0,
                    },
                }
            ]
        ],
    )
    listener._sender.forward_message = lambda **kw: {"message_id": 1}  # type: ignore
    listener._sender.send_message = lambda **kw: {"message_id": 2}  # type: ignore
    listener._sender.update_message = lambda **kw: {}  # type: ignore
    listener._sender.answer_callback_query = lambda **kw: {}  # type: ignore

    listener.tick()
    with SessionFactory() as s:
        draft_id = s.query(ActionDraft).one().id

    cb_state = {"served": False}

    def fetch_cb(*, offset):
        if cb_state["served"]:
            return []
        cb_state["served"] = True
        return [
            {
                "update_id": 901,
                "callback_query": {
                    "id": "cb-r",
                    "from": {"id": 444},
                    "data": f"ignore:{draft_id}",
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 444, "type": "private"},
                    },
                },
            }
        ]

    listener._fetch_updates = fetch_cb  # type: ignore[method-assign]
    listener.tick()

    with SessionFactory() as s:
        assert s.query(Task).count() == 0
        d = s.get(ActionDraft, draft_id)
        assert d.state == ActionDraftState.ignored


def test_listener_disabled_when_token_empty():
    listener = TelegramListener(token="", ingest=_make_ingest(
        IntentClassification(intent=IntentType.no_action, confidence=0.0)
    ))
    assert listener.enabled is False
