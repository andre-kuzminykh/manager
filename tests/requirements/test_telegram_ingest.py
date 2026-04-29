"""Requirement coverage: FR-CR-04-26 — Telegram channel ingest.

Tests cover:

- ``TelegramSourceReader`` maps a flexible Supabase row shape
  (different column names) into our internal `TelegramSourceMessage`.
- ``TelegramIngestService.process_one`` writes a Task with
  ``source_kind = 'telegram'`` when the pipeline returns
  ``create_task``, and a `processed_telegram_messages` row in
  every case (task or no_action).
- Idempotency: a second call on the same (chat_id, message_id) is
  a no-op.
- Empty / non-text messages are recorded as processed but yield no
  task.
- The historical migration script's batching helper merges report
  counters correctly.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.context.retriever import ContextWindow
from app.intent import IntentClassifier
from app.models import (
    ProcessedTelegramMessage,
    Task,
    TaskSourceKind,
)
from app.orchestrator import Orchestrator
from app.schemas.intent import (
    IntentClassification,
    IntentType,
    InvocationType,
    TaskDraft,
)
from app.telegram_ingest.reader import (
    TelegramSourceMessage,
    TelegramSourceReader,
    _map_row,
)
from app.telegram_ingest.service import (
    IngestReport,
    TelegramIngestService,
    _build_window,
    _telegram_permalink,
)


# --------------------------------------------------------------------------- #
# Reader: flexible row mapping
# --------------------------------------------------------------------------- #


def test_map_row_canonical_columns():
    out = _map_row(
        {
            "chat_id": -10012345,
            "message_id": 7,
            "text": "надо подготовить презу",
            "from_user_id": 42,
            "from_user_name": "andre",
            "date": datetime(2026, 4, 28, tzinfo=timezone.utc),
        }
    )
    assert out is not None
    assert out.chat_id == -10012345
    assert out.message_id == 7
    assert out.user_id == 42
    assert out.user_name == "andre"
    assert out.text == "надо подготовить презу"


def test_map_row_alternative_column_names():
    """The ingestion pipeline that fills the view might use different
    column names; the reader should be flexible enough to handle the
    common alternatives without extra config."""
    out = _map_row(
        {
            "chatid": 555,
            "messageid": 8,
            "body": "do X",
            "sender_id": 77,
            "username": "alice",
        }
    )
    assert out is not None
    assert out.chat_id == 555
    assert out.message_id == 8
    assert out.user_id == 77
    assert out.user_name == "alice"
    assert out.text == "do X"


def test_map_row_drops_when_no_identifiers():
    assert _map_row({"text": "orphan"}) is None
    assert _map_row({"chat_id": 1}) is None
    assert _map_row({"message_id": 1}) is None


def test_reader_unconfigured_yields_nothing():
    """Without a database URL the reader is disabled and `page` is
    a no-op iterator."""
    reader = TelegramSourceReader(database_url="")
    assert list(reader.page()) == []


def test_reader_resolves_ipv4_and_passes_hostaddr(monkeypatch):
    """Supabase free-tier hostnames resolve to IPv6 only, which kills
    GCE VMs without outbound IPv6. The reader must pre-resolve to
    IPv4 and pass `hostaddr` to libpq so the connection never tries
    an AAAA address."""
    captured: dict = {}

    def fake_create_engine(url, *args, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["connect_args"] = kwargs.get("connect_args", {})
        return object()

    def fake_resolve(database_url):  # noqa: ANN001
        return "203.0.113.7"

    monkeypatch.setattr("app.telegram_ingest.reader.create_engine", fake_create_engine)
    monkeypatch.setattr("app.telegram_ingest.reader._resolve_ipv4", fake_resolve)

    TelegramSourceReader(
        database_url="postgresql://u:p@db.example.supabase.co:5432/postgres",
        view_name="v",
    )
    assert captured["connect_args"].get("hostaddr") == "203.0.113.7"
    assert "default_transaction_read_only" in captured["connect_args"]["options"]


def test_reader_rewrites_postgresql_scheme_to_psycopg3(monkeypatch):
    """The image ships psycopg3 only — SQLAlchemy's default driver
    for the bare `postgresql://` scheme is psycopg2, which would
    crash with `ModuleNotFoundError`. The reader normalises both
    `postgresql://` and `postgres://` to `postgresql+psycopg://`.
    """
    captured: dict[str, str] = {}

    def fake_create_engine(url, *args, **kwargs):  # noqa: ANN001
        captured["url"] = url
        return object()  # we don't actually use it

    monkeypatch.setattr("app.telegram_ingest.reader.create_engine", fake_create_engine)

    TelegramSourceReader(
        database_url="postgresql://u:p@host:5432/db", view_name="v"
    )
    assert captured["url"].startswith("postgresql+psycopg://")

    captured.clear()
    TelegramSourceReader(database_url="postgres://u:p@host:5432/db", view_name="v")
    assert captured["url"].startswith("postgresql+psycopg://")

    captured.clear()
    TelegramSourceReader(
        database_url="postgresql+psycopg://u:p@host:5432/db", view_name="v"
    )
    # Already normalised — left alone.
    assert captured["url"].startswith("postgresql+psycopg://")


# --------------------------------------------------------------------------- #
# permalink
# --------------------------------------------------------------------------- #


def test_recent_in_chat_returns_chronological_with_char_cap(monkeypatch):
    """FR-CR-05-09 — the adaptive context window expands in
    increments of `step` until total chars >= max_chars (or we
    hit max_messages), then returns the captured slice in
    chronological order so the caller can hand it straight to
    `ContextWindow.history_before`."""

    captured_limits: list[int] = []
    # Synthesize a chat where each message is a fixed-length
    # string. With per-message length = 200 chars and max_chars =
    # 1000, we expect the loop to stop after 5 messages — well
    # before the first page of 10 finishes. (The implementation
    # truncates the in-memory list at the char threshold.)
    rows_per_message = [
        {
            "chat_id": -100,
            "message_id": 1000 - i,  # newest first by message_id
            "text": "x" * 200,
            "date": None,
            "from_user_id": 42,
            "from_user_name": "petya",
        }
        for i in range(40)  # 40 prior messages available
    ]

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return iter(self._rows)

        def keys(self):
            return ("chat_id", "message_id", "text", "date")

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "LIMIT 0" in str(sql):
                # _detect_columns probe.
                return _FakeResult([])
            lim = (params or {}).get("lim", 10)
            captured_limits.append(lim)
            return _FakeResult(rows_per_message[:lim])

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    reader = TelegramSourceReader(database_url="")
    reader._engine = _FakeEngine()  # type: ignore[attr-defined]
    # Override the columns cache to skip the probe and force the
    # message-id ORDER BY branch.
    reader._columns_cache = {"chat_id", "message_id", "text"}  # type: ignore[attr-defined]

    out = reader.recent_in_chat(
        chat_id=-100,
        before_message_id=2000,
        max_chars=1000,
        step=10,
        max_messages=50,
    )
    # 5 × 200 chars = 1000 → stop at exactly 5.
    assert len(out) == 5
    # Chronological — oldest first. The reader pulled rows
    # newest-first, then reversed before returning.
    assert out[0].message_id < out[-1].message_id
    # Should have stopped on the FIRST page since we hit the
    # threshold inside the first 10 messages.
    assert captured_limits == [10]


def test_recent_in_chat_expands_in_steps_when_under_threshold(monkeypatch):
    """When the first 10 messages aren't enough chars, the loop
    bumps the limit by `step` until we cross the threshold or hit
    `max_messages`."""

    captured_limits: list[int] = []
    # Tiny messages — each is 50 chars. Need 1000 chars → 20
    # messages. Step 10 means: try 10 (500 chars, not enough),
    # try 20 (1000 chars, hit).
    rows = [
        {
            "chat_id": -100,
            "message_id": 1000 - i,
            "text": "y" * 50,
            "date": None,
        }
        for i in range(40)
    ]

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return iter(self._rows)

        def keys(self):
            return ("chat_id", "message_id", "text")

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "LIMIT 0" in str(sql):
                return _FakeResult([])
            lim = (params or {}).get("lim", 10)
            captured_limits.append(lim)
            return _FakeResult(rows[:lim])

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    reader = TelegramSourceReader(database_url="")
    reader._engine = _FakeEngine()  # type: ignore[attr-defined]
    reader._columns_cache = {"chat_id", "message_id", "text"}  # type: ignore[attr-defined]

    out = reader.recent_in_chat(
        chat_id=-100,
        before_message_id=2000,
        max_chars=1000,
        step=10,
        max_messages=50,
    )
    # Tried 10 (insufficient), then 20 (hit threshold).
    assert captured_limits == [10, 20]
    assert len(out) == 20


def test_recent_in_chat_returns_empty_when_engine_unset():
    """Reader without a database URL just returns an empty list —
    the ingest service treats that as «no adaptive context»."""
    reader = TelegramSourceReader(database_url="")
    out = reader.recent_in_chat(
        chat_id=-1, before_message_id=1, max_chars=100
    )
    assert out == []


def test_telegram_permalink_for_supergroup():
    msg = TelegramSourceMessage(
        chat_id=-1001234567890, message_id=99, text="x"
    )
    assert _telegram_permalink(msg) == "https://t.me/c/1234567890/99"


def test_telegram_permalink_returns_none_for_private_chat():
    msg = TelegramSourceMessage(chat_id=42, message_id=1, text="x")
    assert _telegram_permalink(msg) is None


# --------------------------------------------------------------------------- #
# Window: TG message → ContextWindow
# --------------------------------------------------------------------------- #


def test_build_window_uses_telegram_ids_as_slack_shape_fields():
    msg = TelegramSourceMessage(
        chat_id=-100777,
        message_id=12,
        reply_to=10,
        user_id=99,
        text="давай к завтра подготовим",
    )
    w = _build_window(msg)
    assert isinstance(w, ContextWindow)
    assert w.conversation_id == "-100777"
    assert w.source_ts == "12"
    assert w.thread_ts == "10"
    assert w.source_message["text"] == "давай к завтра подготовим"
    assert w.source_message["user"] == "99"


def test_build_window_carries_history_before():
    """FR-CR-05-09 — adaptive context: when history_before is
    populated by the reader, it threads through to the
    ContextWindow and `flat_messages()` so detect / title / owner
    stages see chat history."""
    msg = TelegramSourceMessage(
        chat_id=-100,
        message_id=42,
        text="хорошо! напишу ему",
        user_id=99,
    )
    history = [
        {"ts": "40", "user": "111", "text": "надо ответить Андрею", "subtype": None},
        {"ts": "41", "user": "222", "text": "да, важно сегодня", "subtype": None},
    ]
    w = _build_window(msg, history_before=history)
    assert w.history_before == history
    flat = w.flat_messages()
    assert len(flat) == 3
    assert flat[0]["text"] == "надо ответить Андрею"
    assert flat[-1]["text"] == "хорошо! напишу ему"


# --------------------------------------------------------------------------- #
# Service: process_one
# --------------------------------------------------------------------------- #


class _StubClassifier:
    """Returns whatever IntentClassification we hand in. No LLM call."""

    def __init__(self, classification: IntentClassification) -> None:
        self._c = classification

    def classify(self, *, context, invocation_type, known_employees=None):
        return self._c


def _make_service(classification: IntentClassification) -> TelegramIngestService:
    from app.config import Settings

    return TelegramIngestService(
        classifier=_StubClassifier(classification),
        orchestrator=Orchestrator(Settings()),
    )


def test_process_one_uses_user_name_as_fallback_owner_display_name(
    patched_session_scope, SessionFactory
):
    """FR-CR-04-30: when the LLM didn't extract a display name (the
    common case for TG ingest with no employees table), the sender's
    user_name is used so cards / sheet show "Andre" instead of the
    raw numeric user id."""
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="prepare deck"),  # no owner_display_name
        reasoning="...",
    )
    service = _make_service(classification)
    msg = TelegramSourceMessage(
        chat_id=-100,
        message_id=1,
        text="prepare deck for tomorrow",
        user_id=222968032,
        user_name="Andre",
    )
    with SessionFactory() as s:
        task = service.process_one(s, msg)
        s.commit()
        assert task is not None
        assert task.owner_display_name == "Andre"
        # FR-CR-04-30 regression: setting owner_display_name from
        # user_name must NOT leave owner_user_id empty — without an
        # owner id the task card renders only the bystander Subscribe
        # button (is_owner never matches None).
        assert task.owner_user_id == "222968032"


def test_process_one_keeps_llm_display_name_when_present(
    patched_session_scope, SessionFactory
):
    """If the LLM did extract a display name, the fallback must NOT
    overwrite it."""
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="x", owner_display_name="From LLM"),
        reasoning="...",
    )
    service = _make_service(classification)
    msg = TelegramSourceMessage(
        chat_id=-100,
        message_id=2,
        text="x",
        user_id=42,
        user_name="ShouldNotWin",
    )
    with SessionFactory() as s:
        task = service.process_one(s, msg)
        s.commit()
        assert task.owner_display_name == "From LLM"


def test_process_all_creates_one_task_per_chunk(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-05: a classification carrying ``tasks=[a, b]`` lands
    as TWO Task rows from a single message. Both share the same
    source bookmark; the bookmark's `task_id` points at the first."""
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        tasks=[
            TaskDraft(title="prepare deck"),
            TaskDraft(title="write report"),
        ],
        reasoning="two-tasks message",
    )
    service = _make_service(classification)
    msg = TelegramSourceMessage(
        chat_id=-100,
        message_id=42,
        text="prepare deck and write report",
        user_id=222968032,
        user_name="Andre",
    )
    with SessionFactory() as s:
        out = service.process_all(s, msg)
        s.commit()
        assert len(out) == 2
        assert {t.title for t in out} == {"prepare deck", "write report"}
        # Bookmark singular per message — points at the first task.
        bookmark = s.get(ProcessedTelegramMessage, (-100, 42))
        assert bookmark is not None
        assert bookmark.task_id == out[0].id


def test_prepare_drafts_creates_one_draft_per_chunk(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-05 + FR-CR-04-32: a multi-task message in a group
    creates one ``ActionDraft`` per detected task; each draft carries
    its own ``_pending`` block so the listener can DM a separate
    confirm widget per task."""
    from app.models import ActionDraft, ActionDraftState

    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        tasks=[
            TaskDraft(title="prepare deck"),
            TaskDraft(title="write report"),
        ],
    )
    service = _make_service(classification)
    msg = TelegramSourceMessage(
        chat_id=-2002,
        message_id=11,
        text="prep deck and report",
        user_id=42,
        user_name="Andre",
    )
    with SessionFactory() as s:
        drafts = service.prepare_drafts(s, msg)
        s.commit()
        assert len(drafts) == 2
        assert {d.payload.get("title") for d in drafts} == {
            "prepare deck",
            "write report",
        }
        for d in drafts:
            assert d.state == ActionDraftState.proposed
            pending = (d.payload or {}).get("_pending")
            assert pending is not None
            assert pending["source_chat_id"] == -2002
            assert pending["source_message_id"] == 11


# --------------------------------------------------------------------------- #
# FR-CR-05-09 — owner fallback chain + source-text stash
# --------------------------------------------------------------------------- #


def test_process_all_falls_back_to_admin_when_owner_unresolved(
    patched_session_scope, SessionFactory, monkeypatch
):
    """LLM didn't extract an owner; the chat-members registry IS
    populated for this chat but the sender is NOT in it (typical
    bot-account / forwarded-post case); an admin is configured →
    owner = the first admin from `TELEGRAM_ADMIN_USER_IDS` rather
    than the unknown sender.

    The presence of known_employees is what enables the bot-account
    suppression — without it (registry not yet populated for this
    chat) we still fall back to the sender per FR-CR-04-30 to avoid
    leaving every task ownerless on a fresh deploy.
    """
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777,888")
    from app.config import get_settings
    from app.services.telegram_members import upsert_member

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        classification = IntentClassification(
            intent=IntentType.create_task,
            confidence=0.9,
            task=TaskDraft(title="ping the customer"),  # no owner
            reasoning="...",
        )
        service = _make_service(classification)
        msg = TelegramSourceMessage(
            chat_id=-100,
            message_id=1,
            text="напишу ему",
            user_id=9999,  # not a registered member
            user_name="bot_account",
        )
        with SessionFactory() as s:
            # Populate the registry with at least one OTHER user so
            # the «empty registry → keep author fallback» branch is
            # explicitly NOT taken. Sender 9999 still isn't in it,
            # so the admin fallback should fire.
            upsert_member(s, chat_id=-100, user_id=42, username="petya")
            s.flush()
            task = service.process_one(s, msg)
            s.commit()
            assert task is not None
            # First admin from env wins.
            assert task.owner_user_id == "777"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_process_all_keeps_real_member_sender_as_owner(
    patched_session_scope, SessionFactory, monkeypatch
):
    """When the sender IS a registered chat member, the legacy
    «author fallback» wins — no admin promotion. This preserves
    FR-CR-04-30 behaviour for normal team traffic."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings
    from app.services.telegram_members import upsert_member

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        classification = IntentClassification(
            intent=IntentType.create_task,
            confidence=0.9,
            task=TaskDraft(title="prep deck"),  # no owner
            reasoning="...",
        )
        service = _make_service(classification)
        msg = TelegramSourceMessage(
            chat_id=-100,
            message_id=2,
            text="prep deck",
            user_id=42,
            user_name="@petya",
        )
        with SessionFactory() as s:
            # Register the sender so they're a known member.
            upsert_member(s, chat_id=-100, user_id=42, username="petya")
            s.flush()
            task = service.process_one(s, msg)
            s.commit()
            assert task is not None
            assert task.owner_user_id == "42"
            assert task.owner_display_name == "@petya"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_prepare_drafts_stashes_source_text_for_quote_fallback(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-09 — every prepared draft carries the raw source
    text on `_pending["source_text"]` so `post_draft_confirmation`
    can fall back to an inline quote when forwardMessage fails
    (which it does for every historical-migration draft — the bot
    never observed those messages)."""
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="написать"),
        reasoning="...",
    )
    service = _make_service(classification)
    msg = TelegramSourceMessage(
        chat_id=-100,
        message_id=99,
        text="хорошо! напишу ему",
        user_id=42,
    )
    with SessionFactory() as s:
        drafts = service.prepare_drafts(s, msg)
        s.commit()
        assert len(drafts) == 1
        pending = (drafts[0].payload or {}).get("_pending") or {}
        assert pending.get("source_text") == "хорошо! напишу ему"
        assert pending.get("source_chat_id") == -100
        assert pending.get("source_message_id") == 99


def test_process_one_creates_task_with_telegram_source_kind(
    patched_session_scope, SessionFactory
):
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.92,
        task=TaskDraft(title="prepare deck"),
        reasoning="...",
    )
    service = _make_service(classification)

    msg = TelegramSourceMessage(
        chat_id=-1001234567890,
        message_id=42,
        text="prepare deck for tomorrow",
        user_id=99,
    )

    with SessionFactory() as s:
        task = service.process_one(s, msg)
        s.commit()
        assert task is not None
        tid = task.id

    with SessionFactory() as s:
        task = s.get(Task, tid)
        assert task is not None
        assert task.source_kind == TaskSourceKind.telegram
        assert task.source_conversation_id == "-1001234567890"
        assert task.source_message_ts == "42"
        assert task.source_permalink == "https://t.me/c/1234567890/42"
        # And the ingest bookmark exists.
        bm = s.get(ProcessedTelegramMessage, (-1001234567890, 42))
        assert bm is not None
        assert bm.task_id == tid


def test_process_one_records_no_action_without_creating_a_task(
    patched_session_scope, SessionFactory
):
    classification = IntentClassification(
        intent=IntentType.no_action,
        confidence=0.1,
        reasoning="not a task",
    )
    service = _make_service(classification)
    msg = TelegramSourceMessage(
        chat_id=-1, message_id=5, text="спасибо!"
    )

    with SessionFactory() as s:
        out = service.process_one(s, msg)
        s.commit()
        assert out is None

    with SessionFactory() as s:
        bm = s.get(ProcessedTelegramMessage, (-1, 5))
        assert bm is not None
        assert bm.task_id is None
        assert s.query(Task).count() == 0


def test_process_one_is_idempotent_on_repeat(
    patched_session_scope, SessionFactory
):
    """Re-processing the same (chat, msg) pair must NOT create a
    second task or a duplicate bookmark."""
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="x"),
        reasoning="...",
    )
    service = _make_service(classification)
    msg = TelegramSourceMessage(chat_id=1, message_id=1, text="prepare a report")

    with SessionFactory() as s:
        first = service.process_one(s, msg)
        s.commit()
        assert first is not None

    with SessionFactory() as s:
        second = service.process_one(s, msg)
        assert second is None  # bookmark hit → no-op
        s.commit()

    with SessionFactory() as s:
        assert s.query(Task).count() == 1
        assert s.query(ProcessedTelegramMessage).count() == 1


def test_process_one_skips_empty_text(
    patched_session_scope, SessionFactory
):
    """Sticker / photo-only messages have no text to extract from;
    we record the bookmark so the cron doesn't reprocess but we
    create no task and don't even call the classifier."""
    called: list[bool] = []

    class _Tracking(_StubClassifier):
        def classify(self, **kw):  # type: ignore[override]
            called.append(True)
            return super().classify(**kw)

    from app.config import Settings

    service = TelegramIngestService(
        classifier=_Tracking(
            IntentClassification(
                intent=IntentType.no_action, confidence=0.0
            )
        ),
        orchestrator=Orchestrator(Settings()),
    )
    msg = TelegramSourceMessage(chat_id=2, message_id=3, text="   ")

    with SessionFactory() as s:
        assert service.process_one(s, msg) is None
        s.commit()

    assert called == []  # classifier never invoked
    with SessionFactory() as s:
        assert s.get(ProcessedTelegramMessage, (2, 3)) is not None


# --------------------------------------------------------------------------- #
# Service: process_batch report counters
# --------------------------------------------------------------------------- #


def test_process_batch_counts_each_outcome(
    patched_session_scope, SessionFactory
):
    classification = IntentClassification(
        intent=IntentType.create_task,
        confidence=0.9,
        task=TaskDraft(title="x"),
        reasoning="r",
    )
    service = _make_service(classification)

    msgs = [
        TelegramSourceMessage(chat_id=1, message_id=1, text="prepare a report"),
        TelegramSourceMessage(chat_id=1, message_id=2, text=""),
        TelegramSourceMessage(chat_id=1, message_id=3, text="another task"),
    ]

    with SessionFactory() as s:
        report = service.process_batch(s, msgs)
        s.commit()

    assert report.seen == 3
    assert report.tasks_created == 2
    assert report.skipped_empty_text == 1


def test_process_batch_re_run_with_same_empty_text_message_doesnt_dup_pk(
    patched_session_scope, SessionFactory
):
    """Regression — running ``process_batch`` twice over the same
    non-textual message used to crash on
    ``processed_telegram_messages_pkey``: the first run inserted a
    bookmark with `task_id=None`, the second run hit the same row
    again because the empty-text branch wrote without first checking
    `existing`. Now both runs converge — second pass counts as
    `skipped_already_processed`."""
    classification = IntentClassification(
        intent=IntentType.no_action,
        confidence=0.0,
    )
    service = _make_service(classification)
    msgs = [TelegramSourceMessage(chat_id=99, message_id=99, text="")]
    with SessionFactory() as s:
        first = service.process_batch(s, msgs)
        s.commit()
    with SessionFactory() as s:
        second = service.process_batch(s, msgs)
        s.commit()
    assert first.skipped_empty_text == 1
    assert second.skipped_already_processed == 1
    assert second.skipped_empty_text == 0


# --------------------------------------------------------------------------- #
# IngestReport merge (used by the migration script)
# --------------------------------------------------------------------------- #


def test_ingest_report_dataclass_default_lists_are_independent():
    a = IngestReport()
    b = IngestReport()
    a.error_samples.append("a-1")
    assert b.error_samples == []  # not shared via class-level mutable
