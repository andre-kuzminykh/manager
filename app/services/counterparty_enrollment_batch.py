"""FR-CR-05-138 — batch multi-select enrollment widget.

Replaces the per-entity yes/no flow (FR-CR-05-133) with one
multi-select message per recipient + a per-entity processing
loop. State machine:

    pending_selection
        ↓ (user toggles indices, clicks [Next →])
    processing (current_index=1, current_step="confirm_name")
        ↓ (user types corrected name OR clicks [Keep] OR [Skip])
    processing (current_index=1, current_step="context")    ← if not Skip
        ↓ (user types context OR clicks [Skip])
    processing (current_index=2, current_step="confirm_name")
        ... (loop until last selected index processed)
    completed

The legacy per-entity widget code (`counterparty_enrollment.py`)
stays for backwards compat with old DB rows; the pipeline now
calls THIS module's `post_enrollment_batch` instead.

Operator-pinned shape («сделаем так: что не распознал, выводи
одним сообщением после задач… кнопки к виджету сделай мульти-
выбор циферный … далее, и он по каждой сначала корректное
название поставить / подтвердить, далее тебя начнет спрашивать
информацию и можно ввести текстом или голосом»).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
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
    STATUS_PENDING_CONFIRM_NAME,
    STATUS_PENDING_CONTEXT,
    STATUS_PENDING_SELECTION,
    STEP_CONFIRM_NAME,
    STEP_CONTEXT,
)
from app.services.trace_log import trace_event
from app.sync.counterparties import normalise_name
from app.telegram_bot.keyboards import (
    batch_confirm_name_keyboard,
    batch_context_keyboard,
    batch_select_keyboard,
)

log = get_logger(__name__)


# --- Telegram-facing copy (English, operator-pinned) ----------

def _selection_text(
    *, meeting_title: str, prompts: list[CounterpartyPrompt]
) -> str:
    """Stage 1 — numbered list + ask to select."""
    header = (
        f"🔍 Found {len(prompts)} unrecognised entities in "
        f"this meeting"
    )
    if meeting_title:
        header += f" «{meeting_title.strip()[:80]}»"
    body = []
    for p in prompts:
        marker = "✅" if p.selected else "  "
        body.append(f"{marker} {p.index_in_batch}. {p.mention_text}")
    return (
        f"{header}:\n\n"
        + "\n".join(body)
        + "\n\nTap numbers to select which to track, then [Next →]."
    )


def _confirm_name_text(
    *, idx: int, total: int, mention: str
) -> str:
    return (
        f"Processing entity {idx} of {total}: «{mention}»\n\n"
        f"Reply with the correct canonical name (text or voice), "
        f"or press [Keep] to use «{mention}» as-is, or [Skip] to "
        f"drop this entity."
    )


def _context_text(*, idx: int, total: int, name: str) -> str:
    return (
        f"Entity {idx} of {total} ✓  —  «{name}»\n\n"
        f"Send any context for this entity (text or voice), or "
        f"[Skip] to add it without notes."
    )


def _final_recap(
    *,
    added_with_context: int,
    added_no_context: int,
    skipped: int,
    declined: int,
) -> str:
    parts = ["✅ Done."]
    if added_with_context:
        parts.append(
            f"Added {added_with_context} with context."
        )
    if added_no_context:
        parts.append(
            f"Added {added_no_context} without context."
        )
    if skipped:
        parts.append(f"Skipped {skipped}.")
    if declined:
        parts.append(f"Untouched {declined}.")
    return " ".join(parts)


# --- Public API -----------------------------------------------

@dataclass
class BatchPostResult:
    """Aggregate result of `post_enrollment_batch` so the
    pipeline can log how many recipients got a widget."""

    batches_created: int
    batches_skipped_existing: int
    failed: int


def post_enrollment_batch(
    session: Session,
    *,
    sender: Any,
    source_kind: str,
    source_id: str,
    meeting_title: str,
    unresolved_mentions: Iterable[str],
    recipient_user_ids: Iterable[int],
) -> BatchPostResult:
    """FR-CR-05-138 — for each (recipient × recording), create a
    batch + N CounterpartyPrompt rows + post ONE multi-select
    widget. Idempotent: skips recipients that already have a
    batch for this recording (UNIQUE constraint).
    """
    by_norm: dict[str, str] = {}
    for m in unresolved_mentions:
        norm = normalise_name(m)
        if not norm:
            continue
        existing = by_norm.get(norm)
        if existing is None or len(m) > len(existing):
            by_norm[norm] = m
    ordered_mentions = list(by_norm.items())  # [(norm, surface), ...]
    recipient_list = [int(u) for u in recipient_user_ids]
    if not ordered_mentions or not recipient_list:
        return BatchPostResult(0, 0, 0)

    created = 0
    skipped_existing = 0
    failed = 0
    for uid in recipient_list:
        existing_batch = (
            session.query(CounterpartyPromptBatch)
            .filter(
                CounterpartyPromptBatch.source_kind == source_kind,
                CounterpartyPromptBatch.source_id == source_id,
                CounterpartyPromptBatch.user_id == uid,
            )
            .one_or_none()
        )
        if existing_batch is not None:
            skipped_existing += 1
            continue

        batch = CounterpartyPromptBatch(
            source_kind=source_kind,
            source_id=source_id,
            chat_id=uid,
            user_id=uid,
            entity_count=len(ordered_mentions),
            status=STATUS_PENDING_SELECTION,
        )
        session.add(batch)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            skipped_existing += 1
            continue

        prompts: list[CounterpartyPrompt] = []
        for idx, (norm, surface) in enumerate(ordered_mentions, start=1):
            p = CounterpartyPrompt(
                source_kind=source_kind,
                source_id=source_id,
                mention_text=surface,
                mention_normalised=norm,
                chat_id=uid,
                user_id=uid,
                status=STATUS_PENDING_SELECTION,
                batch_id=batch.id,
                index_in_batch=idx,
                selected=False,
            )
            session.add(p)
            prompts.append(p)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            skipped_existing += 1
            continue

        msg_text = _selection_text(
            meeting_title=meeting_title, prompts=prompts
        )
        keyboard = batch_select_keyboard(
            prompts=[
                {"id": p.id, "index": p.index_in_batch,
                 "selected": p.selected}
                for p in prompts
            ],
            batch_id=batch.id,
            selected_count=0,
        )
        try:
            resp = sender.send_message(
                chat_id=uid, text=msg_text, reply_markup=keyboard,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "enrollment_batch_send_failed",
                batch_id=batch.id, uid=uid, error=str(e),
            )
            failed += 1
            continue
        msg_id = (resp or {}).get("message_id")
        if msg_id:
            batch.multiselect_message_id = int(msg_id)
            session.flush()
            created += 1
        else:
            failed += 1

    trace_event(
        source=source_kind, recording_id=source_id,
        event="enrollment_batch_posted",
        unique_mentions=len(ordered_mentions),
        recipients=len(recipient_list),
        created=created, skipped_existing=skipped_existing,
        failed=failed,
    )
    log.info(
        "enrollment_batch_posted",
        source_kind=source_kind, source_id=source_id,
        unique_mentions=len(ordered_mentions),
        recipients=len(recipient_list),
        created=created, skipped_existing=skipped_existing,
        failed=failed,
    )
    return BatchPostResult(
        batches_created=created,
        batches_skipped_existing=skipped_existing,
        failed=failed,
    )


# --- Stage 1 callbacks: toggle + next -------------------------

def handle_toggle(
    session: Session,
    *,
    sender: Any,
    prompt_id: int,
    actor_user_id: int,
) -> CounterpartyPromptBatch | None:
    """User tapped a number in the multi-select grid. Toggle
    `selected` flag on that prompt, redraw the widget."""
    prompt = session.get(CounterpartyPrompt, prompt_id)
    if prompt is None or prompt.batch_id is None:
        return None
    if int(prompt.user_id) != int(actor_user_id):
        return None
    batch = session.get(CounterpartyPromptBatch, prompt.batch_id)
    if batch is None or batch.status != STATUS_PENDING_SELECTION:
        return batch
    prompt.selected = not bool(prompt.selected)
    session.flush()
    _redraw_selection_widget(sender, session, batch)
    return batch


def handle_next(
    session: Session,
    *,
    sender: Any,
    batch_id: int,
    actor_user_id: int,
) -> CounterpartyPromptBatch | None:
    """User clicked [Next →]. Mark unselected entities as
    `declined`, transition batch into processing mode starting
    with the first selected entity at the confirm-name step."""
    batch = session.get(CounterpartyPromptBatch, batch_id)
    if batch is None:
        return None
    if int(batch.user_id) != int(actor_user_id):
        return None
    if batch.status != STATUS_PENDING_SELECTION:
        return batch

    selected = [
        p for p in (batch.prompts or [])
        if p.selected
    ]
    if not selected:
        # Nothing selected — terminal-skip the whole batch.
        for p in (batch.prompts or []):
            p.status = STATUS_DECLINED
            p.responded_at = datetime.now(timezone.utc)
        batch.status = STATUS_BATCH_COMPLETED
        batch.completed_at = datetime.now(timezone.utc)
        session.flush()
        if batch.multiselect_message_id:
            try:
                sender.update_message(
                    chat_id=batch.chat_id,
                    message_id=batch.multiselect_message_id,
                    text=_final_recap(
                        added_with_context=0, added_no_context=0,
                        skipped=0, declined=batch.entity_count,
                    ),
                    reply_markup={"inline_keyboard": []},
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "enrollment_batch_finish_edit_failed",
                    batch_id=batch.id, error=str(e),
                )
        trace_event(
            source=batch.source_kind, recording_id=batch.source_id,
            event="enrollment_batch_finished_zero_selected",
            batch_id=batch.id,
        )
        return batch

    # Mark unselected as declined.
    for p in (batch.prompts or []):
        if not p.selected:
            p.status = STATUS_DECLINED
            p.responded_at = datetime.now(timezone.utc)
    batch.status = STATUS_BATCH_PROCESSING
    batch.current_index = 1  # 1-based into selected[]
    batch.current_step = STEP_CONFIRM_NAME
    session.flush()

    _send_current_prompt(sender, session, batch)
    return batch


# --- Stage 2 / 3 callbacks ------------------------------------

def handle_keep_current_name(
    session: Session,
    *,
    sender: Any,
    batch_id: int,
    actor_user_id: int,
) -> CounterpartyPromptBatch | None:
    """User clicked [Keep] — accept the surface form as the
    canonical name. Advance to context step."""
    batch = session.get(CounterpartyPromptBatch, batch_id)
    if batch is None:
        return None
    if int(batch.user_id) != int(actor_user_id):
        return None
    if (
        batch.status != STATUS_BATCH_PROCESSING
        or batch.current_step != STEP_CONFIRM_NAME
    ):
        return batch
    current = _current_prompt(batch)
    if current is None:
        return batch
    current.canonical_name_corrected = current.mention_text
    current.status = STATUS_PENDING_CONTEXT
    batch.current_step = STEP_CONTEXT
    session.flush()
    _send_current_prompt(sender, session, batch)
    return batch


def handle_skip_current(
    session: Session,
    *,
    sender: Any,
    batch_id: int,
    actor_user_id: int,
) -> CounterpartyPromptBatch | None:
    """User clicked [Skip] in either confirm-name or context
    step. If confirm-name skip: drop entity entirely. If
    context skip: keep entity but no satellite. Then advance to
    next selected."""
    batch = session.get(CounterpartyPromptBatch, batch_id)
    if batch is None:
        return None
    if int(batch.user_id) != int(actor_user_id):
        return None
    if batch.status != STATUS_BATCH_PROCESSING:
        return batch
    current = _current_prompt(batch)
    if current is None:
        return batch
    if batch.current_step == STEP_CONFIRM_NAME:
        # Drop entity entirely.
        current.status = STATUS_COMPLETED_SKIPPED
        current.responded_at = datetime.now(timezone.utc)
    elif batch.current_step == STEP_CONTEXT:
        # Keep entity (use confirmed name), but no satellite.
        cp = _ensure_counterparty(
            session,
            name=current.canonical_name_corrected
            or current.mention_text,
        )
        if cp is not None:
            current.created_counterparty_id = cp.id
        current.status = STATUS_COMPLETED_SKIPPED
        current.responded_at = datetime.now(timezone.utc)
    session.flush()
    _advance_or_finish(sender, session, batch)
    return batch


def receive_text_for_current(
    session: Session,
    *,
    sender: Any,
    batch_id: int,
    text: str,
    actor_user_id: int,
) -> CounterpartyPromptBatch | None:
    """User replied with text or transcribed voice. Routes by
    `batch.current_step`:

    - `confirm_name`: store as `canonical_name_corrected`,
      advance to context step.
    - `context`: store as `context_text`, create the
      Counterparty + telegram_enrollment satellite, advance to
      next selected entity.
    """
    batch = session.get(CounterpartyPromptBatch, batch_id)
    if batch is None:
        return None
    if int(batch.user_id) != int(actor_user_id):
        return None
    if batch.status != STATUS_BATCH_PROCESSING:
        return batch
    text_clean = (text or "").strip()
    if not text_clean:
        return batch
    current = _current_prompt(batch)
    if current is None:
        return batch
    if batch.current_step == STEP_CONFIRM_NAME:
        current.canonical_name_corrected = text_clean[:512]
        current.status = STATUS_PENDING_CONTEXT
        batch.current_step = STEP_CONTEXT
        session.flush()
        _send_current_prompt(sender, session, batch)
        return batch
    if batch.current_step == STEP_CONTEXT:
        cp = _ensure_counterparty(
            session,
            name=current.canonical_name_corrected
            or current.mention_text,
        )
        if cp is not None:
            existing_attr = (
                session.query(CounterpartyAttribute)
                .filter(
                    CounterpartyAttribute.counterparty_id == cp.id,
                    CounterpartyAttribute.source == "telegram_enrollment",
                )
                .one_or_none()
            )
            attrs_payload = {
                "notes": text_clean,
                "via_recording_kind": batch.source_kind,
                "via_recording_id": batch.source_id,
            }
            if existing_attr is not None:
                existing_attr.attributes = attrs_payload
                existing_attr.captured_at = datetime.now(timezone.utc)
            else:
                session.add(
                    CounterpartyAttribute(
                        counterparty_id=cp.id,
                        source="telegram_enrollment",
                        attributes=attrs_payload,
                        captured_at=datetime.now(timezone.utc),
                    )
                )
        current.context_text = text_clean
        current.created_counterparty_id = cp.id if cp else None
        current.status = STATUS_COMPLETED_ADDED
        current.responded_at = datetime.now(timezone.utc)
        session.flush()
        _advance_or_finish(sender, session, batch)
        return batch
    return batch


def find_active_batch_for_user(
    session: Session, *, chat_id: int, user_id: int
) -> CounterpartyPromptBatch | None:
    """Listener helper — when a text/voice arrives without a
    `reply_to`, find the user's currently-processing batch (if
    exactly one)."""
    candidates = (
        session.query(CounterpartyPromptBatch)
        .filter(
            CounterpartyPromptBatch.chat_id == chat_id,
            CounterpartyPromptBatch.user_id == user_id,
            CounterpartyPromptBatch.status == STATUS_BATCH_PROCESSING,
        )
        .all()
    )
    return candidates[0] if len(candidates) == 1 else None


# --- Internal helpers -----------------------------------------

def _current_prompt(
    batch: CounterpartyPromptBatch,
) -> CounterpartyPrompt | None:
    selected = sorted(
        [p for p in (batch.prompts or []) if p.selected],
        key=lambda p: p.index_in_batch or 0,
    )
    if not selected:
        return None
    idx = batch.current_index or 1
    if idx < 1 or idx > len(selected):
        return None
    return selected[idx - 1]


def _selected_total(batch: CounterpartyPromptBatch) -> int:
    return sum(1 for p in (batch.prompts or []) if p.selected)


def _send_current_prompt(
    sender: Any, session: Session, batch: CounterpartyPromptBatch
) -> None:
    current = _current_prompt(batch)
    if current is None:
        return
    total = _selected_total(batch)
    idx = batch.current_index or 1
    if batch.current_step == STEP_CONFIRM_NAME:
        text = _confirm_name_text(
            idx=idx, total=total, mention=current.mention_text
        )
        kb = batch_confirm_name_keyboard(
            batch_id=batch.id, mention_text=current.mention_text
        )
    else:
        name = (
            current.canonical_name_corrected
            or current.mention_text
        )
        text = _context_text(idx=idx, total=total, name=name)
        kb = batch_context_keyboard(batch_id=batch.id)
    try:
        sender.send_message(
            chat_id=batch.chat_id, text=text, reply_markup=kb,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "enrollment_batch_send_step_failed",
            batch_id=batch.id, step=batch.current_step,
            error=str(e),
        )


def _redraw_selection_widget(
    sender: Any, session: Session, batch: CounterpartyPromptBatch
) -> None:
    if not batch.multiselect_message_id:
        return
    prompts = sorted(
        list(batch.prompts or []),
        key=lambda p: p.index_in_batch or 0,
    )
    try:
        sender.update_message(
            chat_id=batch.chat_id,
            message_id=batch.multiselect_message_id,
            text=_selection_text(
                meeting_title="", prompts=prompts,
            ),
            reply_markup=batch_select_keyboard(
                prompts=[
                    {"id": p.id, "index": p.index_in_batch,
                     "selected": p.selected}
                    for p in prompts
                ],
                batch_id=batch.id,
                selected_count=_selected_total(batch),
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "enrollment_batch_redraw_failed",
            batch_id=batch.id, error=str(e),
        )


def _advance_or_finish(
    sender: Any, session: Session, batch: CounterpartyPromptBatch
) -> None:
    total = _selected_total(batch)
    idx = (batch.current_index or 1) + 1
    if idx > total:
        # Done — emit recap.
        added_w = sum(
            1 for p in (batch.prompts or [])
            if p.status == STATUS_COMPLETED_ADDED and p.context_text
        )
        added_no = sum(
            1 for p in (batch.prompts or [])
            if p.status == STATUS_COMPLETED_SKIPPED
            and p.created_counterparty_id is not None
        )
        skipped = sum(
            1 for p in (batch.prompts or [])
            if p.status == STATUS_COMPLETED_SKIPPED
            and p.created_counterparty_id is None
        )
        declined = sum(
            1 for p in (batch.prompts or [])
            if p.status == STATUS_DECLINED
        )
        batch.status = STATUS_BATCH_COMPLETED
        batch.completed_at = datetime.now(timezone.utc)
        batch.current_index = None
        batch.current_step = None
        session.flush()
        if batch.multiselect_message_id:
            try:
                sender.update_message(
                    chat_id=batch.chat_id,
                    message_id=batch.multiselect_message_id,
                    text=_final_recap(
                        added_with_context=added_w,
                        added_no_context=added_no,
                        skipped=skipped, declined=declined,
                    ),
                    reply_markup={"inline_keyboard": []},
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "enrollment_batch_recap_edit_failed",
                    batch_id=batch.id, error=str(e),
                )
        trace_event(
            source=batch.source_kind, recording_id=batch.source_id,
            event="enrollment_batch_completed",
            batch_id=batch.id,
            added_with_context=added_w, added_no_context=added_no,
            skipped=skipped, declined=declined,
        )
        return
    batch.current_index = idx
    batch.current_step = STEP_CONFIRM_NAME
    # Reset the next prompt to confirm-name status so its
    # state lookup makes sense.
    nxt = _current_prompt(batch)
    if nxt is not None:
        nxt.status = STATUS_PENDING_CONFIRM_NAME
    session.flush()
    _send_current_prompt(sender, session, batch)


def _ensure_counterparty(
    session: Session, *, name: str
) -> Counterparty | None:
    norm = normalise_name(name or "")
    if not norm:
        return None
    existing = (
        session.query(Counterparty)
        .filter(Counterparty.name_normalised == norm)
        .one_or_none()
    )
    if existing is not None:
        return existing
    # FR-CR-05-230 — auto-enroll kill-switch (default off).
    from app.config import get_settings

    if not get_settings().counterparty_autoenroll_enabled:
        return None
    cp = Counterparty(name=name, name_normalised=norm)
    session.add(cp)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return (
            session.query(Counterparty)
            .filter(Counterparty.name_normalised == norm)
            .one_or_none()
        )
    return cp


__all__ = [
    "BatchPostResult",
    "post_enrollment_batch",
    "handle_toggle",
    "handle_next",
    "handle_keep_current_name",
    "handle_skip_current",
    "receive_text_for_current",
    "find_active_batch_for_user",
]
