"""FR-CR-05-133 — Telegram-side enrollment widget for
counterparty mentions that didn't resolve to the directory.

Covers the full state machine end-to-end:

  pending_yesno ─[Yes]──→ awaiting_context ─[text/voice]→ completed_added
                ─[No]───→ declined          ─[Skip]──────→ completed_skipped

Each terminal-completed transition either creates a new
Counterparty hub (with `name = mention`) plus an optional
`telegram_enrollment` satellite carrying the operator's notes,
or a no-op declined row (no DB write to `counterparties`).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import (
    Counterparty,
    CounterpartyAttribute,
    CounterpartyPrompt,
)
from app.models.counterparty_prompt import (
    STATUS_AWAITING_CONTEXT,
    STATUS_COMPLETED_ADDED,
    STATUS_COMPLETED_SKIPPED,
    STATUS_DECLINED,
    STATUS_PENDING_YESNO,
)
from app.services.counterparty_enrollment import (
    complete_with_context,
    handle_no,
    handle_skip,
    handle_yes,
    post_enrollment_prompts,
)
from app.telegram_bot.keyboards import (
    ACTION_ENROLL_NO,
    ACTION_ENROLL_SKIP,
    ACTION_ENROLL_YES,
    enrollment_skip_keyboard,
    enrollment_yesno_keyboard,
    parse_callback_data,
)


# --- Fake Telegram sender capturing every API call ------------

class _FakeSender:
    """Minimal stand-in for `TelegramSender`. Captures
    `send_message` / `update_message` / `delete_message` calls
    and returns deterministic message_ids so tests can assert
    against the persisted state."""

    enabled = True

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self._next_msg_id = 5000

    def _alloc_msg_id(self) -> int:
        mid = self._next_msg_id
        self._next_msg_id += 1
        return mid

    def send_message(self, **kwargs):
        kwargs = dict(kwargs)
        msg_id = self._alloc_msg_id()
        kwargs["_assigned_message_id"] = msg_id
        self.sent.append(kwargs)
        return {"message_id": msg_id}

    def update_message(self, **kwargs):
        self.edited.append(dict(kwargs))
        return {"message_id": kwargs.get("message_id")}

    def delete_message(self, **kwargs):  # noqa: D401
        return {"ok": True}


# --- Keyboard contracts ---------------------------------------

def test_enrollment_yesno_keyboard_uses_english_labels_and_action_constants():
    """FR-CR-05-133 — operator-pinned: all enrollment-widget
    text is English. The two callback_data values must encode
    the FR-CR-05-133 action constants and the prompt id."""
    kbd = enrollment_yesno_keyboard(prompt_id=42)
    rows = kbd["inline_keyboard"]
    assert len(rows) == 1
    btn_yes, btn_no = rows[0]
    assert btn_yes["text"] == "Yes"
    assert btn_no["text"] == "No"
    assert parse_callback_data(btn_yes["callback_data"]) == (
        ACTION_ENROLL_YES, 42,
    )
    assert parse_callback_data(btn_no["callback_data"]) == (
        ACTION_ENROLL_NO, 42,
    )


def test_enrollment_skip_keyboard_single_button():
    kbd = enrollment_skip_keyboard(prompt_id=99)
    rows = kbd["inline_keyboard"]
    assert len(rows) == 1
    [btn] = rows[0]
    assert btn["text"] == "Skip"
    assert parse_callback_data(btn["callback_data"]) == (
        ACTION_ENROLL_SKIP, 99,
    )


# --- post_enrollment_prompts ----------------------------------

def test_post_enrollment_prompts_creates_one_widget_per_mention_per_recipient(session):
    """One mention × N recipients → N prompts. Phonetically
    identical surface forms («Тезер» / «тезер») dedupe to ONE
    widget per recipient — different forms of the same entity
    shouldn't spam the operator multiple times."""
    sender = _FakeSender()
    result = post_enrollment_prompts(
        session,
        sender=sender,
        source_kind="zoom",
        source_id="zoom-abc-1",
        unresolved_mentions=["Тезер", "тезер", "Jabal", "Одея"],
        recipient_user_ids=[7001, 7002],
    )
    # 3 unique mentions × 2 recipients = 6 widgets.
    assert result.posted == 6
    assert result.skipped_existing == 0
    assert result.failed == 0
    assert len(sender.sent) == 6

    rows = (
        session.query(CounterpartyPrompt)
        .order_by(CounterpartyPrompt.id)
        .all()
    )
    assert len(rows) == 6
    assert {r.mention_normalised for r in rows} == {"tezer", "jabal", "odeya"}
    # Both recipients see all three mentions.
    by_user = {}
    for r in rows:
        by_user.setdefault(r.user_id, set()).add(r.mention_normalised)
    assert by_user[7001] == {"tezer", "jabal", "odeya"}
    assert by_user[7002] == {"tezer", "jabal", "odeya"}
    # Each row got its message_id back from the fake sender.
    assert all(r.yesno_message_id for r in rows)
    # Status starts at pending_yesno.
    assert {r.status for r in rows} == {STATUS_PENDING_YESNO}


def test_post_enrollment_prompts_idempotent_on_rerun(session):
    """A second pipeline pass for the same recording must NOT
    re-send widgets the operator already saw — UNIQUE
    constraint protects the operator from double-pings."""
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-abc-1",
        unresolved_mentions=["Tether"], recipient_user_ids=[7001],
    )
    assert session.query(CounterpartyPrompt).count() == 1

    sender2 = _FakeSender()
    result2 = post_enrollment_prompts(
        session, sender=sender2,
        source_kind="zoom", source_id="zoom-abc-1",
        unresolved_mentions=["Tether"], recipient_user_ids=[7001],
    )
    assert result2.posted == 0
    assert result2.skipped_existing == 1
    assert sender2.sent == []
    assert session.query(CounterpartyPrompt).count() == 1


def test_post_enrollment_prompts_skips_when_no_recipients(session):
    sender = _FakeSender()
    result = post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-abc-1",
        unresolved_mentions=["Tether"], recipient_user_ids=[],
    )
    assert result.posted == 0
    assert sender.sent == []
    assert session.query(CounterpartyPrompt).count() == 0


def test_post_enrollment_prompts_records_send_failure_without_crashing(session):
    """When Telegram returns an empty body (404 / 500), the
    prompt row stays in the DB so a future retry / manual
    enrollment can succeed; we don't crash the pipeline."""

    class _SilentlyFailingSender:
        enabled = True
        def send_message(self, **kw):  # noqa: D401
            return {}  # No message_id.

    result = post_enrollment_prompts(
        session, sender=_SilentlyFailingSender(),
        source_kind="zoom", source_id="zoom-abc-1",
        unresolved_mentions=["Tether"], recipient_user_ids=[7001],
    )
    assert result.posted == 0
    assert result.failed == 1
    row = session.query(CounterpartyPrompt).one()
    assert row.yesno_message_id is None
    assert row.status == STATUS_PENDING_YESNO


# --- handle_yes (Stage 1 → Stage 2) ---------------------------

def test_handle_yes_transitions_to_awaiting_context_and_edits_widget(session):
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["Tether"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()
    yesno_msg_id = prompt.yesno_message_id

    sender2 = _FakeSender()
    out = handle_yes(
        session, sender=sender2, prompt_id=prompt.id,
        actor_user_id=7001,
    )
    assert out is not None
    assert out.status == STATUS_AWAITING_CONTEXT
    assert out.responded_at is not None
    # Widget got edited in place to the stage-2 copy.
    assert len(sender2.edited) == 1
    edit = sender2.edited[0]
    assert edit["chat_id"] == 7001
    assert edit["message_id"] == yesno_msg_id
    assert "Tether" in edit["text"]
    assert "Skip" in str(edit["reply_markup"])
    # context_message_id captured for follow-up reply matching.
    assert out.context_message_id is not None


def test_handle_yes_rejects_unrelated_admin_clicks(session):
    """Only the row's intended recipient owns the flow — a
    different admin clicking Yes on someone else's widget gets
    ignored (returns None, no state change)."""
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["Tether"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()

    out = handle_yes(
        session, sender=_FakeSender(), prompt_id=prompt.id,
        actor_user_id=9999,  # different admin
    )
    assert out is None
    session.refresh(prompt)
    assert prompt.status == STATUS_PENDING_YESNO


def test_handle_yes_idempotent_on_double_click(session):
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["Tether"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()
    handle_yes(session, sender=_FakeSender(), prompt_id=prompt.id,
               actor_user_id=7001)
    sender3 = _FakeSender()
    handle_yes(session, sender=sender3, prompt_id=prompt.id,
               actor_user_id=7001)
    # Second call must NOT re-edit the message (it's already
    # in stage-2; another edit would be confusing).
    assert sender3.edited == []
    session.refresh(prompt)
    assert prompt.status == STATUS_AWAITING_CONTEXT


# --- handle_no (terminal, no DB write) ------------------------

def test_handle_no_sets_declined_and_does_not_create_counterparty(session):
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["RandomAcq"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()

    sender2 = _FakeSender()
    out = handle_no(
        session, sender=sender2, prompt_id=prompt.id,
        actor_user_id=7001,
    )
    assert out is not None
    assert out.status == STATUS_DECLINED
    assert out.created_counterparty_id is None
    assert (
        session.query(Counterparty)
        .filter(Counterparty.name == "RandomAcq")
        .count() == 0
    )
    # Widget edited in place, keyboard cleared.
    assert len(sender2.edited) == 1
    edit = sender2.edited[0]
    assert "RandomAcq" in edit["text"]
    assert edit["reply_markup"] == {"inline_keyboard": []}


# --- handle_skip (Stage 2 terminal, hub-only) -----------------

def test_handle_skip_creates_hub_without_satellite(session):
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["NewFund"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()
    handle_yes(session, sender=_FakeSender(), prompt_id=prompt.id,
               actor_user_id=7001)
    session.refresh(prompt)
    assert prompt.status == STATUS_AWAITING_CONTEXT

    sender3 = _FakeSender()
    out = handle_skip(
        session, sender=sender3, prompt_id=prompt.id,
        actor_user_id=7001,
    )
    assert out is not None
    assert out.status == STATUS_COMPLETED_SKIPPED
    cp = session.get(Counterparty, out.created_counterparty_id)
    assert cp is not None
    assert cp.name == "NewFund"
    # No telegram_enrollment satellite when skipped.
    assert (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.counterparty_id == cp.id)
        .count() == 0
    )
    # Widget edited to confirmation.
    assert len(sender3.edited) == 1
    assert "NewFund" in sender3.edited[0]["text"]


def test_handle_skip_reuses_existing_hub_when_same_normalised_name(session):
    """If the operator manually added the entity to the Sheet
    while the prompt was open, a Skip click must NOT create a
    duplicate hub."""
    session.add(Counterparty(name="NewFund", name_normalised="newfund"))
    session.flush()

    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["newfund"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()
    handle_yes(session, sender=_FakeSender(), prompt_id=prompt.id,
               actor_user_id=7001)
    out = handle_skip(
        session, sender=_FakeSender(), prompt_id=prompt.id,
        actor_user_id=7001,
    )
    assert (
        session.query(Counterparty)
        .filter(Counterparty.name_normalised == "newfund")
        .count() == 1
    )
    assert out.created_counterparty_id is not None


# --- complete_with_context (Stage 2 terminal, hub + satellite) -

def test_complete_with_context_creates_hub_and_satellite_with_notes(session):
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["Odeya"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()
    handle_yes(session, sender=_FakeSender(), prompt_id=prompt.id,
               actor_user_id=7001)

    sender3 = _FakeSender()
    out = complete_with_context(
        session, sender=sender3, prompt_id=prompt.id,
        context_text="Israeli partner introduced via Ziya, follow-up next week.",
        actor_user_id=7001,
    )
    assert out is not None
    assert out.status == STATUS_COMPLETED_ADDED
    cp = session.get(Counterparty, out.created_counterparty_id)
    assert cp is not None
    assert cp.name == "Odeya"
    sat = (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.counterparty_id == cp.id)
        .one()
    )
    assert sat.source == "telegram_enrollment"
    assert "Israeli partner" in sat.attributes["notes"]
    assert sat.attributes["via_recording_kind"] == "zoom"
    assert sat.attributes["via_recording_id"] == "zoom-1"

    assert len(sender3.edited) == 1
    assert "Odeya" in sender3.edited[0]["text"]
    assert sender3.edited[0]["reply_markup"] == {"inline_keyboard": []}


def test_complete_with_context_strips_whitespace_and_skips_empty_reply(session):
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["Odeya"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()
    handle_yes(session, sender=_FakeSender(), prompt_id=prompt.id,
               actor_user_id=7001)

    out = complete_with_context(
        session, sender=_FakeSender(), prompt_id=prompt.id,
        context_text="   \n  ", actor_user_id=7001,
    )
    # Empty-after-strip should NOT transition; keep awaiting.
    session.refresh(out)
    assert out.status == STATUS_AWAITING_CONTEXT
    assert out.created_counterparty_id is None


def test_complete_with_context_replays_overwrites_satellite(session):
    """Operator changes their mind and resends context — the
    satellite must be UPDATED in place, not duplicated."""
    sender = _FakeSender()
    post_enrollment_prompts(
        session, sender=sender,
        source_kind="zoom", source_id="zoom-1",
        unresolved_mentions=["Odeya"], recipient_user_ids=[7001],
    )
    [prompt] = session.query(CounterpartyPrompt).all()
    handle_yes(session, sender=_FakeSender(), prompt_id=prompt.id,
               actor_user_id=7001)

    complete_with_context(
        session, sender=_FakeSender(), prompt_id=prompt.id,
        context_text="first note", actor_user_id=7001,
    )
    # Hub is now in completed_added; second complete is a
    # no-op on the row, so simulate the «awaiting_context →
    # complete» path by manually rewinding the row to
    # awaiting_context (mimics operator pressing Yes again on
    # a future fresh widget for the same entity).
    prompt.status = STATUS_AWAITING_CONTEXT
    session.flush()
    complete_with_context(
        session, sender=_FakeSender(), prompt_id=prompt.id,
        context_text="updated note", actor_user_id=7001,
    )
    cp = session.query(Counterparty).filter(
        Counterparty.name_normalised == "odeya"
    ).one()
    sats = (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.counterparty_id == cp.id)
        .all()
    )
    assert len(sats) == 1
    assert sats[0].attributes["notes"] == "updated note"


# --- Pipeline integration smoke tests --------------------------

def test_fireflies_pipeline_posts_enrollment_widgets_for_unresolved_mentions(
    session, monkeypatch
):
    """FR-CR-05-133 end-to-end: when Pass 2 returns
    `directory_id is None` for any mention, the fireflies
    pipeline's `_step_enroll_unresolved` posts widgets to every
    admin DM and persists prompt rows. Verifies the wiring;
    state-machine semantics are pinned in the unit tests above."""
    from datetime import datetime as _dt
    from app.fireflies.pipeline import FirefliesPipeline
    from app.models import MeetingRecording

    # Existing directory entry — Tether resolves, the other
    # three («Тезер» phonetic, Jabal, Одея) do NOT — actually
    # «Тезер»→Tether is plausible, so the LLM stub below maps
    # only Tether-as-Tether and leaves Jabal / Одея as null.
    session.add(Counterparty(name="Tether", name_normalised="tether"))
    session.flush()

    rec = MeetingRecording(
        fireflies_id="ff-enroll-1",
        title="Test",
        transcript_text="Тезер и Jabal и Одея в одном предложении",
        detailed_summary="x",
        audio_downloaded=True,
        transcribed=True,
        detailed_summarised=True,
        meeting_date=_dt(2026, 5, 2, tzinfo=timezone.utc),
    )
    session.add(rec)
    session.flush()
    tether_id = (
        session.query(Counterparty)
        .filter(Counterparty.name_normalised == "tether")
        .one()
        .id
    )

    class _DummyLLM:
        def call_tool(self, **kw):
            raise NotImplementedError
        def complete_text(self, *, system_prompt, **kw):
            import json as _json
            if "list every" in system_prompt and "COUNTERPARTY mention" in system_prompt:
                return _json.dumps({
                    "mentions": ["Тезер", "Jabal", "Одея"]
                })
            if "map each counterparty MENTION" in system_prompt:
                return _json.dumps({"matches": [
                    {"mention": "Тезер", "directory_id": tether_id},
                    {"mention": "Jabal", "directory_id": None},
                    {"mention": "Одея", "directory_id": None},
                ]})
            return "{}"

    sender = _FakeSender()

    monkeypatch.setattr(
        "app.telegram_bot.handlers.admin_user_ids",
        lambda: {"7001"},
    )

    from app.config import Settings
    settings = Settings(
        fireflies_tasks_model="gpt-5.5",
        fireflies_tasks_reasoning_effort="",
    )

    class _StubFireflies:
        def list_recent(self, **kw):
            return []
        def fetch_audio_url(self, **kw):
            return None
        def fetch_transcript(self, **kw):
            return None

    pipeline = FirefliesPipeline(
        settings=settings,
        client=_StubFireflies(),
        llm_backend=_DummyLLM(),
        sender=sender,
    )
    # Drive only the two steps under test.
    pipeline._step_match_counterparties(session, rec)
    posted = pipeline._step_enroll_unresolved(session, rec)
    # FR-CR-05-138 — pipeline now returns batches_created
    # (one batch per recipient), not per-entity prompts. Two
    # unresolved × one admin → 1 batch with 2 prompts inside.
    assert posted == 1
    from app.models import CounterpartyPromptBatch
    [batch] = (
        session.query(CounterpartyPromptBatch)
        .filter(CounterpartyPromptBatch.source_kind == "fireflies")
        .filter(CounterpartyPromptBatch.source_id == "ff-enroll-1")
        .all()
    )
    assert batch.entity_count == 2
    rows = (
        session.query(CounterpartyPrompt)
        .filter(CounterpartyPrompt.source_kind == "fireflies")
        .filter(CounterpartyPrompt.source_id == "ff-enroll-1")
        .all()
    )
    assert len(rows) == 2
    assert {r.mention_text for r in rows} == {"Jabal", "Одея"}
