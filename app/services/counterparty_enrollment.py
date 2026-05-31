"""FR-CR-05-133 — enrollment service for counterparty mentions
that didn't resolve to the directory.

After a meeting pipeline finishes Pass 2
(`resolve_mentions_to_directory`), unresolved mentions
(`directory_id is None`) flow into this service. For each
unresolved mention × each admin recipient we:

  1. POST a stage-1 widget «Track «<name>»? [Yes] [No]» to the
     recipient's DM and persist a `CounterpartyPrompt` row
     keyed by `(source_kind, source_id, mention_normalised,
     user_id)` so reruns don't double-post.
  2. On [Yes]: edit the widget to «Send text or voice context,
     or [Skip]», flip status → `awaiting_context`, and let the
     listener register an in-memory PendingQuestion so the next
     reply lands back on `complete_with_context`.
  3. On [No]: edit the widget to «Won't track «<name>».», flip
     status → `declined`. No DB write to `counterparties`.
  4. On [Skip]: create a `Counterparty` with name = mention
     surface form (no notes), edit widget to «Added «<name>»
     to the directory.», flip status → `completed_skipped`.
  5. On text/voice reply: create a `Counterparty` + a
     `CounterpartyAttribute` (source=`telegram_enrollment`,
     attributes={notes, via_meeting}), edit widget to «Added
     «<name>» to the directory with context.», flip status →
     `completed_added`.

All Telegram-facing strings are English (operator-pinned).
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
)
from app.models.counterparty_prompt import (
    STATUS_AWAITING_CONTEXT,
    STATUS_COMPLETED_ADDED,
    STATUS_COMPLETED_SKIPPED,
    STATUS_DECLINED,
    STATUS_PENDING_YESNO,
)
from app.services.trace_log import trace_event
from app.sync.counterparties import normalise_name
from app.telegram_bot.keyboards import (
    enrollment_skip_keyboard,
    enrollment_yesno_keyboard,
)

log = get_logger(__name__)


# --- Telegram-facing copy (English, operator-pinned) ----------

def _stage1_text(mention: str) -> str:
    return (
        f"🔍 I picked up «{mention}» in a meeting transcript "
        f"but it isn't in the counterparties directory.\n\n"
        f"Track this entity?"
    )


def _stage2_text(mention: str) -> str:
    return (
        f"Got it — adding «{mention}» to the directory.\n\n"
        f"Send any context (text or voice), or press Skip to "
        f"add it without notes."
    )


def _terminal_added_with_context_text(mention: str) -> str:
    return f"✅ Added «{mention}» to the directory with context."


def _terminal_added_no_context_text(mention: str) -> str:
    return f"✅ Added «{mention}» to the directory (no context)."


def _terminal_declined_text(mention: str) -> str:
    return f"❌ Won't track «{mention}»."


def _terminal_failed_text(mention: str) -> str:
    return (
        f"⚠️ Couldn't add «{mention}» — saving for retry. "
        f"You can re-add it manually in the Sheet."
    )


# --- Public API -----------------------------------------------

@dataclass
class EnrollmentPostResult:
    """Aggregate result of `post_enrollment_prompts` so the
    pipeline can log how many widgets actually went out."""

    posted: int
    skipped_existing: int
    failed: int


def post_enrollment_prompts(
    session: Session,
    *,
    sender: Any,
    source_kind: str,
    source_id: str,
    unresolved_mentions: Iterable[str],
    recipient_user_ids: Iterable[int],
) -> EnrollmentPostResult:
    """Post the stage-1 widget for every (mention × recipient).

    Idempotent: skips any (mention, recipient) pair that already
    has a `CounterpartyPrompt` row. Survives Telegram-side send
    failures by leaving the row's `yesno_message_id` NULL — the
    next pipeline rerun will skip it (UNIQUE constraint), so we
    rely on operator-side cleanup if the bot was offline.
    """
    posted = 0
    skipped_existing = 0
    failed = 0

    # Dedupe by `mention_normalised` first so phonetically
    # identical surface forms («Тезер» / «тезер») emit ONE
    # widget per recipient. Picks the longest surface form as
    # the display value (more informative for the operator).
    by_norm: dict[str, str] = {}
    for m in unresolved_mentions:
        norm = normalise_name(m)
        if not norm:
            continue
        existing = by_norm.get(norm)
        if existing is None or len(m) > len(existing):
            by_norm[norm] = m

    recipient_list = [int(u) for u in recipient_user_ids]
    if not by_norm or not recipient_list:
        return EnrollmentPostResult(posted=0, skipped_existing=0, failed=0)

    for norm, mention in by_norm.items():
        for uid in recipient_list:
            existing = (
                session.query(CounterpartyPrompt)
                .filter(
                    CounterpartyPrompt.source_kind == source_kind,
                    CounterpartyPrompt.source_id == source_id,
                    CounterpartyPrompt.mention_normalised == norm,
                    CounterpartyPrompt.user_id == uid,
                )
                .one_or_none()
            )
            if existing is not None:
                skipped_existing += 1
                continue

            row = CounterpartyPrompt(
                source_kind=source_kind,
                source_id=source_id,
                mention_text=mention,
                mention_normalised=norm,
                chat_id=uid,  # DM: chat_id == user_id
                user_id=uid,
                status=STATUS_PENDING_YESNO,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                # Race: another worker raced us in. Roll back
                # this row only and treat as «skip existing».
                session.rollback()
                skipped_existing += 1
                continue

            try:
                resp = sender.send_message(
                    chat_id=uid,
                    text=_stage1_text(mention),
                    reply_markup=enrollment_yesno_keyboard(
                        prompt_id=row.id
                    ),
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "counterparty_enrollment_send_failed",
                    prompt_id=row.id, uid=uid, error=str(e),
                )
                failed += 1
                continue

            msg_id = (resp or {}).get("message_id")
            if msg_id:
                row.yesno_message_id = int(msg_id)
                session.flush()
                posted += 1
            else:
                failed += 1

    trace_event(
        source=source_kind, recording_id=source_id,
        event="counterparty_enrollment_posted",
        unique_mentions=len(by_norm),
        recipients=len(recipient_list),
        posted=posted, skipped_existing=skipped_existing,
        failed=failed,
    )
    log.info(
        "counterparty_enrollment_posted",
        source_kind=source_kind, source_id=source_id,
        unique_mentions=len(by_norm), recipients=len(recipient_list),
        posted=posted, skipped_existing=skipped_existing,
        failed=failed,
    )
    return EnrollmentPostResult(
        posted=posted,
        skipped_existing=skipped_existing,
        failed=failed,
    )


def handle_yes(
    session: Session,
    *,
    sender: Any,
    prompt_id: int,
    actor_user_id: int | None = None,
) -> CounterpartyPrompt | None:
    """Stage 1 → Stage 2 transition. Edits the widget in place
    to «Send text or voice context, or Skip». Returns the row
    so the listener can register an in-memory PendingQuestion
    against `(chat_id, user_id, context_message_id)` (which
    falls back to `yesno_message_id` if the edit didn't return
    a fresh id).
    """
    row = session.get(CounterpartyPrompt, prompt_id)
    if row is None:
        return None
    if actor_user_id is not None and int(row.user_id) != int(actor_user_id):
        # Another admin clicked; ignore — only the row's own
        # recipient owns the flow.
        return None
    if row.status != STATUS_PENDING_YESNO:
        # Idempotency: a second click after Yes shouldn't
        # re-flow. Return the row so the listener can re-attach
        # the pending registration if needed.
        return row

    row.status = STATUS_AWAITING_CONTEXT
    row.responded_at = datetime.now(timezone.utc)
    session.flush()

    if row.yesno_message_id:
        try:
            resp = sender.update_message(
                chat_id=row.chat_id,
                message_id=row.yesno_message_id,
                text=_stage2_text(row.mention_text),
                reply_markup=enrollment_skip_keyboard(prompt_id=row.id),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "counterparty_enrollment_yes_edit_failed",
                prompt_id=row.id, error=str(e),
            )
            resp = {}
        # Telegram returns the EDITED message; reuse the
        # existing message_id for context tracking. Fall back
        # to the stage-1 id if the response was empty.
        new_id = (resp or {}).get("message_id") or row.yesno_message_id
        row.context_message_id = int(new_id)
        session.flush()

    trace_event(
        source=row.source_kind, recording_id=row.source_id,
        event="counterparty_enrollment_yes",
        prompt_id=row.id, mention=row.mention_text,
    )
    return row


def handle_no(
    session: Session,
    *,
    sender: Any,
    prompt_id: int,
    actor_user_id: int | None = None,
) -> CounterpartyPrompt | None:
    """Stage 1 → Declined terminal. Edits widget in place to a
    confirmation, drops the keyboard."""
    row = session.get(CounterpartyPrompt, prompt_id)
    if row is None:
        return None
    if actor_user_id is not None and int(row.user_id) != int(actor_user_id):
        return None
    if row.status != STATUS_PENDING_YESNO:
        return row

    row.status = STATUS_DECLINED
    row.responded_at = datetime.now(timezone.utc)
    session.flush()

    if row.yesno_message_id:
        try:
            sender.update_message(
                chat_id=row.chat_id,
                message_id=row.yesno_message_id,
                text=_terminal_declined_text(row.mention_text),
                reply_markup={"inline_keyboard": []},
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "counterparty_enrollment_no_edit_failed",
                prompt_id=row.id, error=str(e),
            )

    trace_event(
        source=row.source_kind, recording_id=row.source_id,
        event="counterparty_enrollment_no",
        prompt_id=row.id, mention=row.mention_text,
    )
    return row


def handle_skip(
    session: Session,
    *,
    sender: Any,
    prompt_id: int,
    actor_user_id: int | None = None,
) -> CounterpartyPrompt | None:
    """Stage 2 → Completed-skipped terminal. Creates a
    Counterparty hub with `name = mention` (no satellite
    attributes), edits widget to confirmation."""
    row = session.get(CounterpartyPrompt, prompt_id)
    if row is None:
        return None
    if actor_user_id is not None and int(row.user_id) != int(actor_user_id):
        return None
    if row.status != STATUS_AWAITING_CONTEXT:
        return row

    cp = _ensure_counterparty(session, name=row.mention_text)
    row.created_counterparty_id = cp.id if cp else None
    row.status = STATUS_COMPLETED_SKIPPED
    row.responded_at = datetime.now(timezone.utc)
    session.flush()

    msg_id = row.context_message_id or row.yesno_message_id
    if msg_id:
        try:
            sender.update_message(
                chat_id=row.chat_id,
                message_id=msg_id,
                text=(
                    _terminal_added_no_context_text(row.mention_text)
                    if cp is not None
                    else _terminal_failed_text(row.mention_text)
                ),
                reply_markup={"inline_keyboard": []},
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "counterparty_enrollment_skip_edit_failed",
                prompt_id=row.id, error=str(e),
            )

    trace_event(
        source=row.source_kind, recording_id=row.source_id,
        event="counterparty_enrollment_skip",
        prompt_id=row.id, mention=row.mention_text,
        counterparty_id=row.created_counterparty_id,
    )
    return row


def complete_with_context(
    session: Session,
    *,
    sender: Any,
    prompt_id: int,
    context_text: str,
    actor_user_id: int | None = None,
) -> CounterpartyPrompt | None:
    """Stage 2 → Completed-added terminal. Creates a
    Counterparty hub plus a CounterpartyAttribute satellite
    with the operator's notes, edits widget to confirmation.
    `context_text` is the already-cleaned reply (Whisper
    transcription, if voice)."""
    row = session.get(CounterpartyPrompt, prompt_id)
    if row is None:
        return None
    if actor_user_id is not None and int(row.user_id) != int(actor_user_id):
        return None
    if row.status != STATUS_AWAITING_CONTEXT:
        return row
    text = (context_text or "").strip()
    if not text:
        # Empty reply — caller should have nudged the user; we
        # don't transition.
        return row

    cp = _ensure_counterparty(session, name=row.mention_text)
    if cp is not None:
        # Attach satellite. Source label «telegram_enrollment»
        # so it's distinguishable from sheet-pulled attrs.
        existing_attr = (
            session.query(CounterpartyAttribute)
            .filter(
                CounterpartyAttribute.counterparty_id == cp.id,
                CounterpartyAttribute.source == "telegram_enrollment",
            )
            .one_or_none()
        )
        attrs_payload = {
            "notes": text,
            "via_recording_kind": row.source_kind,
            "via_recording_id": row.source_id,
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
        session.flush()

    row.context_text = text
    row.created_counterparty_id = cp.id if cp else None
    row.status = STATUS_COMPLETED_ADDED
    row.responded_at = datetime.now(timezone.utc)
    session.flush()

    msg_id = row.context_message_id or row.yesno_message_id
    if msg_id:
        try:
            sender.update_message(
                chat_id=row.chat_id,
                message_id=msg_id,
                text=(
                    _terminal_added_with_context_text(row.mention_text)
                    if cp is not None
                    else _terminal_failed_text(row.mention_text)
                ),
                reply_markup={"inline_keyboard": []},
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "counterparty_enrollment_context_edit_failed",
                prompt_id=row.id, error=str(e),
            )

    trace_event(
        source=row.source_kind, recording_id=row.source_id,
        event="counterparty_enrollment_completed_with_context",
        prompt_id=row.id, mention=row.mention_text,
        counterparty_id=row.created_counterparty_id,
        context_chars=len(text),
    )
    return row


# --- Internal helpers -----------------------------------------

def _ensure_counterparty(
    session: Session, *, name: str
) -> Counterparty | None:
    """Find-or-create a Counterparty hub for `name`. Returns the
    existing row (if `name_normalised` already exists) or the
    newly-inserted one. Returns None on integrity failure."""
    norm = normalise_name(name)
    if not norm:
        return None
    existing = (
        session.query(Counterparty)
        .filter(Counterparty.name_normalised == norm)
        .one_or_none()
    )
    if existing is not None:
        return existing
    # FR-CR-05-230 — do not mint new cards for unmatched mentions when
    # auto-enrollment is off (default). Keeps the directory clean; new
    # counterparties enter only via the Google-Sheet sync.
    from app.config import get_settings

    if not get_settings().counterparty_autoenroll_enabled:
        return None
    cp = Counterparty(name=name, name_normalised=norm)
    session.add(cp)
    try:
        session.flush()
    except IntegrityError:
        # Lost a race — re-fetch.
        session.rollback()
        existing = (
            session.query(Counterparty)
            .filter(Counterparty.name_normalised == norm)
            .one_or_none()
        )
        return existing
    return cp


__all__ = [
    "EnrollmentPostResult",
    "post_enrollment_prompts",
    "handle_yes",
    "handle_no",
    "handle_skip",
    "complete_with_context",
]
