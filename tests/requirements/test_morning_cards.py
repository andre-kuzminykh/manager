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

from datetime import date, datetime, time, timedelta, timezone

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
        self.deleted: list[dict] = []
        self.delete_should_raise: bool = False

    def send_message(self, *, chat_id, text, reply_markup=None, **kw):
        self.sent.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"message_id": len(self.sent)}

    def delete_message(self, *, chat_id, message_id):
        if self.delete_should_raise:
            raise RuntimeError("Bad Request: message to delete not found")
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})
        return {}


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

    # User 333 should see: intro + own card + Watching separator
    # + subscribed card.
    sent_to_333 = [m for m in sender.sent if m["chat_id"] == 333]
    sep_present = any("Watching" in m["text"] for m in sent_to_333)
    assert sep_present
    # User 222 (just an owner of `t_other`, no subs) sees: intro
    # + 1 card; no separator.
    sent_to_222 = [m for m in sender.sent if m["chat_id"] == 222]
    sep_for_222 = [m for m in sent_to_222 if "Watching" in m["text"]]
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
    assert "Good morning" in intro["text"]
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
    assert "OVERDUE" in sender.sent[1]["text"]
    assert "🚨" in sender.sent[1]["text"]
    # Intro mentions the overdue count.
    assert "🚨 Overdue: 1" in sender.sent[0]["text"]


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
    assert "OVERDUE" not in bodies
    assert "real task" in bodies


# --------------------------------------------------------------------------- #
# FR-CR-05-91 — admin morning diff vs yesterday's per-person plan
# --------------------------------------------------------------------------- #


def test_morning_admin_diff_renders_added_and_done_per_person(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-91 — operator: «перед этим [утренними карточками]
    изменения во вчерашнем плане по людям новую сделай (если
    есть изменения)». Admin gets a delta DM listing per-person:
      ➕ added tasks (new on today's plan)
      ✅ tasks done since
      ➖ tasks otherwise removed
    Sent BEFORE the morning intro + cards."""
    from app.models import AuditLog, TeamMember
    from app.telegram_bot.morning_cards import _render_admin_morning_diff

    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        yesterday = date(2026, 4, 29)
        today = date(2026, 4, 30)
        with SessionFactory() as s:
            # Three tasks on yesterday's plan owned by 111:
            #   - t_done: now done → ✅
            #   - t_kept: still pending → not in diff
            #   - t_removed: deleted → ➖
            t_done = _mk_task(
                s, title="closed", owner_user_id="111", due_date=today,
                status=TaskStatus.done,
            )
            t_kept = _mk_task(
                s, title="still here", owner_user_id="111", due_date=today,
            )
            t_removed = _mk_task(
                s, title="gone", owner_user_id="111", due_date=today,
            )
            t_removed.deleted_at = datetime(
                2026, 4, 30, tzinfo=timezone.utc
            )
            # New task added today, NOT in yesterday's plan.
            t_added = _mk_task(
                s, title="new today", owner_user_id="111", due_date=today,
            )
            s.add(
                TeamMember(
                    slack_user_id=None,
                    telegram_user_id=111,
                    real_name="Андрей",
                    active=True,
                )
            )
            # Yesterday's evening admin audit row carrying the
            # per-person plan map.
            s.add(
                AuditLog(
                    category="telegram_evening_status",
                    action="admin",
                    entity_type="telegram_evening_status",
                    entity_id=f"admin:{yesterday.isoformat()}",
                    actor="999",
                    payload={
                        "per_person_plan_task_ids": {
                            "111": [t_done.id, t_kept.id, t_removed.id],
                        },
                    },
                )
            )
            s.commit()

            text = _render_admin_morning_diff(
                s, today=today, admin_uid="999",
            )
        assert text is not None
        # Per-person section header present.
        assert "👤 <b>Андрей</b>" in text
        # Done line with ✅.
        assert "✅" in text and "closed" in text
        # Removed line with 🗑 (deleted) — distinct from generic ➖.
        assert "🗑" in text and "gone" in text
        # Added line with ➕.
        assert "➕" in text and "new today" in text
        # Untouched task NOT in diff.
        assert "still here" not in text
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_morning_admin_diff_returns_none_with_no_changes(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-91 — when nothing changed since yesterday's
    plan (every task still pending, no new ones), the diff
    helper returns None and the morning loop sends no
    delta DM."""
    from app.models import AuditLog, TeamMember
    from app.telegram_bot.morning_cards import _render_admin_morning_diff

    yesterday = date(2026, 4, 29)
    today = date(2026, 4, 30)
    with SessionFactory() as s:
        t = _mk_task(s, title="x", owner_user_id="111", due_date=today)
        s.add(
            AuditLog(
                category="telegram_evening_status",
                action="admin",
                entity_type="telegram_evening_status",
                entity_id=f"admin:{yesterday.isoformat()}",
                actor="999",
                payload={
                    "per_person_plan_task_ids": {"111": [t.id]},
                },
            )
        )
        s.commit()
        text = _render_admin_morning_diff(s, today=today, admin_uid="999")
    assert text is None


def test_morning_admin_diff_returns_none_when_no_prior_plan(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-91 — first run has no prior admin audit row →
    nothing to diff against, return None silently."""
    from app.telegram_bot.morning_cards import _render_admin_morning_diff

    today = date(2026, 4, 30)
    with SessionFactory() as s:
        text = _render_admin_morning_diff(s, today=today, admin_uid="999")
    assert text is None


# --------------------------------------------------------------------------- #
# FR-CR-05-84 — delete yesterday's morning cards before posting today's
# --------------------------------------------------------------------------- #


def test_morning_cards_records_card_messages_in_audit_payload(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-84 — the audit row written after a successful run
    carries every (chat_id, message_id) the bot posted today, so
    tomorrow's run can delete them."""
    from app.models import AuditLog

    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(s, title="a", due_date=today)
        _mk_task(s, title="b", due_date=today)
        s.commit()
        sender = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender, today=today)
        s.commit()

    with SessionFactory() as s:
        row = s.query(AuditLog).filter_by(
            category="telegram_morning_cards", actor="111",
        ).one()
    payload = row.payload or {}
    cards = payload.get("card_messages") or []
    # Intro + 2 cards = 3 message_ids.
    assert len(cards) == 3
    for c in cards:
        assert isinstance(c.get("chat_id"), int)
        assert isinstance(c.get("message_id"), int)


def test_morning_cards_deletes_yesterdays_cards_before_posting_today(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-84 — operator: «ты их как бы удаляй если они
    ранее были и создавай заново с утра». On day 2's run, the
    bot must call `deleteMessage` on every (chat_id, message_id)
    saved in yesterday's audit row before posting today's
    intro + cards."""
    from datetime import timedelta as _td

    yesterday = date(2026, 4, 28)
    today = date(2026, 4, 29)

    with SessionFactory() as s:
        # Due yesterday → in yesterday's run as due-today; in
        # today's run as overdue. Either way the task qualifies
        # for both days' digests.
        _mk_task(s, title="rolling task", owner_user_id="111", due_date=yesterday)
        s.commit()
        sender_y = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender_y, today=yesterday)
        s.commit()
    # Sanity — yesterday's run posted intro + 1 card.
    assert len(sender_y.sent) == 2

    # Today's run uses a fresh sender (mimics a separate cron tick).
    sender_t = _RecordingTGSender()
    with SessionFactory() as s:
        report = send_morning_task_cards(s, sender=sender_t, today=today)
        s.commit()

    # Today's sender deleted yesterday's 2 messages first.
    assert len(sender_t.deleted) == 2
    deleted_mids = sorted(d["message_id"] for d in sender_t.deleted)
    assert deleted_mids == [1, 2]
    # The report counter exposes the cleanup.
    assert report.prior_cards_deleted == 2
    # Today still posted intro + 1 card (delete-then-post, not
    # skip).
    assert len(sender_t.sent) == 2


def test_morning_cards_no_prior_audit_row_means_no_delete_calls(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-84 — first-ever run has nothing to delete; the
    sender's `delete_message` must not be called at all."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(s, title="x", due_date=today)
        s.commit()
        sender = _RecordingTGSender()
        report = send_morning_task_cards(s, sender=sender, today=today)
        s.commit()
    assert sender.deleted == []
    assert report.prior_cards_deleted == 0


def test_morning_cards_delete_failures_dont_abort_today_post(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-84 — Telegram refuses deletions older than 48h
    or for messages that no longer exist. Per-message failures
    must be swallowed; today's intro + cards still post."""
    yesterday = date(2026, 4, 28)
    today = date(2026, 4, 29)

    with SessionFactory() as s:
        _mk_task(s, title="x", owner_user_id="111", due_date=yesterday)
        s.commit()
        sender_y = _RecordingTGSender()
        send_morning_task_cards(s, sender=sender_y, today=yesterday)
        s.commit()

    sender_t = _RecordingTGSender()
    sender_t.delete_should_raise = True
    with SessionFactory() as s:
        report = send_morning_task_cards(s, sender=sender_t, today=today)
        s.commit()
    # Despite delete failures, today's run posted the digest.
    assert len(sender_t.sent) >= 2  # intro + at least 1 card
    assert report.recipients == 1
    # `prior_cards_deleted` counts only successful deletes.
    assert report.prior_cards_deleted == 0


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
