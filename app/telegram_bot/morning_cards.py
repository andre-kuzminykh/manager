"""FR-CR-05-41 — Morning task cards (one card per task).

The legacy `send_morning_plan` posted a single bullet-list DM
that doesn't surface the per-task action buttons. The morning
flow operators actually want is one INTERACTIVE CARD per task,
identical to the cards posted on initial intent confirmation:
title hyperlinks to the source message, owner deeplink, full
[Start / Edit / Mark done / Delete / Subscribe] keyboard.

Selector, ordering, and idempotency mirror the FR-CR-05-40
evening report so a re-run is a no-op:

  - One DM with a single «☀ Доброе утро — задачи на день: N»
    intro, listing tasks tersely.
  - Then one task card per open task due today, ordered:
    priority desc → due_time asc → start_time asc → id asc.

Tasks that are owned by the recipient land first; if they're
also subscribed to others' tasks due today, those follow with
a thin separator. Same `task_card_keyboard` permissions as
the live cards: Start only for owner, Edit/Done/Delete for
owner+admin, Subscribe toggle for non-owners.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import (
    AuditLog,
    Task,
    TaskStatus,
    TaskSubscription,
)
from app.telegram_bot.handlers import admin_user_ids
from app.telegram_bot.cards import _viewer_is_owner
from app.telegram_bot.keyboards import task_card_keyboard
from app.telegram_bot.notifications import (
    _is_telegram_user_id,
    _telegram_owner_ids,
)
from app.telegram_bot.sender import (
    PRIORITY_EMOJI,
    TelegramSender,
    build_task_card_text,
)

log = get_logger(__name__)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)
_CATEGORY = "telegram_morning_cards"


# Priority sort order — urgent tasks first, low last.
_PRIORITY_RANK = {
    "urgent": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


# --------------------------------------------------------------------------- #
# Audit / idempotency
# --------------------------------------------------------------------------- #


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
            action="morning",
            entity_type=_CATEGORY,
            entity_id=day.isoformat(),
            actor=user_id,
            payload=payload,
        )
    )
    session.flush()


def _last_morning_card_messages(
    session: Session, *, user_id: str, today: date
) -> tuple[date | None, list[dict]]:
    """FR-CR-05-84 — find the most recent prior-day morning audit
    row for `user_id` and return its `card_messages` payload list.

    Used to delete yesterday's cards before posting today's so the
    operator's DM only ever shows the current-day cards. Returns
    `(prior_day, list_of_{chat_id, message_id})` or
    `(None, [])` when no prior row exists / payload is empty.
    """
    rows = (
        session.query(AuditLog)
        .filter(
            AuditLog.category == _CATEGORY,
            AuditLog.actor == user_id,
            AuditLog.entity_id != today.isoformat(),
        )
        .order_by(AuditLog.entity_id.desc())
        .limit(1)
        .all()
    )
    if not rows:
        return None, []
    payload = rows[0].payload or {}
    cards = payload.get("card_messages") or []
    if not isinstance(cards, list):
        return None, []
    try:
        prior_day = date.fromisoformat(rows[0].entity_id)
    except (TypeError, ValueError):
        prior_day = None
    return prior_day, cards


def _delete_prior_morning_cards(
    *,
    sender: TelegramSender,
    session: Session,
    user_id: str,
    today: date,
) -> int:
    """FR-CR-05-84 — delete the recipient's prior-day morning
    cards before we post today's. Best-effort: a delete failure
    (message already gone, 7-day deletion window expired,
    permissions revoked) is logged and skipped, never aborts
    today's post."""
    prior_day, cards = _last_morning_card_messages(
        session, user_id=user_id, today=today
    )
    if not cards:
        return 0
    deleted = 0
    for c in cards:
        try:
            cid = int(c.get("chat_id"))
            mid = int(c.get("message_id"))
        except (TypeError, ValueError):
            continue
        try:
            sender.delete_message(chat_id=cid, message_id=mid)
            deleted += 1
        except Exception as e:  # noqa: BLE001
            log.info(
                "morning_cards_prior_delete_failed",
                uid=user_id,
                prior_day=prior_day.isoformat() if prior_day else None,
                chat_id=cid,
                message_id=mid,
                error=str(e),
            )
    return deleted


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #


def _owned_due_today(
    session: Session, *, owner_uid: str, today: date
) -> list[Task]:
    """Owner's open tasks that need to land on today's plan.

    A task is «for today» if EITHER:
      - `due_date == today`, OR
      - `due_date < today` — overdue, badge 🚨 (FR-CR-05-49),
      - it's currently `in_progress` (must finish or move it),
        OR
      - it's flagged `is_current_week` AND status in (todo,
        backlog) AND due_date is None (no firm due date but
        slated for this week)."""
    return (
        session.query(Task)
        .filter(
            Task.owner_user_id == owner_uid,
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
            or_(
                Task.due_date == today,
                Task.due_date < today,
                Task.status == TaskStatus.in_progress,
                (Task.is_current_week.is_(True))
                & Task.due_date.is_(None)
                & Task.status.in_((TaskStatus.todo, TaskStatus.backlog)),
            ),
        )
        .all()
    )


def _is_overdue(task: Task, *, today: date) -> bool:
    """FR-CR-05-49 — overdue = due_date strictly before today AND
    not already closed. Helper mirrors the one in
    `evening_status` so the badge logic stays in lockstep."""
    if task.status == TaskStatus.done:
        return False
    return bool(task.due_date and task.due_date < today)


def _subscribed_due_today(
    session: Session, *, recipient_uid: str, today: date
) -> list[Task]:
    """Open tasks the user follows that are due today (or
    already in flight). `IN (subquery)` instead of `JOIN ...
    DISTINCT` to avoid Postgres' «no equality operator for json»
    on `tasks.extra` (DISTINCT on the full row needs to compare
    every column)."""
    return (
        session.query(Task)
        .filter(
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
            (Task.owner_user_id != recipient_uid) | (Task.owner_user_id.is_(None)),
            or_(
                Task.due_date == today,
                Task.status == TaskStatus.in_progress,
            ),
            Task.id.in_(
                session.query(TaskSubscription.task_id).filter(
                    TaskSubscription.slack_user_id == recipient_uid,
                )
            ),
        )
        .all()
    )


def _sort_tasks_for_morning(
    tasks: list[Task], *, today: date | None = None
) -> list[Task]:
    """Order: overdue first (FR-CR-05-49), then priority desc →
    due_time asc → start_time asc → id asc."""

    def key(t: Task):
        # Overdue rank: 0 if overdue today, 1 otherwise — strictly
        # promotes overdue tasks above same-priority non-overdue
        # ones.
        overdue_rank = 0 if (today is not None and _is_overdue(t, today=today)) else 1
        pri_rank = _PRIORITY_RANK.get(t.priority.value, 99)
        # `time` instances sort fine, but None has to go last.
        due_t = t.due_time or time(23, 59, 59)
        start_t = t.start_time or time(23, 59, 59)
        return (overdue_rank, pri_rank, due_t, start_t, t.id)

    return sorted(tasks, key=key)


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _build_intro_text(*, today: date, tasks: list[Task]) -> str:
    if not tasks:
        return f"☀ <b>Good morning — {today.isoformat()}</b>\nNothing on the plate. Have a good day."
    overdue_count = sum(1 for t in tasks if _is_overdue(t, today=today))
    lines = [
        f"☀ <b>Good morning — tasks for {today.isoformat()}: {len(tasks)}</b>",
    ]
    if overdue_count:
        lines.append(f"🚨 Overdue: {overdue_count}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass
class MorningCardsReport:
    recipients: int = 0
    cards_sent: int = 0
    # FR-CR-05-84 — counter for prior-day cards deleted at the
    # start of today's run (so the cron log shows the cleanup
    # happening, separately from today's posting).
    prior_cards_deleted: int = 0
    # FR-CR-05-91 — admin morning diff DMs sent (one per admin
    # who had any per-person delta vs yesterday's evening plan).
    admin_diff_sent: int = 0
    skipped_idempotent: int = 0
    skipped_no_tasks: int = 0
    failures: int = 0


def _render_admin_morning_diff(
    session: Session,
    *,
    today: date,
    admin_uid: str,
) -> str | None:
    """FR-CR-05-91 — operator: «перед этим [утренними карточками]
    изменения во вчерашнем плане по людям новую сделай (если
    есть изменения)».

    Diffs:
      - YESTERDAY: read `audit_logs.payload.per_person_plan_task_ids`
        from the most recent admin row of the evening status
        (one entry per owner with the task IDs that were on the
        evening plan).
      - TODAY: re-run the same per-person tomorrow selector
        (now `today` since we've crossed midnight).

    Per owner, classify each task as:
      - DONE: yesterday-only AND now status=done
      - REMOVED: yesterday-only AND not done (deleted /
        deferred / re-assigned)
      - ADDED: today-only (didn't exist on yesterday's plan)

    Returns the rendered HTML message, or `None` when there's
    nothing to report (no audit row, or no changes anywhere).
    """
    from app.telegram_bot.evening_status import _CATEGORY as _EVENING_CATEGORY
    from app.telegram_bot.evening_status import _owned_for_tomorrow

    # Most-recent admin row before today.
    prior = (
        session.query(AuditLog)
        .filter(
            AuditLog.category == _EVENING_CATEGORY,
            AuditLog.action == "admin",
            AuditLog.actor == admin_uid,
            AuditLog.entity_id < f"admin:{today.isoformat()}",
        )
        .order_by(AuditLog.entity_id.desc())
        .limit(1)
        .one_or_none()
    )
    if prior is None:
        return None
    yesterday_payload = prior.payload or {}
    yesterday_ids: dict[str, list[int]] = (
        yesterday_payload.get("per_person_plan_task_ids") or {}
    )
    if not yesterday_ids:
        return None

    # Today's per-person plan — same selector, anchored to today.
    today_ids: dict[str, set[int]] = {}
    all_owners = set(yesterday_ids.keys())
    # Also include owners who don't show up in yesterday but do today
    # (i.e., NEW additions). For that we need to query everyone open
    # for today.
    rows = (
        session.query(Task)
        .filter(
            Task.deleted_at.is_(None),
            Task.owner_user_id.isnot(None),
        )
        .all()
    )
    owners_today = {t.owner_user_id for t in rows if t.owner_user_id}
    all_owners |= owners_today
    for owner in all_owners:
        if owner == "_unassigned_":
            continue
        tasks = _owned_for_tomorrow(session, owner_uid=owner, tomorrow=today)
        today_ids[owner] = {t.id for t in tasks}

    # Resolve display names once.
    from app.telegram_bot.sender import (
        _escape_html as _esc,
        _resolve_owner_link_target,
    )

    def _label(uid: str) -> str:
        if uid == "_unassigned_":
            return "Не назначено"
        _tg_id, _tg_handle, real_name = _resolve_owner_link_target(
            session, uid, None
        )
        return real_name or uid

    sections: list[str] = []
    any_change = False
    for owner_uid in sorted(all_owners, key=lambda u: _label(u).lower()):
        y_ids = set(yesterday_ids.get(owner_uid) or [])
        t_ids = today_ids.get(owner_uid, set())
        added = sorted(t_ids - y_ids)
        removed = sorted(y_ids - t_ids)
        if not added and not removed:
            continue
        any_change = True
        section_lines = [f"\n👤 <b>{_esc(_label(owner_uid))}</b>"]
        # Resolve titles for the changed task ids.
        if added:
            for tid in added:
                t = session.get(Task, tid)
                if t is None or t.deleted_at is not None:
                    continue
                section_lines.append(
                    f"  ➕ <b>{_esc(t.title or '')}</b>"
                )
        if removed:
            for tid in removed:
                t = session.get(Task, tid)
                if t is None:
                    section_lines.append(f"  ➖ <i>task #{tid} removed</i>")
                    continue
                # Classify: done / deleted / deferred / other.
                if t.status == TaskStatus.done:
                    section_lines.append(
                        f"  ✅ <b>{_esc(t.title or '')}</b> — done"
                    )
                elif t.deleted_at is not None:
                    section_lines.append(
                        f"  🗑 <s>{_esc(t.title or '')}</s> — deleted"
                    )
                elif t.due_date and t.due_date > today:
                    section_lines.append(
                        f"  📅 <b>{_esc(t.title or '')}</b> — "
                        f"deferred to {t.due_date.isoformat()}"
                    )
                else:
                    section_lines.append(
                        f"  ➖ <b>{_esc(t.title or '')}</b> — removed"
                    )
        sections.append("\n".join(section_lines))

    if not any_change:
        return None

    header = f"📊 <b>Plan changes since yesterday — {today.isoformat()}</b>"
    return header + "\n" + "\n".join(sections)


def send_morning_task_cards(
    session: Session,
    *,
    sender: TelegramSender,
    today: date | None = None,
) -> MorningCardsReport:
    """Send one interactive task card per due-today task to every
    Telegram owner / subscriber. Idempotent per (user, date)."""
    today = today or date.today()
    report = MorningCardsReport()

    owners = _telegram_owner_ids(session)
    sub_only = _telegram_subscriber_ids(session)
    admin_uids = sorted(admin_user_ids())
    # FR-CR-05-66 — drop recipients who never /started the bot.
    # Telegram bans bot-initiated conversations, so DMs to them
    # 400 with «chat not found» — clutters logs and counts as
    # spurious sends. Admin uids ALWAYS pass through (the
    # operator chose them deliberately, even if they happen
    # to lack the registry row).
    from app.services.telegram_members import users_who_started_bot

    started = users_who_started_bot(session) | set(admin_uids)
    recipients = sorted((set(owners) | set(sub_only)) & started)

    for uid in recipients:
        if _already_sent(session, user_id=uid, day=today):
            report.skipped_idempotent += 1
            continue
        owned = _sort_tasks_for_morning(
            _owned_due_today(session, owner_uid=uid, today=today),
            today=today,
        )
        subs = _sort_tasks_for_morning(
            _subscribed_due_today(session, recipient_uid=uid, today=today),
            today=today,
        )
        all_tasks = owned + subs
        if not all_tasks:
            report.skipped_no_tasks += 1
            _mark_sent(
                session, user_id=uid, day=today,
                payload={"cards": 0, "owned": 0, "subs": 0, "card_messages": []},
            )
            continue

        # FR-CR-05-84 — operator: «ты их как бы удаляй если они
        # ранее были и создавай заново с утра». Wipe yesterday's
        # cards first so the DM shows only today's set. Best-
        # effort: per-message failures don't abort today's post.
        deleted_prior = _delete_prior_morning_cards(
            sender=sender, session=session, user_id=uid, today=today,
        )
        report.prior_cards_deleted += deleted_prior

        # FR-CR-05-91 — admin recipients ALSO get a per-person
        # «what changed since yesterday's plan» diff DM BEFORE
        # today's intro + cards. Only sent when there are
        # actual changes, so a quiet day doesn't spam an empty
        # message. Best-effort.
        if uid in admin_uids:
            try:
                diff_text = _render_admin_morning_diff(
                    session, today=today, admin_uid=uid,
                )
            except Exception as e:  # noqa: BLE001
                diff_text = None
                log.warning(
                    "morning_admin_diff_render_failed",
                    uid=uid,
                    error=str(e),
                )
            if diff_text:
                try:
                    sender.send_message(chat_id=int(uid), text=diff_text)
                    report.admin_diff_sent += 1
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "morning_admin_diff_send_failed",
                        uid=uid,
                        error=str(e),
                    )

        # Track every (chat_id, message_id) we successfully post
        # today so tomorrow's run can delete them in turn.
        card_messages: list[dict[str, int]] = []

        # Intro DM listing the day's load at a glance. If THIS
        # send fails (e.g. «chat not found» — operator on the
        # team list but never /started the bot), we flip
        # `has_started_bot=False` for them and SKIP the rest of
        # the cards for this recipient. Otherwise we'd dump the
        # full N-card stream into a black hole and pollute the
        # logs (FR-CR-05-67).
        intro_resp = sender.send_message(
            chat_id=int(uid),
            text=_build_intro_text(today=today, tasks=all_tasks),
        )
        intro_mid = (intro_resp or {}).get("message_id")
        if not intro_mid:
            report.failures += 1
            log.warning(
                "morning_cards_intro_send_failed",
                uid=uid,
                hint="recipient probably hasn't /start-ed the bot",
            )
            try:
                _mark_started_bot_false(session, int(uid))
                session.flush()
            except Exception:  # noqa: BLE001
                pass
            continue
        card_messages.append({"chat_id": int(uid), "message_id": int(intro_mid)})

        cards = 0
        is_admin = uid in admin_uids
        for t in owned:
            mid = _post_one_card(
                sender=sender,
                session=session,
                chat_id=int(uid),
                task=t,
                is_owner=True,
                is_admin=is_admin,
                today=today,
            )
            if mid:
                cards += 1
                card_messages.append({"chat_id": int(uid), "message_id": int(mid)})
        # Tasks the user follows (separator first if there were
        # owned ones above).
        if subs and owned:
            try:
                sep_resp = sender.send_message(
                    chat_id=int(uid),
                    text="— — —\n👀 <b>Watching</b>",
                )
                sep_mid = (sep_resp or {}).get("message_id")
                if sep_mid:
                    card_messages.append(
                        {"chat_id": int(uid), "message_id": int(sep_mid)}
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "morning_cards_subs_separator_failed",
                    uid=uid,
                    error=str(e),
                )
        for t in subs:
            subscribed = True
            mid = _post_one_card(
                sender=sender,
                session=session,
                chat_id=int(uid),
                task=t,
                # FR-CR-05-113 — set-intersection check via
                # team_members so handle / real_name / uid
                # variants of the same person all match.
                is_owner=_viewer_is_owner(t, uid),
                is_admin=is_admin,
                subscribed=subscribed,
                today=today,
            )
            if mid:
                cards += 1
                card_messages.append({"chat_id": int(uid), "message_id": int(mid)})

        if cards == 0:
            report.failures += 1
            continue
        _mark_sent(
            session, user_id=uid, day=today,
            payload={
                "cards": cards,
                "owned": len(owned),
                "subs": len(subs),
                "card_messages": card_messages,
                "prior_deleted": deleted_prior,
            },
        )
        report.recipients += 1
        report.cards_sent += cards
    return report


def _post_one_card(
    *,
    sender: TelegramSender,
    session: Session,
    chat_id: int,
    task: Task,
    is_owner: bool,
    is_admin: bool,
    subscribed: bool = False,
    today: date | None = None,
) -> int | None:
    """Render + send one task card with the same keyboard the
    live cards use. Returns the posted Telegram `message_id` on
    success, or `None` on failure. Failures are logged but
    don't abort the rest of the digest.

    FR-CR-05-49 — overdue tasks (`due_date < today`) get a
    «🚨 OVERDUE · was due {date}» header so the operator
    sees the alarm before the rest of the card body.

    FR-CR-05-84 — return type changed from bool → message_id
    so the caller can store it in `audit_logs.payload.card_
    messages` and tomorrow's run can delete it.
    """
    header: str | None = None
    if today is not None and _is_overdue(task, today=today) and task.due_date:
        header = f"🚨 OVERDUE · was due {task.due_date.isoformat()}"
    text = build_task_card_text(task, session=session, header=header)
    keyboard = task_card_keyboard(
        task_id=task.id,
        status=task.status.value,
        is_owner=is_owner,
        is_admin=is_admin,
        subscribed=subscribed,
    )
    try:
        resp = sender.send_message(
            chat_id=chat_id, text=text, reply_markup=keyboard
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "morning_cards_card_send_failed",
            chat_id=chat_id,
            task_id=task.id,
            error=str(e),
        )
        return None
    # FR-CR-05-67 — `TelegramSender._post` swallows 4xx errors and
    # returns `{}` instead of raising. Detect failure by missing
    # `message_id` so the digest doesn't think it sent 41 cards
    # to a chat the bot can't actually DM («Bad Request: chat
    # not found»).
    mid = (resp or {}).get("message_id")
    if not mid:
        return None
    try:
        return int(mid)
    except (TypeError, ValueError):
        return None


def _mark_started_bot_false(session: Session, user_id: int) -> None:
    """FR-CR-05-67 — Telegram returned «chat not found» for
    this user. Whatever flag was set previously was wrong;
    flip every `telegram_chat_members` row for them to
    `has_started_bot=False` so the next pull's recipient
    filter (FR-CR-05-66) drops them silently."""
    from app.models import TelegramChatMember

    rows = (
        session.query(TelegramChatMember)
        .filter(TelegramChatMember.user_id == user_id)
        .all()
    )
    for r in rows:
        r.has_started_bot = False


def _telegram_subscriber_ids(session: Session) -> list[str]:
    """Distinct Telegram user ids that subscribe to at least one
    open, non-deleted task. Mirrors the helper in
    `evening_status` but lives here too so the morning module is
    importable on its own."""
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
    "MorningCardsReport",
    "send_morning_task_cards",
]
