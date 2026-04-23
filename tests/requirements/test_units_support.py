"""Dense unit tests for supporting modules used in multiple FR/NFRs:

- app/config.py (Settings)
- app/slack_bot/blocks.py (card/modal builders across intents)
- app/slack_bot/handlers/shared.py (metadata helpers)
- app/intent/prompts.py (system prompt + user prompt builder)
- app/intent/classifier.py (tool schema + tool_use parser)
- app/schemas/intent.py (IntentClassification.draft_payload)
- app/sync/google_auth.py (TokenCipher roundtrip)
- app/sync/sheets.py (row rendering)
- app/sync/tasks_api.py (body rendering)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.schemas.intent import (
    IntentClassification,
    IntentType,
    MeetingDraft,
    TaskDraft,
)
from app.slack_bot import blocks as bk


# =============================================================================
# Settings
# =============================================================================


def test_settings_default_values():
    s = Settings()
    assert s.app_env == "development"
    assert s.context_window_before == 10
    assert 0.0 < s.intent_confidence_low < s.intent_confidence_high <= 1.0
    assert s.google_tasks_default_tasklist_id == "@default"


def test_settings_accepts_alias_env_vars(monkeypatch):
    monkeypatch.setenv("INTENT_CONFIDENCE_HIGH", "0.91")
    monkeypatch.setenv("INTENT_CONFIDENCE_LOW", "0.3")
    s = Settings()
    assert s.intent_confidence_high == 0.91
    assert s.intent_confidence_low == 0.3


def test_settings_ignores_unknown_env():
    # extra="ignore" means rogue env vars don't break instantiation
    s = Settings(UNRELATED_VAR="whatever")
    assert s.app_env == "development"


# =============================================================================
# Block Kit — draft cards
# =============================================================================


@pytest.mark.parametrize(
    "intent, has_task, has_meeting, expected_header",
    [
        (IntentType.create_task, True, False, "Task draft"),
        (IntentType.update_task, True, False, "Task draft"),
        (IntentType.create_meeting, False, True, "Meeting draft"),
        (IntentType.update_meeting, False, True, "Meeting draft"),
        (IntentType.no_action, False, False, "Task draft"),
    ],
)
def test_draft_card_header_per_intent(intent, has_task, has_meeting, expected_header):
    c = IntentClassification(
        intent=intent,
        confidence=0.8,
        task=TaskDraft(title="t") if has_task else None,
        meeting=MeetingDraft(title="m") if has_meeting else None,
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket="high")
    header = next(b for b in blocks if b["type"] == "header")
    assert header["text"]["text"] == expected_header


def test_draft_card_embeds_confidence_in_context_block():
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.78, task=TaskDraft(title="t")
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket="high")
    ctx = next(b for b in blocks if b["type"] == "context")
    rendered = ctx["elements"][0]["text"]
    assert "0.78" in rendered
    assert "high confidence" in rendered


@pytest.mark.parametrize("bucket_label", ["high", "medium", "low", "unexpected"])
def test_draft_card_confidence_bucket_labels_do_not_crash(bucket_label):
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.5, task=TaskDraft(title="t")
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket=bucket_label)
    assert blocks


def test_draft_card_renders_meeting_fields():
    c = IntentClassification(
        intent=IntentType.create_meeting,
        confidence=0.9,
        meeting=MeetingDraft(
            title="Sync",
            participants=["@ivan", "@alice"],
            datetime_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        ),
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket="high")
    section = next(b for b in blocks if b["type"] == "section" and "fields" in b)
    text = "\n".join(f["text"] for f in section["fields"])
    assert "Sync" in text
    assert "@ivan" in text and "@alice" in text


def test_draft_card_empty_fields_render_dashes():
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.9, task=TaskDraft(title="only-title")
    )
    blocks = bk.draft_card(classification=c, draft_id=1, confidence_bucket="high")
    section = next(b for b in blocks if b["type"] == "section" and "fields" in b)
    text = "\n".join(f["text"] for f in section["fields"])
    assert "—" in text


def test_soft_prompt_has_correct_copy_for_task():
    blocks = bk.soft_prompt(IntentType.create_task, draft_id=1)
    text = blocks[0]["text"]["text"]
    assert "задача" in text.lower()


def test_soft_prompt_has_correct_copy_for_meeting():
    blocks = bk.soft_prompt(IntentType.create_meeting, draft_id=1)
    text = blocks[0]["text"]["text"]
    assert "встреча" in text.lower()


def test_soft_prompt_has_correct_copy_for_update_task():
    blocks = bk.soft_prompt(IntentType.update_task, draft_id=1)
    text = blocks[0]["text"]["text"]
    assert "задачи" in text.lower() or "задача" in text.lower()


def test_soft_prompt_draft_id_embedded_in_actions():
    blocks = bk.soft_prompt(IntentType.create_task, draft_id=77)
    actions = next(b for b in blocks if b["type"] == "actions")
    for el in actions["elements"]:
        assert el["value"] == "77"


# =============================================================================
# Block Kit — success / failure messages
# =============================================================================


@pytest.mark.parametrize("entity_type", ["task", "meeting"])
def test_success_message_mentions_entity_type_and_id(entity_type):
    blocks = bk.success_message(entity_type, 101, "hello")
    text = blocks[0]["text"]["text"]
    assert entity_type.capitalize() in text
    assert "101" in text
    assert "hello" in text


@pytest.mark.parametrize("entity_type", ["task", "meeting", "entity"])
def test_failure_message_includes_error(entity_type):
    blocks = bk.failure_message(entity_type, "boom")
    text = blocks[0]["text"]["text"]
    assert "boom" in text
    assert entity_type in text


# =============================================================================
# Shared helpers
# =============================================================================


def test_draft_private_metadata_roundtrip():
    from app.slack_bot.handlers.shared import (
        draft_private_metadata,
        load_private_metadata,
    )

    raw = draft_private_metadata(
        conversation_id="C1",
        message_ts="1.0",
        thread_ts="0.5",
        draft_id=7,
        context_snapshot_id=12,
        source_user_id="U1",
        permalink="https://x",
    )
    parsed = load_private_metadata(raw)
    assert parsed["conversation_id"] == "C1"
    assert parsed["draft_id"] == 7
    assert parsed["permalink"] == "https://x"


def test_load_private_metadata_handles_none():
    from app.slack_bot.handlers.shared import load_private_metadata

    assert load_private_metadata(None) == {}
    assert load_private_metadata("") == {}
    assert load_private_metadata("{not json") == {}


def test_fetch_permalink_returns_none_on_error():
    from app.slack_bot.handlers.shared import fetch_permalink

    class Cli:
        def chat_getPermalink(self, channel, message_ts):  # noqa: N802
            raise RuntimeError("nope")

    assert fetch_permalink(Cli(), channel="C1", ts="1.0") is None


def test_fetch_permalink_returns_value_on_success():
    from app.slack_bot.handlers.shared import fetch_permalink

    class Cli:
        def chat_getPermalink(self, channel, message_ts):  # noqa: N802
            return {"permalink": "https://ok"}

    assert fetch_permalink(Cli(), channel="C1", ts="1.0") == "https://ok"


# =============================================================================
# Prompts
# =============================================================================


def test_system_prompt_mentions_all_intents():
    from app.intent.prompts import SYSTEM_PROMPT

    for intent in ("create_task", "create_meeting", "update_task", "update_meeting", "no_action"):
        assert intent in SYSTEM_PROMPT


def test_user_prompt_contains_source_and_context():
    from app.intent.prompts import build_user_prompt

    ctx_msgs = [
        {"ts": "1.0", "user": "U2", "text": "hi"},
        {"ts": "1.5", "user": "U1", "text": "there"},
    ]
    prompt = build_user_prompt(
        source_text="please do X",
        context_messages=ctx_msgs,
        invocation_type="mention",
        current_date="2026-04-23",
    )
    assert "current_date: 2026-04-23" in prompt
    assert "invocation_type: mention" in prompt
    assert "please do X" in prompt
    assert "hi" in prompt
    assert "there" in prompt


def test_user_prompt_handles_empty_context():
    from app.intent.prompts import build_user_prompt

    prompt = build_user_prompt(
        source_text="x",
        context_messages=[],
        invocation_type="passive",
        current_date="2026-01-01",
    )
    assert "source_message:" in prompt


# =============================================================================
# Classifier parsing helpers
# =============================================================================


def test_classifier_parse_handles_missing_task_field():
    from app.intent.classifier import _parse_classification

    c = _parse_classification({"intent": "create_task", "confidence": 0.8})
    assert c.intent == IntentType.create_task
    assert c.task is None


def test_classifier_parse_handles_full_meeting_payload():
    from app.intent.classifier import _parse_classification

    c = _parse_classification(
        {
            "intent": "create_meeting",
            "confidence": 0.9,
            "meeting": {
                "title": "Sync",
                "notes": "catch up",
                "participants": ["@a", "@b"],
                "datetime_at": "2026-06-01T10:00:00+00:00",
                "timezone": "UTC",
            },
        }
    )
    assert c.meeting.title == "Sync"
    assert c.meeting.participants == ["@a", "@b"]
    assert c.meeting.datetime_at == datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def test_classifier_parse_invalid_data_falls_back_to_no_action():
    from app.intent.classifier import _parse_classification

    c = _parse_classification({"intent": "not_a_real_intent"})
    assert c.intent == IntentType.no_action


def test_classifier_parse_ignores_extra_fields():
    from app.intent.classifier import _parse_classification

    c = _parse_classification(
        {"intent": "create_task", "confidence": 0.7, "task": {"title": "t"}, "noise": 42}
    )
    assert c.task.title == "t"


def test_classifier_extract_tool_input_object():
    from app.intent.classifier import _extract_tool_input

    class Block:
        type = "tool_use"
        input = {"intent": "no_action", "confidence": 0.1}

    class Resp:
        content = [Block()]

    assert _extract_tool_input(Resp()) == {"intent": "no_action", "confidence": 0.1}


def test_classifier_extract_tool_input_string_json():
    from app.intent.classifier import _extract_tool_input

    class Block:
        type = "tool_use"
        input = '{"intent": "create_task", "confidence": 0.9}'

    class Resp:
        content = [Block()]

    out = _extract_tool_input(Resp())
    assert out["intent"] == "create_task"


def test_classifier_extract_tool_input_dict_block():
    from app.intent.classifier import _extract_tool_input

    resp = type(
        "R",
        (),
        {"content": [{"type": "tool_use", "input": {"intent": "no_action", "confidence": 0.0}}]},
    )()
    out = _extract_tool_input(resp)
    assert out == {"intent": "no_action", "confidence": 0.0}


def test_classifier_extract_tool_input_no_match_returns_none():
    from app.intent.classifier import _extract_tool_input

    resp = type("R", (), {"content": [{"type": "text", "text": "hi"}]})()
    assert _extract_tool_input(resp) is None


def test_classifier_with_failing_llm_falls_back_to_rules():
    from app.intent import IntentClassifier
    from app.context.retriever import ContextWindow
    from app.schemas.intent import InvocationType

    stub_client = MagicMock()
    stub_client.messages.create.side_effect = RuntimeError("boom")

    classifier = IntentClassifier(anthropic_client=stub_client)
    ctx = ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "надо задачу сделать", "user": "U1"},
    )
    result = classifier.classify(context=ctx, invocation_type=InvocationType.mention)
    assert result.intent == IntentType.create_task  # rule prefilter kicked in
    assert "fail" in (result.reasoning or "").lower() or "rules" in (result.reasoning or "").lower()


def test_classifier_llm_no_tool_use_returns_no_action():
    from app.intent import IntentClassifier
    from app.context.retriever import ContextWindow
    from app.schemas.intent import InvocationType

    class StubMessages:
        def create(self, **kwargs):
            class Resp:
                content = [{"type": "text", "text": "sorry"}]

            return Resp()

    class StubClient:
        messages = StubMessages()

    classifier = IntentClassifier(anthropic_client=StubClient())
    ctx = ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "надо задачу", "user": "U1"},
    )
    result = classifier.classify(context=ctx, invocation_type=InvocationType.mention)
    assert result.intent == IntentType.no_action


def test_classifier_llm_returns_parsed_classification():
    from app.intent import IntentClassifier
    from app.context.retriever import ContextWindow
    from app.schemas.intent import InvocationType

    class StubMessages:
        def create(self, **kwargs):
            resp = MagicMock()
            resp.content = [
                type(
                    "Block",
                    (),
                    {
                        "type": "tool_use",
                        "input": {
                            "intent": "create_task",
                            "confidence": 0.95,
                            "task": {"title": "Do it", "priority": "high"},
                        },
                    },
                )()
            ]
            return resp

    class StubClient:
        messages = StubMessages()

    classifier = IntentClassifier(anthropic_client=StubClient())
    ctx = ContextWindow(
        conversation_id="C1",
        source_ts="1.0",
        thread_ts=None,
        source_message={"ts": "1.0", "text": "please build X", "user": "U1"},
    )
    result = classifier.classify(context=ctx, invocation_type=InvocationType.mention)
    assert result.intent == IntentType.create_task
    assert result.confidence == 0.95
    assert result.task.title == "Do it"


# =============================================================================
# Schemas
# =============================================================================


def test_draft_payload_is_none_for_no_action():
    c = IntentClassification(intent=IntentType.no_action, confidence=0.5)
    assert c.draft_payload() is None


def test_draft_payload_picks_task_for_task_intents():
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.8, task=TaskDraft(title="t")
    )
    assert c.draft_payload()["title"] == "t"


def test_draft_payload_picks_meeting_for_meeting_intents():
    c = IntentClassification(
        intent=IntentType.create_meeting,
        confidence=0.8,
        meeting=MeetingDraft(title="m"),
    )
    assert c.draft_payload()["title"] == "m"


def test_classification_confidence_bounds_reject_invalid():
    with pytest.raises(ValueError):
        IntentClassification(intent=IntentType.no_action, confidence=1.5)


def test_classification_confidence_bounds_reject_negative():
    with pytest.raises(ValueError):
        IntentClassification(intent=IntentType.no_action, confidence=-0.1)


def test_task_draft_defaults():
    t = TaskDraft(title="t")
    assert t.priority == "medium"
    assert t.due_date is None


def test_task_draft_rejects_unknown_priority():
    with pytest.raises(ValueError):
        TaskDraft(title="t", priority="critical")


def test_meeting_draft_default_participants_list():
    m = MeetingDraft(title="x")
    assert m.participants == []


# =============================================================================
# Token cipher
# =============================================================================


def test_token_cipher_roundtrip(monkeypatch):
    from cryptography.fernet import Fernet

    from app.sync.google_auth import TokenCipher

    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    # Clear cached settings so the new env is picked up.
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    cipher = TokenCipher()
    ct = cipher.encrypt("access-token")
    assert ct != "access-token"
    assert cipher.decrypt(ct) == "access-token"


def test_token_cipher_missing_key_raises(monkeypatch):
    from app.sync.google_auth import TokenCipher

    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        TokenCipher()


def test_token_cipher_invalid_key_raises():
    from app.sync.google_auth import TokenCipher

    with pytest.raises(RuntimeError):
        TokenCipher(key="not-a-valid-fernet-key")


def test_token_cipher_generate_key_round_trips():
    from cryptography.fernet import Fernet

    from app.sync.google_auth import TokenCipher

    key = Fernet.generate_key().decode("ascii")
    c = TokenCipher(key=key)
    assert c.decrypt(c.encrypt("x")) == "x"


# =============================================================================
# Google Sheets row rendering
# =============================================================================


def test_sheets_task_row_has_expected_columns(session):
    from app.models.task import TaskPriority, TaskStatus
    from app.sync.sheets import _task_row

    t = __import__("app.models", fromlist=["Task"]).Task(
        title="hello",
        description="world",
        owner_display_name="@a",
        priority=TaskPriority.high,
        due_date=date(2026, 7, 1),
        status=TaskStatus.todo,
        source_permalink="https://p",
    )
    session.add(t)
    session.flush()
    row = _task_row(t)
    assert row[1] == "hello"
    assert row[2] == "world"
    assert row[3] == "@a"
    assert row[4] == "high"
    assert row[5] == "2026-07-01"
    assert row[6] == "todo"
    assert row[7] == "https://p"


def test_sheets_task_row_handles_missing_optional_fields(session):
    from app.models.task import TaskPriority, TaskStatus
    from app.sync.sheets import _task_row

    t = __import__("app.models", fromlist=["Task"]).Task(
        title="only title",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    session.add(t)
    session.flush()
    row = _task_row(t)
    assert row[2] == ""  # description
    assert row[5] == ""  # no due date
    assert row[7] == ""  # no permalink


# =============================================================================
# Google Tasks body rendering
# =============================================================================


def test_google_tasks_body_includes_due(session):
    from app.models.task import TaskPriority, TaskStatus
    from app.sync.tasks_api import _task_body

    t = __import__("app.models", fromlist=["Task"]).Task(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        due_date=date(2026, 9, 1),
    )
    session.add(t)
    session.flush()
    body = _task_body(t)
    assert body["title"] == "t"
    assert "due" in body
    assert body["due"].startswith("2026-09-01")


def test_google_tasks_body_status_done_means_completed(session):
    from app.models.task import TaskPriority, TaskStatus
    from app.sync.tasks_api import _task_body

    t = __import__("app.models", fromlist=["Task"]).Task(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.done,
    )
    session.add(t)
    session.flush()
    body = _task_body(t)
    assert body["status"] == "completed"


def test_google_tasks_body_status_open_is_needsAction(session):
    from app.models.task import TaskPriority, TaskStatus
    from app.sync.tasks_api import _task_body

    t = __import__("app.models", fromlist=["Task"]).Task(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    session.add(t)
    session.flush()
    body = _task_body(t)
    assert body["status"] == "needsAction"


def test_google_tasks_body_omits_due_when_no_date(session):
    from app.models.task import TaskPriority, TaskStatus
    from app.sync.tasks_api import _task_body

    t = __import__("app.models", fromlist=["Task"]).Task(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
    )
    session.add(t)
    session.flush()
    body = _task_body(t)
    assert "due" not in body


# =============================================================================
# Orchestrator drafts
# =============================================================================


def test_orchestrator_persist_inference_stores_invocation(session):
    from app.config import Settings
    from app.orchestrator import Orchestrator
    from app.schemas.intent import InvocationType
    from app.context.retriever import ContextWindow

    orch = Orchestrator(Settings())
    snap = orch.persist_context_snapshot(
        session,
        ContextWindow(
            conversation_id="C1",
            source_ts="1.0",
            thread_ts=None,
            source_message={"ts": "1.0", "text": "x", "user": "U1"},
        ).to_snapshot_dict(),
    )
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.7, task=TaskDraft(title="t")
    )
    inf = orch.persist_inference(
        session,
        context_snapshot=snap,
        classification=c,
        invocation_type=InvocationType.shortcut,
    )
    assert inf.invocation_type == "shortcut"
    assert inf.confidence == 0.7


def test_orchestrator_create_draft_starts_proposed(session):
    from app.config import Settings
    from app.models import ActionDraftState
    from app.orchestrator import Orchestrator
    from app.schemas.intent import InvocationType
    from app.context.retriever import ContextWindow

    orch = Orchestrator(Settings())
    snap = orch.persist_context_snapshot(
        session,
        ContextWindow(
            conversation_id="C1",
            source_ts="1.0",
            thread_ts=None,
            source_message={"ts": "1.0", "text": "x", "user": "U1"},
        ).to_snapshot_dict(),
    )
    c = IntentClassification(
        intent=IntentType.create_task, confidence=0.9, task=TaskDraft(title="t")
    )
    inf = orch.persist_inference(
        session,
        context_snapshot=snap,
        classification=c,
        invocation_type=InvocationType.passive,
    )
    d = orch.create_draft(
        session,
        inference=inf,
        classification=c,
        created_by_slack_user_id="U1",
        slack_message_ts="1.0",
    )
    assert d.state == ActionDraftState.proposed
    assert d.payload["title"] == "t"
