from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActionDraft, ActionDraftState, Task, TaskStatusHistory
from app.models.task import TaskPriority, TaskStatus


_TITLE_HARD_CAP = 80


# FR-CR-05-99 — operator: «таких тем тоже быть не должно,
# встретиться с кем-то и тд». Bare verbs without an object /
# addressee leave no actionable signal — the operator can't
# tell with whom / about what. Reject at the draft-prep step.
_BARE_VERB_TITLES: frozenset[str] = frozenset(
    {
        # Russian — meeting / contact verbs that demand an object
        "встретиться",
        "созвониться",
        "пообщаться",
        "обсудить",
        "поговорить",
        "позвонить",
        "написать",
        # Russian — work verbs that demand a deliverable
        "организовать",
        "подготовить",
        "сделать",
        "доделать",
        "отправить",
        "проверить",
        "уточнить",
        "узнать",
        "напомнить",
        "ответить",
        # English equivalents
        "meet",
        "discuss",
        "call",
        "schedule",
        "organize",
        "prepare",
        "send",
        "check",
        "clarify",
        "remind",
        "follow up",
        "follow-up",
    }
)


def is_naked_verb_title(title: str) -> bool:
    """FR-CR-05-99 — return True when `title` is a bare action
    verb with no object / addressee / topic. Used by
    `prepare_drafts` to drop such drafts at intake — the LLM
    occasionally emits «Встретиться» / «Организовать» with no
    complement, and the operator can't act on those.
    """
    if not title:
        return False
    norm = title.strip().lower().rstrip(".!?,;:—-«»\"' ")
    # Single-word OR a 2-word ending like «follow up» / «follow-up».
    if norm in _BARE_VERB_TITLES:
        return True
    return False


def normalize_task_title(raw: Any) -> str:
    """FR-CR-05-72 / -75 / -89 / -95 — produce a one-glance title.

    Pipeline:
      1. Strip whitespace; raise on empty.
      2. Capitalize the first character (works for Cyrillic).
      3. If multi-line, keep the FIRST line only.
      4. If still ≥80 chars, trim at the first strong break
         (`: `, ` — `, ` - `, `; `, `. `, `? `, `! `, `, `)
         sitting between offset 8 and 80 — usually ends the
         action-verb clause.
      5. Fallback: word-boundary cut at 80 + ellipsis. This is
         the FR-CR-05-95 backstop — operator's hard rule
         «никогда названий длинных быть не может, все в
         описании!». 80 is tighter than the previous 100 so
         even a screenshot-transcript dump leaves room for
         the bullet emoji + space in the rendered widget.

    Reused from `ops.resync_sheet` to retro-cap legacy rows.
    """
    title = (raw or "").strip()
    if not title:
        raise ValueError("Task title is required")
    if not title[0].isupper():
        title = title[0].upper() + title[1:]
    if "\n" in title:
        title = title.split("\n", 1)[0].strip()
    cap = _TITLE_HARD_CAP
    if len(title) > cap:
        for sep in (": ", " — ", " - ", "; ", ". ", "? ", "! ", ", "):
            idx = title.find(sep)
            if 8 <= idx <= cap:
                title = title[:idx].rstrip(",.!?;: ")
                break
    if len(title) > cap:
        cut = title[:cap].rstrip()
        last_ws = max(cut.rfind(" "), cut.rfind("—"), cut.rfind("-"))
        if last_ws > 50:
            cut = cut[:last_ws].rstrip()
        title = cut + "…"
    return title


def _coerce_due(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_due_time(value: Any) -> time | None:
    """Parse an HH:MM string (or `time`) into a `datetime.time`.
    Returns None on anything else — the caller falls back to the
    18:00 default per FR-CR-05-63."""
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_priority(value: Any) -> TaskPriority:
    try:
        return TaskPriority(value)
    except (ValueError, TypeError):
        return TaskPriority.medium


def _initial_status(due: date | None, today: date | None = None) -> TaskStatus:
    """CR-01: tasks due within a week land in To Do; others sit in Backlog."""
    if due is None:
        return TaskStatus.backlog
    today = today or date.today()
    return TaskStatus.todo if due <= today + timedelta(days=7) else TaskStatus.backlog


def create_task_from_draft(
    session: Session,
    *,
    draft: ActionDraft,
    source: dict[str, Any],
    context_snapshot_id: int | None,
    fallback_author_slack_id: str | None,
) -> Task:
    """Persist a Task from a confirmed draft and mark the draft as confirmed.

    Also writes the initial TaskStatusHistory row and auto-subscribes the
    owner and source-message author (CR-01).
    """

    payload: dict[str, Any] = draft.payload or {}
    # Truncate every user-provided string to a sensible upper bound
    # (FR-CR-XX). Forwarded chat threads / pasted documents can in
    # principle blow past the Sheets cell limit (50 000 chars) and
    # bloat downstream LLM prompts. 10 000 chars is generous enough
    # to keep useful detail and well under every downstream cap.
    _MAX = 10_000

    def _cap(v: Any) -> Any:
        if isinstance(v, str) and len(v) > _MAX:
            return v[:_MAX]
        return v

    title = normalize_task_title(payload.get("title"))

    owner_user_id = _cap(payload.get("owner_user_id"))
    owner_display_name = _cap(payload.get("owner_display_name"))
    # The upstream pipeline already populates owner_user_id = author
    # with owner_assumed=True when no explicit assignee is present
    # (see app/slack_bot/handlers/shared.py). Respect that flag so the
    # "(предположительно)" label survives into task.extra.
    owner_assumed = bool(payload.get("owner_assumed"))
    if not owner_user_id and not owner_display_name:
        # Extra safety net for code paths that bypass classify_and_persist
        # (e.g. the raw @mention fallback when the LLM timed out).
        owner_user_id = fallback_author_slack_id
        if owner_user_id:
            owner_assumed = True

    due = _coerce_due(payload.get("due_date"))
    # FR-CR-05-63 — default deadline = today 18:00 when the LLM
    # didn't pull a date out of the source. Operator wants every
    # captured task to have a deadline so the morning / evening
    # digests can include it; «когда-нибудь» / «потом» tasks
    # leak to nowhere otherwise.
    if due is None:
        due = date.today()
    due_time = _coerce_due_time(payload.get("due_time"))
    if due_time is None:
        due_time = time(18, 0)
    status = _initial_status(due)

    # FR-CR-04-26: discriminate Slack vs Telegram tasks. Source dict
    # may carry `kind` ('slack' | 'telegram'); defaults to slack for
    # back-compat with all existing call sites.
    from app.models import TaskSourceKind

    source_kind_raw = (source.get("kind") or "slack")
    try:
        source_kind = TaskSourceKind(source_kind_raw)
    except ValueError:
        source_kind = TaskSourceKind.slack

    extra: dict[str, Any] = {}
    if owner_assumed:
        extra["owner_assumed"] = True

    task = Task(
        title=title,
        description=_cap(payload.get("description")),
        owner_user_id=owner_user_id,
        owner_display_name=_cap(payload.get("owner_display_name")),
        priority=_coerce_priority(payload.get("priority", "medium")),
        due_date=due,
        due_time=due_time,
        status=status,
        is_current_week=(status == TaskStatus.todo),
        estimated_minutes=payload.get("estimated_minutes"),
        source_kind=source_kind,
        source_conversation_id=source.get("conversation_id"),
        source_message_ts=source.get("message_ts"),
        source_thread_ts=source.get("thread_ts"),
        source_permalink=source.get("permalink"),
        context_snapshot_id=context_snapshot_id,
        created_by_slack_user_id=draft.created_by_slack_user_id or fallback_author_slack_id,
        extra=extra or None,
    )
    session.add(task)
    draft.state = ActionDraftState.confirmed
    session.flush()
    # Remember the link so follow-up thread replies can edit the task
    # directly (CR-03 always-create flows).
    draft.task_id = task.id
    session.flush()

    # Initial history row (from_status = None).
    session.add(
        TaskStatusHistory(
            task_id=task.id,
            from_status=None,
            to_status=status,
            changed_by_slack_user_id=task.created_by_slack_user_id,
            reason="created",
        )
    )

    # Auto-subscribe owner and source author (de-duplicated).
    from app.services.subscriptions import SubscriptionService

    subs = SubscriptionService()
    for uid in {owner_user_id, fallback_author_slack_id} - {None, ""}:
        if uid:
            subs.subscribe(session, task=task, slack_user_id=uid)

    session.flush()

    # FR-CR-04-23 / FR-CR-04-26 — every newly persisted Task gets an
    # initial Google Sheets row. Schedule via `after_commit` so the
    # syncer's fresh session sees the row (a plain `sync_task()` call
    # here would fire BEFORE the outer session_scope commits, and the
    # syncer would silently no-op on the missing row). Centralised
    # here so any caller — Slack, Telegram immediate-create, the
    # FR-CR-05-05 multi-task loop, the FR-CR-04-32 Accept-on-draft
    # handler — gets the sync for free. Best-effort: a sheets outage
    # must not abort task creation.
    try:
        from app.sync.task_sync import schedule_sync_task

        schedule_sync_task(session, task.id)
    except Exception:  # noqa: BLE001
        pass
    return task


def summarize_task(task: Task) -> str:
    parts = [task.title]
    if task.due_date:
        parts.append(f"due {task.due_date.isoformat()}")
    if task.owner_display_name:
        parts.append(f"owner {task.owner_display_name}")
    return " · ".join(parts)
