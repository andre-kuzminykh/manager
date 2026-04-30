"""FR-CR-05-41 — morning task cards (one card per due-today task).

Covers:
- Selection: owner gets cards for tasks due today + currently
  in_progress + this-week-tagged null-due todo/backlog;
  subscriber gets follow-along cards in a separate section.
- Ordering: priority desc → due_time asc → start_time asc → id.
- Rendering: every card carries the same keyboard the live
  cards use (`task_card_keyboard`); intro DM lists the day's
  load.
- Idempotency: re-running on the same date is a no-op.
- Edge cases: no tasks → skipped + audit row recorded;
  subscriptions only → no separator if there are no owned
  cards above.
"""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app.models import (
    Task,
    TaskStatus,
    TaskSubscription,
)
from app.models.task import TaskPriority
from app.telegram_bot.morning_cards import (
    MorningCardsReport,
    send_morning_task_cards,
)


class _RecordingTGSender:
    def __init__(self):
        self.enabled = True
        self.sent: list[dict] = []

    def send_message(self, *, chat_id, text, reply_markup=None, **kw):
        self.sent.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"message_id": len(self.sent)}


def _mk_task(s, **kw) -> Task:
    base = dict(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="111",
        priority=TaskPriority.medium,
        is_current_week=True,
    )
    base.update(kw)
    t = Task(**base)
    s.add(t)
    s.flush()
    # FR-CR-05-66 — auto-seed has_started_bot for the owner so
    # the morning-cards recipient filter doesn't drop them.
    if t.owner_user_id and str(t.owner_user_id).lstrip("-").isdigit():
        _mark_started_bot(s, int(t.owner_user_id))
    return t


def _mark_started_bot(s, user_id: int) -> None:
    """Seed the `telegram_chat_members` row that signals
    `has_started_bot=True` so the FR-CR-05-66 recipient
    filter lets this uid through."""
    from app.models import TelegramChatMember

    existing = s.query(TelegramChatMember).filter_by(
        chat_id=user_id, user_id=user_id
    ).first()
    if existing is None:
        s.add(
            TelegramChatMember(
                chat_id=user_id,
                user_id=user_id,
                has_started_bot=True,
            )
        )
        s.flush()
    elif not existing.has_started_bot:
        existing.has_started_bot = True
        s.flush()


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_morning_cards_picks_due_today_in_progress_and_current_week_no_due(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        t_due = _mk_task(s, title="due today", due_date=today)
        t_ip = _mk_task(s, title="in flight", status=TaskStatus.in_progress)
        t_cw = _mk_task(
            s, title="this week todo", due_date=None, is_current_week=True
        )
        # Excluded: due far away + not current week.
        _mk_task(
            s,
            title="far",
            due_date=today + timedelta(days=10),
            is_current_week=False,
        )
        # Excluded: done.
        _mk_task(
            s,
            title="closed",
            status=TaskStatus.done,
            is_current_week=True,
        )
        s.commit()

        sender = _RecordingTGSender()
        report = send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    assert report.recipients == 1
    # 1 intro + 3 cards = 4 messages.
    assert len(sender.sent) == 4
    titles = " ".join(m["text"] for m in sender.sent[1:])
    assert "due today" in titles
    assert "in flight" in titles
    assert "this week todo" in titles
    assert "far" not in titles and "closed" not in titles


def test_morning_cards_orders_by_priority_then_due_time(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(s, title="med-late", due_date=today, due_time=time(17, 0), priority=TaskPriority.medium)
        _mk_task(s, title="high-early", due_date=today, due_time=time(9, 0), priority=TaskPriority.high)
        _mk_task(s, title="urgent", due_date=today, priority=TaskPriority.urgent)
        _mk_task(s, title="low", due_date=today, priority=TaskPriority.low)
        s.commit()

        sender = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    # Skip intro (index 0) — cards order on subsequent messages.
    titles_in_order = [m["text"].split("\n")[0] for m in sender.sent[1:]]
    # The bullet emoji prefixes the title so we just check the
    # ordering by `in`.
    indexed = [
        (i, t) for i, t in enumerate(titles_in_order)
        if any(k in t for k in ("urgent", "high-early", "med-late", "low"))
    ]
    keys = [t for _, t in indexed]
    # Expected: urgent → high-early → med-late → low
    assert any("urgent" in s for s in keys[:1])
    assert any("high-early" in s for s in keys[1:2])
    assert any("med-late" in s for s in keys[2:3])
    assert any("low" in s for s in keys[3:4])


def test_morning_cards_subscriber_gets_separator_only_when_owned_above(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        # User 333 owns one task + subscribes to one of 222's.
        t_owned = _mk_task(s, title="my own", owner_user_id="333", due_date=today)
        t_other = _mk_task(s, title="watched", owner_user_id="222", due_date=today)
        s.add(TaskSubscription(task_id=t_other.id, slack_user_id="333"))
        s.commit()

        sender = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender, today=today)
        s.commit()

    # User 333 should see: intro + own card + Подписки separator
    # + subscribed card.
    sent_to_333 = [m for m in sender.sent if m["chat_id"] == 333]
    sep_present = any("Подписки" in m["text"] for m in sent_to_333)
    assert sep_present
    # User 222 (just an owner of `t_other`, no subs) sees: intro
    # + 1 card; no separator.
    sent_to_222 = [m for m in sender.sent if m["chat_id"] == 222]
    sep_for_222 = [m for m in sent_to_222 if "Подписки" in m["text"]]
    assert sep_for_222 == []


# --------------------------------------------------------------------------- #
# Card formatting / keyboard
# --------------------------------------------------------------------------- #


def test_morning_cards_attaches_full_task_keyboard(
    patched_session_scope, SessionFactory
):
    """Each task card carries an inline keyboard with at least
    [Edit / Mark done / Subscribe] buttons. Owner of an
    in_progress task sees the Mark done button."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(
            s,
            title="active task",
            status=TaskStatus.in_progress,
            owner_user_id="111",
        )
        s.commit()
        sender = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    card = sender.sent[1]
    kb = card["reply_markup"]
    assert kb is not None
    flat = [
        btn["text"]
        for row in kb["inline_keyboard"]
        for btn in row
    ]
    assert any("Mark done" in t for t in flat)
    assert any("Edit" in t for t in flat)


def test_morning_cards_intro_lists_day_count(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        for i in range(3):
            _mk_task(s, title=f"task-{i}", due_date=today)
        s.commit()
        sender = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    intro = sender.sent[0]
    assert "Доброе утро" in intro["text"]
    assert "3" in intro["text"]


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_morning_cards_idempotent_per_user_per_day(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(s, title="x", due_date=today)
        s.commit()
        sender = _RecordingTGSender()
        first = send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    assert first.recipients == 1
    sent_first = list(sender.sent)

    with SessionFactory() as s:
        second = send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    assert second.recipients == 0
    assert second.skipped_idempotent == 1
    assert sender.sent == sent_first


def test_morning_cards_owner_with_no_due_today_marked_skipped(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        # Owner has only a far-away task, no current-week flag.
        _mk_task(
            s,
            title="next month",
            due_date=today + timedelta(days=30),
            is_current_week=False,
        )
        s.commit()
        sender = _RecordingTGSender()
        report = send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    assert report.recipients == 0
    assert report.skipped_no_tasks == 1
    assert sender.sent == []


# --------------------------------------------------------------------------- #
# FR-CR-05-49 — overdue badge
# --------------------------------------------------------------------------- #


def test_morning_cards_picks_up_overdue_tasks(
    patched_session_scope, SessionFactory
):
    """A task with `due_date < today` and an open status used to
    fall through the morning-cards selector. FR-CR-05-49 adds it
    explicitly so overdue work doesn't go silently missing."""
    today = date(2026, 4, 29)
    yesterday = today - timedelta(days=1)
    with SessionFactory() as s:
        _mk_task(s, title="overdue", due_date=yesterday, is_current_week=False)
        s.commit()
        sender = _RecordingTGSender()
        report = send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    assert report.recipients == 1
    # 1 intro + 1 card.
    assert len(sender.sent) == 2
    # Card body has the alarm header.
    assert "ПРОСРОЧЕНО" in sender.sent[1]["text"]
    assert "🚨" in sender.sent[1]["text"]
    # Intro mentions the overdue count.
    assert "🚨 Просрочено: 1" in sender.sent[0]["text"]


def test_morning_cards_overdue_sorted_first(
    patched_session_scope, SessionFactory
):
    """Overdue tasks come first, ahead of even high-priority
    tasks that aren't overdue. Within overdue, the usual
    priority/time tiebreaker still applies."""
    today = date(2026, 4, 29)
    yesterday = today - timedelta(days=1)
    with SessionFactory() as s:
        _mk_task(s, title="urgent-today", due_date=today, priority=TaskPriority.urgent)
        _mk_task(
            s, title="overdue-low", due_date=yesterday, priority=TaskPriority.low
        )
        s.commit()
        sender = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    # Skip intro at index 0; cards start at 1.
    first_card = sender.sent[1]["text"]
    second_card = sender.sent[2]["text"]
    assert "overdue-low" in first_card
    assert "urgent-today" in second_card


def test_morning_cards_no_alarm_for_done_overdue(
    patched_session_scope, SessionFactory
):
    """A task that's already `done` is never overdue regardless
    of the past `due_date`. The selector excludes done tasks
    anyway (status filter), so this is a defensive check."""
    today = date(2026, 4, 29)
    yesterday = today - timedelta(days=1)
    with SessionFactory() as s:
        _mk_task(
            s, title="done-yesterday", due_date=yesterday,
            status=TaskStatus.done,
        )
        # Add a non-done task too so the user has SOMETHING to see.
        _mk_task(s, title="real task", due_date=today)
        s.commit()
        sender = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    bodies = " ".join(m["text"] for m in sender.sent)
    assert "done-yesterday" not in bodies
    assert "ПРОСРОЧЕНО" not in bodies
    assert "real task" in bodies


# --------------------------------------------------------------------------- #
# FR-CR-05-66 — recipients filtered to has_started_bot=true
# --------------------------------------------------------------------------- #


def test_morning_cards_skips_recipients_without_started_bot(
    patched_session_scope, SessionFactory
):
    """Operator: production logs were full of «Bad Request: chat
    not found» because the morning digest tried to DM every
    `tasks.owner_user_id` — including team-sheet entries that
    never /started the bot. Telegram bans bot-initiated
    conversations. The recipient set now filters by
    `telegram_chat_members.has_started_bot=true`."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        # User 555 owns a task but never /started the bot.
        # _mk_task auto-seeds 555 as started_bot — undo it.
        _mk_task(s, title="x", owner_user_id="555", due_date=today)
        from app.models import TelegramChatMember
        member = s.query(TelegramChatMember).filter_by(user_id=555).one()
        member.has_started_bot = False
        s.flush()
        s.commit()

        sender = _RecordingTGSender()
        report = send_morning_task_cards(s, sender=sender, today=today)
        s.commit()

    # 555 was filtered out — no DMs sent at all.
    assert report.recipients == 0
    assert sender.sent == []
