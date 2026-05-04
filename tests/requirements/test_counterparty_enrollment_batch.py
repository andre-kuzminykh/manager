"""FR-CR-05-138 — batch multi-select enrollment widget.

Covers the new flow end-to-end:

1. `post_enrollment_batch` creates one
   `CounterpartyPromptBatch` per (recording, recipient) and N
   `CounterpartyPrompt` rows tied to it; posts ONE
   multi-select message; idempotent on rerun.

2. `handle_toggle` flips `selected` flag, redraws widget.

3. `handle_next` with empty selection → batch terminates with
   all entities `declined`.

4. `handle_next` with N selected → transitions batch to
   `processing`, current_index=1, current_step=confirm_name;
   sends the first stage-2 prompt.

5. Confirm-name path: text reply → `canonical_name_corrected`
   set, advances to context step.

6. Context path: text reply → `Counterparty` hub +
   `telegram_enrollment` satellite created, advances to next
   selected entity.

7. Final entity processed → batch `completed`, recap message
   shown.

8. `find_active_batch_for_user` — listener helper for routing
   text/voice replies without a `reply_to`.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import (
    Counterparty,
    CounterpartyAttribute,
    CounterpartyPrompt,
    CounterpartyPromptBatch,
)
from app.models.counterparty_prompt import (
    STATUS_BATCH_COMPLETED,
    STATUS_BATCH_PROCESSING,
    STATUS_COMPLETED_ADDED,
    STATUS_COMPLETED_SKIPPED,
    STATUS_DECLINED,
    STATUS_PENDING_CONTEXT,
    STATUS_PENDING_SELECTION,
    STEP_CONFIRM_NAME,
    STEP_CONTEXT,
)
from app.services.counterparty_enrollment_batch import (
    find_active_batch_for_user,
    handle_keep_current_name,
    handle_next,
    handle_skip_current,
    handle_toggle,
    post_enrollment_batch,
    receive_text_for_current,
)


# --- Fake Telegram sender capturing every API call ------------

class _FakeSender:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self._next_msg_id = 8000

    def _alloc(self) -> int:
        m = self._next_msg_id
        self._next_msg_id += 1
        return m

    def send_message(self, **kwargs):
        m = self._alloc()
        self.sent.append({**kwargs, "_assigned_message_id": m})
        return {"message_id": m}

    def update_message(self, **kwargs):
        self.edited.append(dict(kwargs))
        return {"message_id": kwargs.get("message_id")}


# --- post_enrollment_batch ------------------------------------

def test_post_batch_creates_one_widget_per_recipient(session):
    sender = _FakeSender()
    res = post_enrollment_batch(
        session,
        sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="01/05 - Fundraising sync",
        unresolved_mentions=["Bautert", "Odea", "Bluenet"],
        recipient_user_ids=[7001, 7002],
    )
    assert res.batches_created == 2
    assert res.failed == 0
    # Two batches in DB, three prompts each.
    batches = session.query(CounterpartyPromptBatch).all()
    assert len(batches) == 2
    for b in batches:
        assert b.entity_count == 3
        assert b.status == STATUS_PENDING_SELECTION
        assert b.multiselect_message_id is not None
        prompts = sorted(b.prompts, key=lambda p: p.index_in_batch)
        assert [p.index_in_batch for p in prompts] == [1, 2, 3]
        assert all(p.selected is False for p in prompts)
        assert all(
            p.status == STATUS_PENDING_SELECTION for p in prompts
        )
    # ONE message per recipient, not three.
    assert len(sender.sent) == 2
    assert all("Tap numbers" in m["text"] for m in sender.sent)
    # Numpad has 3 entity buttons + Next row.
    kbd = sender.sent[0]["reply_markup"]
    flat = [
        b["text"] for r in kbd["inline_keyboard"] for b in r
    ]
    assert "1" in flat and "2" in flat and "3" in flat
    assert any("Next" in b for b in flat)


def test_post_batch_idempotent_per_recipient(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    sender2 = _FakeSender()
    res = post_enrollment_batch(
        session, sender=sender2,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    assert res.batches_created == 0
    assert res.batches_skipped_existing == 1
    assert sender2.sent == []
    assert session.query(CounterpartyPromptBatch).count() == 1


def test_post_batch_dedupes_phonetic_variants(session):
    """Same entity in two surface forms should produce ONE
    numbered slot, not two — operator's UX expectation."""
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Тезер", "тезер", "Bautert"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    assert batch.entity_count == 2  # tezer + bautert


# --- handle_toggle --------------------------------------------

def test_toggle_flips_selected_and_redraws(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert", "Odea"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    p1 = sorted(batch.prompts, key=lambda p: p.index_in_batch)[0]

    sender2 = _FakeSender()
    handle_toggle(
        session, sender=sender2, prompt_id=p1.id,
        actor_user_id=7001,
    )
    session.refresh(p1)
    assert p1.selected is True
    # Widget edited in place.
    assert len(sender2.edited) == 1
    edit = sender2.edited[0]
    assert "✅ 1" in edit["text"]
    assert "1 selected" in str(edit["reply_markup"])

    # Toggle again — should unselect.
    sender3 = _FakeSender()
    handle_toggle(
        session, sender=sender3, prompt_id=p1.id,
        actor_user_id=7001,
    )
    session.refresh(p1)
    assert p1.selected is False


def test_toggle_rejects_other_user(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    p = batch.prompts[0]
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=p.id,
        actor_user_id=9999,
    )
    session.refresh(p)
    assert p.selected is False


# --- handle_next ----------------------------------------------

def test_next_with_zero_selected_terminates_batch(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert", "Odea"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    sender2 = _FakeSender()
    handle_next(
        session, sender=sender2, batch_id=batch.id,
        actor_user_id=7001,
    )
    session.refresh(batch)
    assert batch.status == STATUS_BATCH_COMPLETED
    assert all(p.status == STATUS_DECLINED for p in batch.prompts)
    assert (
        session.query(Counterparty)
        .filter(Counterparty.name.in_(["Bautert", "Odea"]))
        .count() == 0
    )


def test_next_with_selected_starts_processing(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert", "Odea", "Bluenet"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    prompts = sorted(batch.prompts, key=lambda p: p.index_in_batch)
    # Select 1 and 3.
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=prompts[0].id,
        actor_user_id=7001,
    )
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=prompts[2].id,
        actor_user_id=7001,
    )
    sender2 = _FakeSender()
    handle_next(
        session, sender=sender2, batch_id=batch.id,
        actor_user_id=7001,
    )
    session.refresh(batch)
    assert batch.status == STATUS_BATCH_PROCESSING
    assert batch.current_index == 1
    assert batch.current_step == STEP_CONFIRM_NAME
    # Bluenet (index 3) selected, Odea (index 2) declined.
    session.refresh(prompts[1])
    assert prompts[1].status == STATUS_DECLINED
    # First prompt-2 message sent, asking about Bautert (1 of 2).
    assert len(sender2.sent) == 1
    assert "1 of 2" in sender2.sent[0]["text"]
    assert "Bautert" in sender2.sent[0]["text"]


# --- Confirm-name + context (full per-entity loop) ------------

def test_keep_name_advances_to_context(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    p = batch.prompts[0]
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=p.id,
        actor_user_id=7001,
    )
    handle_next(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )

    sender3 = _FakeSender()
    handle_keep_current_name(
        session, sender=sender3, batch_id=batch.id,
        actor_user_id=7001,
    )
    session.refresh(batch)
    assert batch.current_step == STEP_CONTEXT
    session.refresh(p)
    assert p.canonical_name_corrected == "Bautert"
    assert p.status == STATUS_PENDING_CONTEXT
    # Context prompt sent.
    assert len(sender3.sent) == 1
    assert "Bautert" in sender3.sent[0]["text"]
    assert "context" in sender3.sent[0]["text"].lower()


def test_text_reply_during_confirm_name_overrides(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    p = batch.prompts[0]
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=p.id,
        actor_user_id=7001,
    )
    handle_next(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )

    sender3 = _FakeSender()
    receive_text_for_current(
        session, sender=sender3, batch_id=batch.id,
        text="Bauerdart Capital", actor_user_id=7001,
    )
    session.refresh(batch)
    assert batch.current_step == STEP_CONTEXT
    session.refresh(p)
    assert p.canonical_name_corrected == "Bauerdart Capital"


def test_text_reply_during_context_creates_counterparty_and_advances(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert", "Odea"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    prompts = sorted(batch.prompts, key=lambda p: p.index_in_batch)
    for p in prompts:
        handle_toggle(
            session, sender=_FakeSender(), prompt_id=p.id,
            actor_user_id=7001,
        )
    handle_next(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )

    # Bautert: keep name, then provide context.
    handle_keep_current_name(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )
    receive_text_for_current(
        session, sender=_FakeSender(), batch_id=batch.id,
        text="Family office, contact via James, attended TWG demo",
        actor_user_id=7001,
    )
    session.refresh(batch)
    # Batch advanced to entity 2 of 2 (Odea), confirm-name.
    assert batch.current_index == 2
    assert batch.current_step == STEP_CONFIRM_NAME

    # Bautert hub + satellite created.
    cp = (
        session.query(Counterparty)
        .filter(Counterparty.name == "Bautert").one()
    )
    sat = (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.counterparty_id == cp.id).one()
    )
    assert sat.source == "telegram_enrollment"
    assert "Family office" in sat.attributes["notes"]


def test_skip_during_confirm_drops_entity(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    p = batch.prompts[0]
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=p.id,
        actor_user_id=7001,
    )
    handle_next(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )

    handle_skip_current(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )
    session.refresh(p)
    assert p.status == STATUS_COMPLETED_SKIPPED
    assert p.created_counterparty_id is None
    assert (
        session.query(Counterparty)
        .filter(Counterparty.name == "Bautert").count() == 0
    )
    session.refresh(batch)
    assert batch.status == STATUS_BATCH_COMPLETED


def test_skip_during_context_keeps_hub_no_satellite(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    p = batch.prompts[0]
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=p.id,
        actor_user_id=7001,
    )
    handle_next(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )
    handle_keep_current_name(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )
    handle_skip_current(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )
    cp = (
        session.query(Counterparty)
        .filter(Counterparty.name == "Bautert").one()
    )
    assert (
        session.query(CounterpartyAttribute)
        .filter(CounterpartyAttribute.counterparty_id == cp.id)
        .count() == 0
    )
    session.refresh(p)
    assert p.created_counterparty_id == cp.id
    assert p.status == STATUS_COMPLETED_SKIPPED
    session.refresh(batch)
    assert batch.status == STATUS_BATCH_COMPLETED


# --- find_active_batch_for_user (listener helper) -------------

def test_find_active_returns_processing_batch(session):
    sender = _FakeSender()
    post_enrollment_batch(
        session, sender=sender,
        source_kind="zoom", source_id="zm-1",
        meeting_title="x",
        unresolved_mentions=["Bautert"],
        recipient_user_ids=[7001],
    )
    [batch] = session.query(CounterpartyPromptBatch).all()
    # Not processing yet — pending_selection.
    assert find_active_batch_for_user(
        session, chat_id=7001, user_id=7001,
    ) is None

    p = batch.prompts[0]
    handle_toggle(
        session, sender=_FakeSender(), prompt_id=p.id,
        actor_user_id=7001,
    )
    handle_next(
        session, sender=_FakeSender(), batch_id=batch.id,
        actor_user_id=7001,
    )
    found = find_active_batch_for_user(
        session, chat_id=7001, user_id=7001,
    )
    assert found is not None
    assert found.id == batch.id
