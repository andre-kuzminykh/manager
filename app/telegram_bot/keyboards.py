"""Inline-keyboard builders for the Telegram bot.

Telegram's button layer uses callback_data strings — there's no
``action_id``/``value`` split like Slack's Block Kit. We pack action
+ entity id into a single string with a ``:`` separator and parse it
on the way back. This is the same shape the Slack handlers use, so
porting the action graph is straightforward.

callback_data format: ``"<action>:<entity_id>"`` where action is one
of ``confirm`` / ``ignore`` / ``start`` / ``done`` / ``cancel`` /
``delete`` / ``edit`` / ``subscribe`` / ``unsubscribe``, and
entity_id is the draft id or task id (a positive integer).
"""
from __future__ import annotations

from typing import Any

ACTION_CONFIRM = "confirm"
ACTION_IGNORE = "ignore"
ACTION_START = "start"
ACTION_DONE = "done"
ACTION_CANCEL = "cancel"
ACTION_DELETE = "delete"
ACTION_EDIT = "edit"
ACTION_SUBSCRIBE = "subscribe"
ACTION_UNSUBSCRIBE = "unsubscribe"
# FR-CR-05-133 — enrollment widget for unresolved counterparty
# mentions. Three buttons across two stages:
#   stage 1: ACTION_ENROLL_YES / ACTION_ENROLL_NO
#   stage 2 (after Yes): ACTION_ENROLL_SKIP (or a free-text /
#   voice reply, which goes through PendingRegistry instead)
# entity_id in the callback_data is `CounterpartyPrompt.id`.
ACTION_ENROLL_YES = "enroll_yes"
ACTION_ENROLL_NO = "enroll_no"
ACTION_ENROLL_SKIP = "enroll_skip"
# FR-CR-05-138 — batch multi-select enrollment widget. Two
# stages of buttons:
#   stage 1 — numpad toggles + finish
#     ACTION_BATCH_TOGGLE — toggles index_in_batch on a prompt
#     ACTION_BATCH_NEXT   — finalise selection, start processing
#   stage 2 — per-entity name confirm + skip
#     ACTION_BATCH_KEEP_NAME — keep mention surface form as-is
#     ACTION_BATCH_SKIP_ENTITY — skip current entity, advance
# entity_id encoding (single-int slot in callback_data):
#   ACTION_BATCH_TOGGLE: prompt_id (the toggled CounterpartyPrompt)
#   all others: batch_id
ACTION_BATCH_TOGGLE = "btoggle"
ACTION_BATCH_NEXT = "bnext"
ACTION_BATCH_KEEP_NAME = "bkeep"
ACTION_BATCH_SKIP_ENTITY = "bskip"


def _btn(text: str, action: str, entity_id: int) -> dict[str, Any]:
    return {"text": text, "callback_data": f"{action}:{entity_id}"}


def _row(*buttons: dict[str, Any]) -> list[dict[str, Any]]:
    return list(buttons)


def confirm_keyboard(*, draft_id: int) -> dict[str, Any]:
    """Inline keyboard for a draft card.

    FR-CR-05-34 — order is **Reject / Edit / Accept** so the
    «commit» button is the rightmost / last-tap one. Operator
    feedback: tapping Accept by accident (it was first) on a
    not-yet-reviewed draft was easy. Reject on the left makes
    the destructive button the safe «I'm out» choice and Accept
    the «I read this and confirm» commit.
    """
    return {
        "inline_keyboard": [
            _row(
                _btn("✖ Reject", ACTION_IGNORE, draft_id),
                _btn("✏ Edit", ACTION_EDIT, draft_id),
                _btn("✅ Accept", ACTION_CONFIRM, draft_id),
            )
        ]
    }


def task_card_keyboard(
    *, task_id: int, status: str, is_owner: bool, is_admin: bool, subscribed: bool
) -> dict[str, Any]:
    """Inline keyboard for a confirmed task card.

    Permission model:
    - **▶ Start** — only the OWNER can start their own task (admins
      and bystanders see no Start button).
    - **✔ Mark done / ✏ Edit / 🗑 Delete** — owner OR admin.
    - **🔔 Subscribe / 🔕 Unsubscribe** — anyone EXCEPT the owner;
      the owner is auto-subscribed at creation, the toggle is a
      no-op for them, so we hide it.

    Layout: row 1 — primary action (Start / Mark done) when
    available; row 2 — Edit + Delete side-by-side; row 3 —
    Subscribe / Unsubscribe.
    """
    rows: list[list[dict[str, Any]]] = []
    primary: list[dict[str, Any]] = []

    # ▶ Start — owner only. Even an unowned task no longer surfaces
    # a Start button to bystanders / admins; if no owner is set the
    # task simply has no Start row until someone is assigned.
    if status in ("backlog", "todo") and is_owner:
        primary.append(_btn("▶ Start", ACTION_START, task_id))
    elif status == "in_progress" and (is_owner or is_admin):
        primary.append(_btn("✔ Mark done", ACTION_DONE, task_id))
    if primary:
        rows.append(primary)

    # Edit + Delete share the second row.
    secondary: list[dict[str, Any]] = []
    if status != "done" and (is_owner or is_admin):
        secondary.append(_btn("✏ Edit", ACTION_EDIT, task_id))
    if is_owner or is_admin:
        secondary.append(_btn("🗑 Delete", ACTION_DELETE, task_id))
    if secondary:
        rows.append(secondary)

    sub_row: list[dict[str, Any]] = []
    if status != "done" and not is_owner:
        if subscribed:
            sub_row.append(_btn("🔕 Unsubscribe", ACTION_UNSUBSCRIBE, task_id))
        else:
            sub_row.append(_btn("🔔 Subscribe", ACTION_SUBSCRIBE, task_id))
    if sub_row:
        rows.append(sub_row)

    return {"inline_keyboard": rows}


def enrollment_yesno_keyboard(*, prompt_id: int) -> dict[str, Any]:
    """FR-CR-05-133 stage 1 — «Track this entity?» widget.

    English-only labels (operator-pinned). Layout: [Yes] [No]
    side-by-side; tapping either tags the row in the DB and
    edits the message in place to the next stage / a terminal
    note. `prompt_id` is `CounterpartyPrompt.id`.
    """
    return {
        "inline_keyboard": [
            _row(
                _btn("Yes", ACTION_ENROLL_YES, prompt_id),
                _btn("No", ACTION_ENROLL_NO, prompt_id),
            )
        ]
    }


def enrollment_skip_keyboard(*, prompt_id: int) -> dict[str, Any]:
    """FR-CR-05-133 stage 2 — «Send text or voice context, or
    Skip» widget.

    Skip is a single button. Text / voice replies are picked up
    by the in-memory `PendingRegistry` (registered when stage 1
    Yes was clicked). English-only label (operator-pinned).
    """
    return {
        "inline_keyboard": [
            _row(_btn("Skip", ACTION_ENROLL_SKIP, prompt_id)),
        ]
    }


def batch_select_keyboard(
    *,
    prompts: list[dict[str, Any]],
    batch_id: int,
    selected_count: int,
) -> dict[str, Any]:
    """FR-CR-05-138 stage 1 — numpad of N buttons (2-per-row)
    + final [Next →]. `prompts` is a list of
    `{id: int, index: int, selected: bool}` ordered by
    `index_in_batch`. Selected indices show with a leading ✅.

    Up to 10 entities = 5 buttons per row × 2 rows. Beyond 10
    we just keep going 5 per row — operator's UX target was 10
    so this is a graceful overflow rather than a paginator (a
    paginator can be added in v2.1 once needed).
    """
    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for p in prompts:
        idx = p["index"]
        label = f"✅{idx}" if p.get("selected") else str(idx)
        current.append(_btn(label, ACTION_BATCH_TOGGLE, p["id"]))
        if len(current) == 5:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    next_label = (
        f"Next →  ({selected_count} selected)"
        if selected_count
        else "Next →"
    )
    rows.append([_btn(next_label, ACTION_BATCH_NEXT, batch_id)])
    return {"inline_keyboard": rows}


def batch_confirm_name_keyboard(
    *, batch_id: int, mention_text: str
) -> dict[str, Any]:
    """FR-CR-05-138 stage 2 — confirm or skip the surface form
    as the canonical name. (User can also reply with text/voice
    to suggest a corrected name; that flows through
    PendingRegistry.) Trim mention to fit Telegram's button-
    text 64-byte cap."""
    keep_label = f"✅ Keep \"{(mention_text or '')[:30]}\""
    return {
        "inline_keyboard": [
            _row(
                _btn(keep_label, ACTION_BATCH_KEEP_NAME, batch_id),
                _btn("⏭ Skip", ACTION_BATCH_SKIP_ENTITY, batch_id),
            ),
        ]
    }


def batch_context_keyboard(*, batch_id: int) -> dict[str, Any]:
    """FR-CR-05-138 stage 3 — single Skip button. Text/voice
    reply provides the context."""
    return {
        "inline_keyboard": [
            _row(_btn("⏭ Skip", ACTION_BATCH_SKIP_ENTITY, batch_id)),
        ]
    }


def parse_callback_data(data: str) -> tuple[str, int] | None:
    """Inverse of `_btn` — used by the inbound callback handler."""
    try:
        action, entity = data.split(":", 1)
        return action, int(entity)
    except (ValueError, AttributeError):
        return None
