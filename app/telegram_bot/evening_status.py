"""FR-CR-05-40 — Evening status report (per-task LLM narrative).

For each Telegram user the bot DMs every evening:

  - ✅ Done сегодня — tasks the user owned that flipped to
    `done` at any point today.
  - 🚀 В процессе — tasks the user owns that are
    `in_progress`.
  - 📋 Todo на завтра — tasks the user owns that are
    `backlog` / `todo` and stay open into tomorrow.
  - 👀 Подписки — open tasks the user is subscribed to but
    doesn't own.

Each task line gets a 1-2 sentence narrative composed by the
LLM from the task's status history + description + last
context snippet. Per-task call so different threads of
discussion get processed under the same prompt — batching
would force one giant prompt that smudges the boundaries
between tasks.

Long reports auto-split at section boundaries to fit under
Telegram's 4096-char per-message cap. Idempotent per
(user, date) via `audit_logs`.

Real-time updates (e.g. status change → instant DM) are out
of scope here; this is the daily digest. The same
`compose_status_narrative` helper will drop straight into
that flow when we add it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    AuditLog,
    Task,
    TaskStatus,
    TaskStatusHistory,
    TaskSubscription,
)
from app.telegram_bot.handlers import admin_user_ids
from app.telegram_bot.notifications import (
    _is_telegram_user_id,
    _telegram_owner_ids,
)
from app.telegram_bot.sender import (
    PRIORITY_EMOJI,
    TelegramSender,
    _escape_html,
    _handle_from_display,
    _owner_html_link,
    _resolve_owner_display,
    _resolve_owner_link_target,
)

log = get_logger(__name__)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)
_TG_MESSAGE_HARD_CAP = 4096
# Leave ~10% headroom so a renderer rounding error or a stray
# entity-encoded char doesn't push us over.
_SPLIT_TARGET = 3800

_NARRATIVE_SYSTEM = (
    "Ты — секретарь по задачам. Получаешь карточку задачи + "
    "последние транзишены статуса. Пиши на РУССКОМ ровно одну "
    "строку (макс 220 символов): что произошло за сегодня по "
    "задаче и где она сейчас. Не дублируй заголовок — он уже "
    "виден отдельно. Не добавляй эмодзи и Markdown — только "
    "обычный текст. Если транзишенов сегодня не было, опиши "
    "текущий статус задачи одной короткой фразой. Не выдумывай "
    "факты — пиши только то, что есть в карточке / истории."
)


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #


@dataclass
class _TaskGroup:
    """One section of the report (Done / In progress / Todo /
    Subscriptions). Header carries the section title with its emoji
    + count, lines are pre-rendered HTML strings."""

    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class EveningStatusReport:
    """Counters returned by `send_evening_status_report` so the
    cron caller can log what happened (mirrors
    `TelegramDigestReport`)."""

    recipients: int = 0
    messages_sent: int = 0
    tasks_described: int = 0
    skipped_idempotent: int = 0
    skipped_no_tasks: int = 0
    failures: int = 0


# --------------------------------------------------------------------------- #
# Audit / idempotency
# --------------------------------------------------------------------------- #


_CATEGORY = "telegram_evening_status"


def _already_sent(session: Session, *, user_id: str, day: date) -> bool:
    return (
        session.query(AuditLog)
        .filter(
            AuditLog.category == _CATEGORY,
            AuditLog.actor == user_id,
            AuditLog.entity_id == day.isoformat(),
        )
        .first()
        is not None
    )


def _mark_sent(
    session: Session, *, user_id: str, day: date, payload: dict
) -> None:
    session.add(
        AuditLog(
            category=_CATEGORY,
            action="evening",
            entity_type=_CATEGORY,
            entity_id=day.isoformat(),
            actor=user_id,
            payload=payload,
        )
    )
    session.flush()


# --------------------------------------------------------------------------- #
# Task selection
# --------------------------------------------------------------------------- #


def _day_bounds(today: date) -> tuple[datetime, datetime]:
    start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _done_today_for_owner(
    session: Session, *, owner_uid: str, today: date
) -> list[Task]:
    """Tasks the owner closed at any point today (history-driven —
    a task closed and re-opened still counts)."""
    start, end = _day_bounds(today)
    return (
        session.query(Task)
        .join(TaskStatusHistory, TaskStatusHistory.task_id == Task.id)
        .filter(
            Task.owner_user_id == owner_uid,
            Task.deleted_at.is_(None),
            TaskStatusHistory.to_status == TaskStatus.done,
            TaskStatusHistory.at >= start,
            TaskStatusHistory.at < end,
        )
        .order_by(Task.completed_at.desc().nullslast(), Task.id.desc())
        .distinct()
        .all()
    )


def _in_progress_for_owner(session: Session, *, owner_uid: str) -> list[Task]:
    return (
        session.query(Task)
        .filter(
            Task.owner_user_id == owner_uid,
            Task.status == TaskStatus.in_progress,
            Task.deleted_at.is_(None),
        )
        .order_by(Task.due_date.asc().nullslast(), Task.id.asc())
        .all()
    )


def _todo_for_owner(session: Session, *, owner_uid: str) -> list[Task]:
    """Open backlog/todo tasks owned by the user, weighted to the
    current week. Order: due_date asc → priority asc → id asc."""
    return (
        session.query(Task)
        .filter(
            Task.owner_user_id == owner_uid,
            Task.status.in_((TaskStatus.backlog, TaskStatus.todo)),
            Task.deleted_at.is_(None),
        )
        .order_by(
            Task.due_date.asc().nullslast(),
            Task.is_current_week.desc(),
            Task.id.asc(),
        )
        .all()
    )


def _subscribed_open_for(session: Session, *, recipient_uid: str) -> list[Task]:
    return (
        session.query(Task)
        .join(TaskSubscription, TaskSubscription.task_id == Task.id)
        .filter(
            TaskSubscription.slack_user_id == recipient_uid,
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
            (Task.owner_user_id != recipient_uid) | (Task.owner_user_id.is_(None)),
        )
        .order_by(Task.due_date.is_(None), Task.due_date, Task.id)
        .distinct()
        .all()
    )


# --------------------------------------------------------------------------- #
# LLM narrative
# --------------------------------------------------------------------------- #


def _recent_history(
    session: Session, *, task_id: int, today: date
) -> list[TaskStatusHistory]:
    start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    cutoff = start - timedelta(days=3)
    return (
        session.query(TaskStatusHistory)
        .filter(
            TaskStatusHistory.task_id == task_id,
            TaskStatusHistory.at >= cutoff,
        )
        .order_by(TaskStatusHistory.at.asc())
        .all()
    )


def _format_history_block(history: list[TaskStatusHistory]) -> str:
    if not history:
        return "(история пуста)"
    lines = []
    for h in history[-6:]:
        from_s = h.from_status.value if h.from_status else "·"
        when = h.at.isoformat(timespec="minutes") if h.at else "?"
        actor = h.changed_by_slack_user_id or "—"
        reason = (h.reason or "").strip()
        line = f"{when} {from_s} → {h.to_status.value} (by {actor})"
        if reason:
            line += f" — {reason[:120]}"
        lines.append(line)
    return "\n".join(lines)


def compose_status_narrative(
    *, llm: Any, session: Session, task: Task, today: date
) -> str:
    """Per-task LLM narrative. Returns a 1-line plain-text summary
    suitable for inclusion in a status DM. Fails open: on any LLM
    error or empty response, falls back to a deterministic
    description so the digest never blocks on infra problems."""
    if llm is None:
        return _fallback_narrative(task)
    history = _recent_history(session, task_id=task.id, today=today)
    user_prompt = (
        f"Заголовок: {task.title or ''}\n"
        f"Описание: {(task.description or '').strip()[:600]}\n"
        f"Статус сейчас: {task.status.value}\n"
        f"Приоритет: {task.priority.value}\n"
        f"Срок: {task.due_date.isoformat() if task.due_date else '—'}\n"
        f"Транзишены за последние 3 дня:\n{_format_history_block(history)}\n"
    )
    try:
        text = llm.complete_text(  # type: ignore[attr-defined]
            system_prompt=_NARRATIVE_SYSTEM,
            user_prompt=user_prompt,
            temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "evening_status_llm_failed",
            task_id=task.id,
            error=str(e),
        )
        return _fallback_narrative(task)
    if not text:
        return _fallback_narrative(task)
    return _truncate_one_line(text, limit=240)


def _fallback_narrative(task: Task) -> str:
    """Deterministic 1-liner used when the LLM is unavailable.
    Keeps the digest informative even with no API key."""
    parts: list[str] = []
    if task.due_date:
        parts.append(f"срок {task.due_date.isoformat()}")
    if task.priority:
        parts.append(f"приоритет {task.priority.value}")
    parts.append(f"статус {task.status.value}")
    return ", ".join(parts) + "."


def _truncate_one_line(text: str, *, limit: int) -> str:
    cleaned = " ".join(text.split())  # collapse whitespace, kill newlines
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rstrip()
    return cut + "…"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_task_line(
    *,
    session: Session | None,
    task: Task,
    narrative: str,
    show_owner: bool,
) -> str:
    """Render one task line as Telegram HTML.

    Format:
        <bullet> <a href="permalink"><b>title</b></a>
            <narrative>
            (👤 owner-link · 📅 due)  ← only when show_owner=True

    The optional owner line is suppressed when the report is
    being rendered for the owner themselves (no need to tell
    Andrey his task is owned by Andrey).
    """
    if task.status == TaskStatus.done:
        bullet = "✅"
    else:
        bullet = PRIORITY_EMOJI.get(task.priority.value, "🟡")
    safe_title = _escape_html(task.title or "")
    if task.source_permalink:
        title_html = (
            f'<a href="{_escape_html(task.source_permalink)}">'
            f"<b>{safe_title}</b></a>"
        )
    else:
        title_html = f"<b>{safe_title}</b>"
    lines = [f"{bullet} {title_html}"]
    if narrative:
        lines.append(f"   <i>{_escape_html(narrative)}</i>")
    if show_owner:
        meta: list[str] = []
        tg_id, tg_handle, real_name = _resolve_owner_link_target(
            session, task.owner_user_id, task.owner_display_name
        )
        owner_label = _resolve_owner_display(task, real_name=real_name)
        eff_handle = tg_handle or _handle_from_display(task.owner_display_name)
        if owner_label:
            meta.append(
                "👤 "
                + _owner_html_link(
                    task.owner_user_id,
                    owner_label,
                    tg_user_id=tg_id,
                    tg_handle=eff_handle,
                )
            )
        if task.due_date:
            meta.append(f"📅 {task.due_date.isoformat()}")
        if meta:
            lines.append("   " + " · ".join(meta))
    return "\n".join(lines)


def _build_groups(
    *,
    session: Session,
    llm: Any,
    user_id: str,
    today: date,
    is_admin_view: bool,
) -> tuple[list[_TaskGroup], int]:
    """Returns the rendered groups + total tasks described (so the
    caller can update its counter)."""
    # `is_admin_view`-style: show owner deeplinks on every line so
    # the admin can see at a glance whose task it is.
    if is_admin_view:
        # Admin sees EVERY active user's tasks, grouped by status.
        done = _done_today_all(session, today=today)
        in_progress = _in_progress_all(session)
        todo = _todo_all(session)
        subs: list[Task] = []
    else:
        done = _done_today_for_owner(session, owner_uid=user_id, today=today)
        in_progress = _in_progress_for_owner(session, owner_uid=user_id)
        todo = _todo_for_owner(session, owner_uid=user_id)
        subs = _subscribed_open_for(session, recipient_uid=user_id)

    groups: list[_TaskGroup] = []
    described = 0

    def _section(emoji_title: str, tasks: list[Task], show_owner: bool) -> None:
        nonlocal described
        if not tasks:
            return
        g = _TaskGroup(title=f"{emoji_title} ({len(tasks)})")
        for t in tasks:
            narrative = compose_status_narrative(
                llm=llm, session=session, task=t, today=today
            )
            described += 1
            g.lines.append(
                _render_task_line(
                    session=session,
                    task=t,
                    narrative=narrative,
                    show_owner=show_owner,
                )
            )
        groups.append(g)

    _section("✅ Сделано сегодня", done, show_owner=is_admin_view)
    _section("🚀 В процессе", in_progress, show_owner=is_admin_view)
    _section("📋 Todo", todo, show_owner=is_admin_view)
    if not is_admin_view:
        _section("👀 Подписки", subs, show_owner=True)
    return groups, described


def _done_today_all(session: Session, *, today: date) -> list[Task]:
    """Admin-view selector — every Telegram-owned task closed today."""
    start, end = _day_bounds(today)
    return (
        session.query(Task)
        .join(TaskStatusHistory, TaskStatusHistory.task_id == Task.id)
        .filter(
            Task.deleted_at.is_(None),
            TaskStatusHistory.to_status == TaskStatus.done,
            TaskStatusHistory.at >= start,
            TaskStatusHistory.at < end,
        )
        .order_by(Task.completed_at.desc().nullslast(), Task.id.desc())
        .distinct()
        .all()
    )


def _in_progress_all(session: Session) -> list[Task]:
    return (
        session.query(Task)
        .filter(
            Task.status == TaskStatus.in_progress,
            Task.deleted_at.is_(None),
        )
        .order_by(Task.owner_user_id, Task.due_date.asc().nullslast(), Task.id.asc())
        .all()
    )


def _todo_all(session: Session) -> list[Task]:
    return (
        session.query(Task)
        .filter(
            Task.status.in_((TaskStatus.backlog, TaskStatus.todo)),
            Task.deleted_at.is_(None),
            Task.is_current_week.is_(True),
        )
        .order_by(Task.owner_user_id, Task.due_date.asc().nullslast(), Task.id.asc())
        .all()
    )


# --------------------------------------------------------------------------- #
# Message split
# --------------------------------------------------------------------------- #


def _split_groups_into_messages(
    *,
    header: str,
    groups: list[_TaskGroup],
    cap: int = _SPLIT_TARGET,
) -> list[str]:
    """Pack groups into ≤`cap`-char messages, splitting at line
    boundaries. Each message starts with the header (smaller
    sub-header on continuation messages so the operator knows it's
    the same report). Returns at least one message even when
    `groups` is empty (the «nothing today» case is handled by the
    caller and never reaches here)."""
    messages: list[str] = []
    current = header
    for g in groups:
        section_header = f"\n\n<b>{g.title}</b>"
        if len(current) + len(section_header) > cap and current.strip():
            messages.append(current)
            current = "(продолжение)"
        current += section_header
        for line in g.lines:
            chunk = "\n" + line
            if len(current) + len(chunk) > cap and current.strip():
                messages.append(current)
                current = "(продолжение)\n" + line
            else:
                current += chunk
    if current.strip():
        messages.append(current)
    return messages


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #


def send_evening_status_report(
    session: Session,
    *,
    sender: TelegramSender,
    llm: Any = None,
    today: date | None = None,
    include_admin_overview: bool = True,
) -> EveningStatusReport:
    """Send the per-user evening status DM to every Telegram owner
    + the consolidated admin overview to every admin uid.

    Each DM is one (or several) HTML messages with `<a href>`
    hyperlinks on titles + owner deeplinks. Idempotent per
    (user, date)."""
    today = today or date.today()
    report = EveningStatusReport()

    # Per-owner reports — owners get their own tasks + subscriptions.
    owners = _telegram_owner_ids(session)
    # Subscribers may not own anything but should still get a recap.
    sub_only = _telegram_subscriber_ids(session)
    recipients = sorted(set(owners) | set(sub_only))

    for uid in recipients:
        if _already_sent(session, user_id=uid, day=today):
            report.skipped_idempotent += 1
            continue
        groups, described = _build_groups(
            session=session,
            llm=llm,
            user_id=uid,
            today=today,
            is_admin_view=False,
        )
        if not groups:
            report.skipped_no_tasks += 1
            _mark_sent(
                session,
                user_id=uid,
                day=today,
                payload={"groups": 0, "tasks": 0},
            )
            continue
        header = f"📊 <b>Статус задач — {today.isoformat()}</b>"
        msgs = _split_groups_into_messages(header=header, groups=groups)
        sent = 0
        for body in msgs:
            try:
                sender.send_message(chat_id=int(uid), text=body)
            except Exception as e:  # noqa: BLE001
                report.failures += 1
                log.warning(
                    "evening_status_send_failed", uid=uid, error=str(e)
                )
                break
            sent += 1
        if sent == 0:
            continue
        _mark_sent(
            session,
            user_id=uid,
            day=today,
            payload={"groups": len(groups), "tasks": described, "messages": sent},
        )
        report.recipients += 1
        report.messages_sent += sent
        report.tasks_described += described

    # Admin overview — full picture across everyone, regardless of
    # whether the admin owns anything.
    if include_admin_overview:
        for admin_uid in sorted(admin_user_ids()):
            if not _is_telegram_user_id(admin_uid):
                continue
            action_id = f"admin:{today.isoformat()}"
            already = (
                session.query(AuditLog)
                .filter(
                    AuditLog.category == _CATEGORY,
                    AuditLog.action == "admin",
                    AuditLog.actor == admin_uid,
                    AuditLog.entity_id == action_id,
                )
                .first()
            )
            if already is not None:
                report.skipped_idempotent += 1
                continue
            groups, described = _build_groups(
                session=session,
                llm=llm,
                user_id=admin_uid,
                today=today,
                is_admin_view=True,
            )
            if not groups:
                report.skipped_no_tasks += 1
                continue
            header = (
                f"📊 <b>Сводка по команде — {today.isoformat()}</b>"
            )
            msgs = _split_groups_into_messages(header=header, groups=groups)
            sent = 0
            for body in msgs:
                try:
                    sender.send_message(chat_id=int(admin_uid), text=body)
                except Exception as e:  # noqa: BLE001
                    report.failures += 1
                    log.warning(
                        "evening_status_admin_send_failed",
                        uid=admin_uid,
                        error=str(e),
                    )
                    break
                sent += 1
            if sent == 0:
                continue
            session.add(
                AuditLog(
                    category=_CATEGORY,
                    action="admin",
                    entity_type=_CATEGORY,
                    entity_id=action_id,
                    actor=admin_uid,
                    payload={
                        "groups": len(groups),
                        "tasks": described,
                        "messages": sent,
                    },
                )
            )
            session.flush()
            report.recipients += 1
            report.messages_sent += sent
            report.tasks_described += described

    return report


def _telegram_subscriber_ids(session: Session) -> list[str]:
    """Distinct Telegram user ids that subscribe to at least one
    open, non-deleted task."""
    rows = (
        session.query(TaskSubscription.slack_user_id)
        .join(Task, Task.id == TaskSubscription.task_id)
        .filter(
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
        )
        .distinct()
        .all()
    )
    return sorted({r[0] for r in rows if _is_telegram_user_id(r[0])})


__all__ = [
    "EveningStatusReport",
    "compose_status_narrative",
    "send_evening_status_report",
]
