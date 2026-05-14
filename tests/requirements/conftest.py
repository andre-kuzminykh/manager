"""Shared fixtures for per-requirement tests.

The handlers call `session_scope()` directly; we monkeypatch every place that
imports it so tests can use a single in-memory SQLite DB without touching the
real environment. We also provide a deterministic stub classifier so tests
don't depend on an LLM.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.schemas.intent import (
    IntentClassification,
    IntentType,
    InvocationType,
    MeetingDraft,
    TaskDraft,
)


# --------------------------------------------------------------------------- #
# DB fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def engine():
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def SessionFactory(engine):  # noqa: N802
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def session(SessionFactory) -> Session:  # noqa: N803
    s = SessionFactory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def patched_session_scope(monkeypatch, SessionFactory):  # noqa: N803
    """Replace `session_scope()` in every handler module with one that uses the
    shared in-memory engine."""

    @contextlib.contextmanager
    def fake_scope():
        s = SessionFactory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    targets = [
        "app.db.session_scope",
        "app.slack_bot.handlers.shared.session_scope",
        "app.slack_bot.handlers.events.session_scope",
        "app.slack_bot.handlers.shortcuts.session_scope",
        "app.slack_bot.handlers.views.session_scope",
        "app.slack_bot.handlers.actions.session_scope",
        "app.slack_bot.handlers.task_actions.session_scope",
        "app.slack_bot.handlers.admin_review.session_scope",
        "app.slack_bot.handlers.weekly_plan.session_scope",
        "app.slack_bot.handlers.daily_plan.session_scope",
        "app.orchestrator.finalize.session_scope",
        "app.sync.factories.session_scope",
        "app.sync.task_sync.session_scope",
        "app.telegram_bot.listener.session_scope",
        "ops.brief_run_once.session_scope",
        "app.counterparty_briefs.runner.session_scope",
    ]
    for t in targets:
        monkeypatch.setattr(t, fake_scope, raising=False)

    return fake_scope


# --------------------------------------------------------------------------- #
# Classifier stub
# --------------------------------------------------------------------------- #


class StubClassifier:
    """Deterministic classifier: returns a preset IntentClassification.

    If `auto=True`, derives a classification from the source text using simple
    heuristics (title = the text itself). This is useful for tests that only
    care that *some* draft is produced.
    """

    def __init__(
        self,
        result: IntentClassification | None = None,
        *,
        auto: bool = False,
    ) -> None:
        self.result = result
        self.auto = auto
        self.calls: list[tuple[Any, InvocationType]] = []

    def classify(
        self,
        *,
        context,
        invocation_type: InvocationType,
        known_employees=None,
    ) -> IntentClassification:
        self.calls.append((context, invocation_type))
        if self.result is not None:
            return self.result

        if self.auto:
            text = (context.source_message.get("text") or "").strip()
            if not text:
                return IntentClassification(intent=IntentType.no_action, confidence=0.0)
            lower = text.lower()
            if any(k in lower for k in ("meeting", "созвон", "встреч", "sync ", "call")):
                return IntentClassification(
                    intent=IntentType.create_meeting,
                    confidence=0.9,
                    meeting=MeetingDraft(title=text[:64]),
                )
            return IntentClassification(
                intent=IntentType.create_task,
                confidence=0.9,
                task=TaskDraft(title=text[:64]),
            )

        return IntentClassification(intent=IntentType.no_action, confidence=0.0)


@pytest.fixture()
def stub_classifier_task():
    return StubClassifier(
        IntentClassification(
            intent=IntentType.create_task,
            confidence=0.92,
            task=TaskDraft(
                title="Prepare report",
                description="details",
                owner_display_name="@alice",
                priority="high",
                due_date=None,
            ),
        )
    )


@pytest.fixture()
def stub_classifier_meeting():
    return StubClassifier(
        IntentClassification(
            intent=IntentType.create_meeting,
            confidence=0.92,
            meeting=MeetingDraft(
                title="Product sync",
                participants=["@ivan"],
                datetime_at=None,
            ),
        )
    )


@pytest.fixture()
def stub_classifier_auto():
    return StubClassifier(auto=True)


@pytest.fixture()
def stub_classifier_silent():
    return StubClassifier(
        IntentClassification(intent=IntentType.no_action, confidence=0.1)
    )


@pytest.fixture()
def stub_classifier_medium():
    return StubClassifier(
        IntentClassification(
            intent=IntentType.create_task,
            confidence=0.55,
            task=TaskDraft(title="Maybe task"),
        )
    )


# --------------------------------------------------------------------------- #
# Fakes mimicking Slack / Bolt
# --------------------------------------------------------------------------- #


class RecordingAck:
    """Fake Bolt ack() that remembers invocation order and payload."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args, **kwargs) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})

    @property
    def called(self) -> bool:
        return bool(self.calls)


@dataclass
class FakeBoltContext:
    bot_user_id: str | None = "UBOT"


@dataclass
class FakeSlackClient:
    permalink: str | None = "https://slack.com/archives/C1/p1"
    history_messages: list[dict[str, Any]] = field(default_factory=list)
    replies_messages: list[dict[str, Any]] = field(default_factory=list)
    views_opened: list[dict[str, Any]] = field(default_factory=list)
    posted_messages: list[dict[str, Any]] = field(default_factory=list)
    posted_ephemerals: list[dict[str, Any]] = field(default_factory=list)

    def conversations_history(self, channel, latest, limit, inclusive):
        return {"messages": list(self.history_messages[:limit])}

    def conversations_replies(self, channel, ts, limit):
        return {"messages": list(self.replies_messages)}

    def chat_getPermalink(self, channel, message_ts):  # noqa: N802
        return {"permalink": self.permalink}

    def views_open(self, trigger_id, view):
        self.views_opened.append({"trigger_id": trigger_id, "view": view})
        return {"ok": True}

    def chat_postMessage(self, **kwargs):  # noqa: N802
        self.posted_messages.append(kwargs)

        class _R:
            data = {"ok": True, "ts": "0.0"}

        return _R()

    def chat_postEphemeral(self, **kwargs):  # noqa: N802
        self.posted_ephemerals.append(kwargs)
        return {"ok": True}


@dataclass
class RecordingSender:
    posted: list[dict[str, Any]] = field(default_factory=list)

    def post_message(self, **kwargs) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True}


@pytest.fixture()
def bolt_context() -> FakeBoltContext:
    return FakeBoltContext()


@pytest.fixture()
def slack_client() -> FakeSlackClient:
    return FakeSlackClient()


@pytest.fixture()
def sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture()
def ack() -> RecordingAck:
    return RecordingAck()


def _make_services(slack_client, classifier):
    from app.config import Settings
    from app.context import ContextRetriever
    from app.orchestrator import Orchestrator
    from app.slack_bot.handlers.shared import Services

    settings = Settings()
    return Services(
        slack=slack_client,
        context_retriever=ContextRetriever(slack_client, window_before=10),
        classifier=classifier,
        orchestrator=Orchestrator(settings),
    )


@pytest.fixture()
def services(slack_client, stub_classifier_auto):
    """Default services fixture uses the auto stub classifier."""
    return _make_services(slack_client, stub_classifier_auto)


@pytest.fixture()
def services_task(slack_client, stub_classifier_task):
    return _make_services(slack_client, stub_classifier_task)


@pytest.fixture()
def services_meeting(slack_client, stub_classifier_meeting):
    return _make_services(slack_client, stub_classifier_meeting)


@pytest.fixture()
def services_silent(slack_client, stub_classifier_silent):
    return _make_services(slack_client, stub_classifier_silent)


@pytest.fixture()
def services_medium(slack_client, stub_classifier_medium):
    return _make_services(slack_client, stub_classifier_medium)


# --------------------------------------------------------------------------- #
# Finalizer stub (used in FR-11, FR-12, NFR-10 tests)
# --------------------------------------------------------------------------- #


class RecordingFinalizer:
    def __init__(self, entity_type: str = "task") -> None:
        self.calls: list[tuple[int, dict[str, Any]]] = []
        self.entity_type = entity_type
        self.raise_error: Exception | None = None

    def finalize_draft(self, *, draft_id: int, source_metadata: dict[str, Any]):
        self.calls.append((draft_id, source_metadata))
        if self.raise_error is not None:
            raise self.raise_error
        return self.entity_type, 42, "summary"


@pytest.fixture()
def finalizer_stub():
    return RecordingFinalizer()
