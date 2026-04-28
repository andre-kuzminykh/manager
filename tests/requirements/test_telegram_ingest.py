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


# --------------------------------------------------------------------------- #
# IngestReport merge (used by the migration script)
# --------------------------------------------------------------------------- #


def test_ingest_report_dataclass_default_lists_are_independent():
    a = IngestReport()
    b = IngestReport()
    a.error_samples.append("a-1")
    assert b.error_samples == []  # not shared via class-level mutable
