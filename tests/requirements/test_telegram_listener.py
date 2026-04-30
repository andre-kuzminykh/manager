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
    from `updates_per_call` on each call (then `[]` forever).

    Test fixtures use synthetic `"date": 0` (1970-01-01) so we
    pre-set `_bot_api_started_at` to the epoch — otherwise the
    FR-CR-05-51 «process from now» filter would correctly drop
    every test message as ancient. Production-side, the cutoff
    is set to `datetime.now(timezone.utc)` on first tick.
    """
    from datetime import datetime as _dt, timezone as _tz

    listener = TelegramListener(
        token="123:abc", ingest=_make_ingest(classification)
    )
    listener._bot_api_started_at = _dt(1970, 1, 1, tzinfo=_tz.utc)
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
        task=TaskDraft(title="x", description="x desc"),
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
    # FR-CR-05-110 — exact-title + owner overlap fast path
    # short-circuits the second task (same title="x" / same
    # owner). Only ONE Task lands.
    assert report.tasks_created == 1

    with SessionFactory() as s:
        state = s.get(TelegramListenerState, 1)
        assert state is not None
        assert state.last_update_id == 101
        # FR-CR-05-110 — second update was deduped against the
        # first via the exact-title fast path.
        assert s.query(Task).count() == 1
        rows = s.query(ProcessedTelegramMessage).all()
        assert {(r.chat_id, r.message_id) for r in rows} == {(7, 1), (7, 2)}
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
        task=TaskDraft(title="x", description="x desc"),
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
        task=TaskDraft(title="prepare deck", description="Prep slides for the meeting."),
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
        task=TaskDraft(title="prepare deck", description="Prep slides for the meeting."),
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
        task=TaskDraft(title="prepare deck", description="Prep slides for the meeting."),
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
        assert tasks[0].title == "Prepare deck"
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
        task=TaskDraft(title="x", description="x desc"),
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


# --------------------------------------------------------------------------- #
# FR-CR-05-28 — periodic Sheet → DB poll
# --------------------------------------------------------------------------- #


def test_listener_runs_sheet_pulls_when_interval_elapsed():
    """FR-CR-05-28 — listener fires both Sheet pulls (Tasks +
    Team) every ``sheet_poll_interval_seconds``; first tick
    after construction always runs them."""
    team_calls = {"n": 0}
    tasks_calls = {"n": 0}

    class _StubTeamSync:
        def pull(self, session):
            team_calls["n"] += 1
            return 0, 0

    class _StubTasksPull:
        def pull(self, session):
            tasks_calls["n"] += 1
            return 0, 0, 0

    listener = TelegramListener(
        token="123:abc",
        ingest=_make_ingest(IntentClassification(
            intent=IntentType.no_action, confidence=0.0
        )),
        team_sheet_factory=lambda: _StubTeamSync(),
        tasks_sheet_pull_factory=lambda: _StubTasksPull(),
        sheet_poll_interval_seconds=60,
    )
    listener._maybe_run_sheet_pulls()
    assert team_calls["n"] == 1
    assert tasks_calls["n"] == 1


def test_listener_throttles_sheet_pulls_within_interval():
    """Calling `_maybe_run_sheet_pulls` repeatedly within the
    interval window must NOT fire repeated pulls."""
    team_calls = {"n": 0}

    class _StubTeamSync:
        def pull(self, session):
            team_calls["n"] += 1
            return 0, 0

    listener = TelegramListener(
        token="123:abc",
        ingest=_make_ingest(IntentClassification(
            intent=IntentType.no_action, confidence=0.0
        )),
        team_sheet_factory=lambda: _StubTeamSync(),
        sheet_poll_interval_seconds=60,
    )
    listener._maybe_run_sheet_pulls()
    listener._maybe_run_sheet_pulls()
    listener._maybe_run_sheet_pulls()
    assert team_calls["n"] == 1


def test_listener_skips_sheet_pulls_when_interval_zero():
    """``sheet_poll_interval_seconds=0`` disables the listener-side
    polling — useful when running an external cron that does the
    same work."""
    team_calls = {"n": 0}

    class _StubTeamSync:
        def pull(self, session):
            team_calls["n"] += 1
            return 0, 0

    listener = TelegramListener(
        token="123:abc",
        ingest=_make_ingest(IntentClassification(
            intent=IntentType.no_action, confidence=0.0
        )),
        team_sheet_factory=lambda: _StubTeamSync(),
        sheet_poll_interval_seconds=0,
    )
    listener._maybe_run_sheet_pulls()
    assert team_calls["n"] == 0


def test_listener_swallows_sheet_pull_errors():
    """A transient HTTP error from Sheets must not break the
    listener — it logs and continues so Telegram updates stay
    flowing."""
    class _BoomTeamSync:
        def pull(self, session):
            raise RuntimeError("HTTP 500")

    listener = TelegramListener(
        token="123:abc",
        ingest=_make_ingest(IntentClassification(
            intent=IntentType.no_action, confidence=0.0
        )),
        team_sheet_factory=lambda: _BoomTeamSync(),
        sheet_poll_interval_seconds=60,
    )
    # Should not raise.
    listener._maybe_run_sheet_pulls()


# --------------------------------------------------------------------------- #
# FR-CR-05-35 — periodic Supabase view poll
# --------------------------------------------------------------------------- #


def test_listener_view_realtime_off_by_default(
    patched_session_scope, SessionFactory
):
    """Disabled flag ⇒ poll is a no-op even when reader is
    configured. Makes sure the realtime feature stays opt-in."""
    from app.telegram_ingest.reader import TelegramSourceMessage

    iter_calls = {"n": 0}

    class _StubReader:
        configured = True

        def iter_newest(self, *, limit):
            iter_calls["n"] += 1
            return iter([])

    classification = IntentClassification(
        intent=IntentType.no_action, confidence=0.0
    )
    ingest = _make_ingest(classification)
    ingest._reader = _StubReader()  # noqa: SLF001

    listener = TelegramListener(
        token="123:abc",
        ingest=ingest,
        view_realtime_enabled=False,
        view_poll_interval_seconds=30,
    )
    listener._maybe_poll_source_view()
    assert iter_calls["n"] == 0


def test_listener_view_realtime_pulls_when_enabled(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-35 — flag on + reader configured ⇒ first call
    pulls the latest batch from the view and runs each message
    through `prepare_drafts`. Already-processed messages
    short-circuit per the existing bookmark."""
    from app.models import ProcessedTelegramMessage
    from app.telegram_ingest.reader import TelegramSourceMessage

    seen_messages: list[TelegramSourceMessage] = [
        TelegramSourceMessage(
            chat_id=-1001234, message_id=42,
            text="prepare deck for Friday", user_id=99,
        ),
        # Already-processed marker — bookmark exists, will skip.
        TelegramSourceMessage(
            chat_id=-1001234, message_id=41,
            text="some old", user_id=99,
        ),
    ]

    class _StubReader:
        configured = True

        def iter_newest(self, *, limit):
            return iter(seen_messages)

    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="prepare deck", description="Prep slides for the meeting."),
    )
    ingest = _make_ingest(classification)
    ingest._reader = _StubReader()  # noqa: SLF001

    # Pre-bookmark message_id=41 so the second message short-
    # circuits as «already processed». That signals «caught up»
    # and the batch-expansion loop terminates after one round.
    from datetime import datetime, timezone as _tz
    with SessionFactory() as s:
        s.add(
            ProcessedTelegramMessage(
                chat_id=-1001234,
                message_id=41,
                processed_at=datetime.now(_tz.utc),
                task_id=None,
            )
        )
        s.commit()

    listener = TelegramListener(
        token="123:abc",
        ingest=ingest,
        view_realtime_enabled=True,
        view_poll_interval_seconds=1,
        view_poll_batch_size=10,
    )
    sent: list[dict] = []

    class _RecSender:
        enabled = True

        def send_message(self, **kw):
            sent.append(kw)
            return {"message_id": 1}

        def update_message(self, **kw):
            return {}

        def forward_message(self, **kw):
            return {}

    listener._sender = _RecSender()
    listener._maybe_poll_source_view()
    # The new message produced a widget; the already-processed
    # one was skipped on the bookmark.
    assert sent, "expected at least one widget to be sent"


def test_listener_view_realtime_pulls_full_batch_size_per_poll(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-36 — listener pulls `view_poll_batch_size`
    rows in a single SQL roundtrip per poll. Already-processed
    rows short-circuit on the bookmark, so the actual work is
    bounded by «new since last poll», not by batch_size. The
    operator can bump VIEW_POLL_BATCH_SIZE if a deploy ever
    sees a burst bigger than the default 500."""
    from app.telegram_ingest.reader import TelegramSourceMessage

    captured_limits: list[int] = []

    class _Reader:
        configured = True

        def iter_newest(self, *, limit):
            captured_limits.append(limit)
            return iter([])

    ingest = _make_ingest(IntentClassification(
        intent=IntentType.no_action, confidence=0.0
    ))
    ingest._reader = _Reader()  # noqa: SLF001
    listener = TelegramListener(
        token="123:abc",
        ingest=ingest,
        view_realtime_enabled=True,
        view_poll_interval_seconds=1,
        view_poll_batch_size=500,
    )
    listener._maybe_poll_source_view()
    assert captured_limits == [500]


def test_listener_view_realtime_throttled_within_interval(
    patched_session_scope, SessionFactory
):
    """Repeated calls inside the poll window are no-ops."""
    from app.telegram_ingest.reader import TelegramSourceMessage

    iter_calls = {"n": 0}

    class _Counter:
        configured = True

        def iter_newest(self, *, limit):
            iter_calls["n"] += 1
            return iter([])

    ingest = _make_ingest(IntentClassification(
        intent=IntentType.no_action, confidence=0.0
    ))
    ingest._reader = _Counter()  # noqa: SLF001
    listener = TelegramListener(
        token="123:abc",
        ingest=ingest,
        view_realtime_enabled=True,
        view_poll_interval_seconds=600,
    )
    listener._maybe_poll_source_view()
    listener._maybe_poll_source_view()
    listener._maybe_poll_source_view()
    assert iter_calls["n"] == 1


def test_listener_view_realtime_no_op_when_reader_unconfigured():
    """No source DB URL ⇒ reader.configured=False ⇒ poll is a
    silent no-op even with the flag flipped on."""

    class _NoReader:
        configured = False

        def iter_newest(self, **kw):  # pragma: no cover
            raise AssertionError("must not be called")

    ingest = _make_ingest(IntentClassification(
        intent=IntentType.no_action, confidence=0.0
    ))
    ingest._reader = _NoReader()  # noqa: SLF001
    listener = TelegramListener(
        token="123:abc",
        ingest=ingest,
        view_realtime_enabled=True,
        view_poll_interval_seconds=1,
    )
    # Should not raise.
    listener._maybe_poll_source_view()


# --------------------------------------------------------------------------- #
# FR-CR-05-14 — voice messages in pending replies
# --------------------------------------------------------------------------- #


def test_maybe_transcribe_voice_returns_text_for_text_message():
    """FR-CR-05-14 — when the reply is plain text, transcription is
    skipped (the early-return guard). The shortcut keeps the
    common case fast."""
    from app.telegram_ingest.reader import TelegramSourceMessage

    listener = TelegramListener(token="123:abc", ingest=_make_ingest(
        IntentClassification(intent=IntentType.no_action, confidence=0.0)
    ))
    msg = TelegramSourceMessage(
        chat_id=1, message_id=2, text="Hello world", user_id=42, raw={}
    )
    assert listener._maybe_transcribe_voice(msg) == "Hello world"


def test_maybe_transcribe_voice_returns_empty_when_no_voice_no_audio():
    """A reply with neither text nor voice / audio attachment returns
    empty — caller can nudge the user."""
    from app.telegram_ingest.reader import TelegramSourceMessage

    listener = TelegramListener(token="123:abc", ingest=_make_ingest(
        IntentClassification(intent=IntentType.no_action, confidence=0.0)
    ))
    msg = TelegramSourceMessage(
        chat_id=1, message_id=2, text="", user_id=42, raw={}
    )
    assert listener._maybe_transcribe_voice(msg) == ""


def test_maybe_transcribe_voice_calls_whisper_with_downloaded_bytes(monkeypatch):
    """When the reply has a `voice` attachment, the listener pulls
    the bytes via the sender's `download_file_bytes` and feeds them
    to `transcribe_bytes`. Returns the resulting text."""
    from app.config import get_settings
    from app.telegram_ingest.reader import TelegramSourceMessage

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    try:
        listener = TelegramListener(token="123:abc", ingest=_make_ingest(
            IntentClassification(intent=IntentType.no_action, confidence=0.0)
        ))
        # Stub the sender's download path.
        listener._sender.download_file_bytes = lambda *, file_id: b"OGG-FAKE-BYTES"  # type: ignore[method-assign]

        captured: dict = {}

        def fake_transcribe(**kw):
            captured.update(kw)
            return "ответственный Андрей Кузьминых"

        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes", fake_transcribe
        )
        msg = TelegramSourceMessage(
            chat_id=1, message_id=2, text="", user_id=42,
            raw={"voice": {"file_id": "FILE-X", "duration": 3, "mime_type": "audio/ogg"}},
        )
        text = listener._maybe_transcribe_voice(msg)
        assert text == "ответственный Андрей Кузьминых"
        assert captured["audio_bytes"] == b"OGG-FAKE-BYTES"
        assert captured["mimetype"] == "audio/ogg"
        assert captured["openai_api_key"] == "sk-test"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_maybe_transcribe_voice_skips_when_openai_key_missing(monkeypatch):
    """No OPENAI_API_KEY → don't even try to download. Empty result."""
    from app.config import get_settings
    from app.telegram_ingest.reader import TelegramSourceMessage

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        listener = TelegramListener(token="123:abc", ingest=_make_ingest(
            IntentClassification(intent=IntentType.no_action, confidence=0.0)
        ))
        called = {"download": False}

        def must_not_call(**kw):
            called["download"] = True
            return b"x"

        listener._sender.download_file_bytes = must_not_call  # type: ignore[method-assign]
        msg = TelegramSourceMessage(
            chat_id=1, message_id=2, text="", user_id=42,
            raw={"voice": {"file_id": "F"}},
        )
        assert listener._maybe_transcribe_voice(msg) == ""
        assert called["download"] is False
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# FR-CR-05-44 — top-level voice / audio capture in DM
# --------------------------------------------------------------------------- #


def test_listener_tick_transcribes_voice_dm_and_creates_task(
    patched_session_scope, SessionFactory, monkeypatch
):
    """A voice message in a private DM (NOT a reply to a prompt)
    used to fall through with `text=""` and silently no-op. The
    listener now transcribes via Whisper and feeds the transcript
    into the ingest pipeline as if it were a normal text capture."""
    from app.config import get_settings

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        classification = IntentClassification(
            intent=IntentType.create_task,
            confidence=0.9,
            task=TaskDraft(title="купить молоко", description="Зайти в магазин по дороге домой."),
            reasoning="r",
        )
        listener = _make_listener(
            classification,
            updates_per_call=[
                [
                    {
                        "update_id": 200,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 7, "type": "private"},
                            "from": {"id": 1},
                            "voice": {"file_id": "F", "duration": 2},
                            "date": 0,
                        },
                    }
                ]
            ],
        )
        # Stub the audio download + Whisper.
        listener._sender.download_file_bytes = lambda *, file_id: b"OGG"  # type: ignore[method-assign]
        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes",
            lambda **kw: "купить молоко",
        )

        report = listener.tick()
        assert report.tasks_created == 1
        with SessionFactory() as s:
            t = s.query(Task).first()
            assert t is not None
            assert "молок" in (t.title or "").lower()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_listener_tick_voice_dm_no_transcript_sends_nudge(
    patched_session_scope, SessionFactory, monkeypatch
):
    """If the voice can't be transcribed (no key, empty bytes, etc.)
    AND the chat is a private DM, the listener tells the user
    explicitly so they don't think the bot ate their message."""
    from app.config import get_settings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        listener = _make_listener(
            IntentClassification(intent=IntentType.no_action, confidence=0.0),
            updates_per_call=[
                [
                    {
                        "update_id": 300,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 7, "type": "private"},
                            "from": {"id": 1},
                            "voice": {"file_id": "F"},
                            "date": 0,
                        },
                    }
                ]
            ],
        )
        sent: list[dict] = []
        listener._sender.send_message = lambda **kw: sent.append(kw) or {"message_id": 1}  # type: ignore[method-assign]

        report = listener.tick()
        assert report.tasks_created == 0
        nudge = sent[-1]
        assert "Couldn't transcribe the voice" in nudge["text"]
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# FR-CR-05-45 — /start welcome widget
# --------------------------------------------------------------------------- #


def test_listener_tick_responds_to_start_with_welcome_widget(
    patched_session_scope, SessionFactory
):
    """`/start` in a private DM gets the welcome widget back. The
    classifier is NOT called (the message isn't a task capture)
    and no Task row lands in the DB."""
    listener = _make_listener(
        IntentClassification(intent=IntentType.no_action, confidence=0.0),
        updates_per_call=[
            [
                {
                    "update_id": 400,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 1},
                        "text": "/start",
                        "date": 0,
                    },
                }
            ]
        ],
    )
    sent: list[dict] = []
    listener._sender.send_message = lambda **kw: sent.append(kw) or {"message_id": 1}  # type: ignore[method-assign]

    report = listener.tick()
    assert report.tasks_created == 0
    assert sent
    assert "Hi" in sent[0]["text"]
    assert "voice" in sent[0]["text"]
    with SessionFactory() as s:
        assert s.query(Task).count() == 0


def test_listener_tick_help_command_also_returns_welcome_widget(
    patched_session_scope, SessionFactory
):
    """`/help` is treated the same as `/start` so users who type the
    canonical Telegram help command get the same one-screen pitch."""
    listener = _make_listener(
        IntentClassification(intent=IntentType.no_action, confidence=0.0),
        updates_per_call=[
            [
                {
                    "update_id": 401,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 1},
                        "text": "/help",
                        "date": 0,
                    },
                }
            ]
        ],
    )
    sent: list[dict] = []
    listener._sender.send_message = lambda **kw: sent.append(kw) or {"message_id": 1}  # type: ignore[method-assign]
    listener.tick()
    assert sent and "task" in sent[0]["text"].lower()


def test_listener_tick_start_in_group_chat_falls_through_to_ingest(
    patched_session_scope, SessionFactory
):
    """`/start` in a GROUP chat is NOT a welcome trigger — groups
    don't need an onboarding widget. Falls through to the normal
    ingest path."""
    listener = _make_listener(
        IntentClassification(intent=IntentType.no_action, confidence=0.0),
        updates_per_call=[
            [
                {
                    "update_id": 402,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": -100, "type": "supergroup", "title": "Team"},
                        "from": {"id": 1},
                        "text": "/start",
                        "date": 0,
                    },
                }
            ]
        ],
    )
    sent: list[dict] = []
    listener._sender.send_message = lambda **kw: sent.append(kw) or {"message_id": 1}  # type: ignore[method-assign]
    listener.tick()
    # No welcome widget would have been posted to a group.
    assert not any("Hi" in m.get("text", "") for m in sent)


# --------------------------------------------------------------------------- #
# FR-CR-05-51 — Bot API «process from now» cutoff
# --------------------------------------------------------------------------- #


def test_listener_drops_pre_startup_bot_api_messages(
    patched_session_scope, SessionFactory
):
    """Telegram holds up to 24h of undelivered updates after a
    cold start. The listener pins a `_bot_api_started_at`
    cutoff on first tick and skips anything older — so a fresh
    deploy doesn't spawn task cards from yesterday's group
    chatter."""
    from datetime import datetime, timezone, timedelta

    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="x", description="x desc"),
        reasoning="r",
    )
    listener = _make_listener(
        classification,
        updates_per_call=[[]],  # one empty fetch — we'll inject manually
    )
    # Override the test's epoch cutoff with one set just now,
    # then feed a message dated 1h before that cutoff.
    cutoff = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    listener._bot_api_started_at = cutoff
    old_ts = int((cutoff - timedelta(hours=1)).timestamp())
    fresh_ts = int((cutoff + timedelta(minutes=5)).timestamp())
    listener._fetch_updates = lambda *, offset: [  # type: ignore[method-assign]
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 1},
                "text": "old task — was sent yesterday",
                "date": old_ts,
            },
        },
        {
            "update_id": 2,
            "message": {
                "message_id": 2,
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 1},
                "text": "fresh task — sent now",
                "date": fresh_ts,
            },
        },
    ]
    report = listener.tick()
    assert report.skipped_pre_startup == 1
    assert report.tasks_created == 1
    with SessionFactory() as s:
        titles = [t.title for t in s.query(Task).all()]
        # Only the fresh-task title was processed; the old one
        # never reached the classifier.
        assert any("fresh" in t for t in titles) or len(titles) == 1
