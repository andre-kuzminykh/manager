"""FR-CR-05-40 — evening status report (per-task LLM narrative).

Covers:
- Group selection: tasks land in the right section (Done /
  In progress / Todo / Subscriptions); subscription-only
  recipients still get a DM.
- LLM integration: `compose_status_narrative` is called once
  per task; LLM exceptions / empty replies fall back to a
  deterministic 1-liner.
- Rendering: HTML hyperlinks on title (`<a href=permalink>`)
  and on the owner deeplink for the admin overview.
- Multi-message split: long reports split at line boundaries,
  every chunk stays under the 4096-char Telegram cap.
- Idempotency: re-running on the same date is a no-op
  (audit_logs entries created and re-checked).
- Admin overview: every admin uid receives the consolidated
  «Сводка по команде» that lists tasks owned by other people.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    AuditLog,
    Task,
    TaskStatus,
    TaskStatusHistory,
    TaskSubscription,
    TeamMember,
)
from app.models.task import TaskPriority
from app.telegram_bot.evening_status import (
    _split_groups_into_messages,
    _TaskGroup,
    compose_status_narrative,
    send_evening_status_report,
)


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #


class _StubLLM:
    """Records every per-task complete_text call. Returns a
    canned string by default; can be configured to raise or
    return empty."""

    def __init__(self, *, reply: str = "Прогресс: что-то сделано.", raise_on_call: bool = False, return_empty: bool = False):
        self.reply = reply
        self.raise_on_call = raise_on_call
        self.return_empty = return_empty
        self.calls: list[dict] = []

    def complete_text(self, *, system_prompt, user_prompt, model=None, temperature=0.2):
        self.calls.append(
            {"system": system_prompt, "user": user_prompt}
        )
        if self.raise_on_call:
            raise RuntimeError("LLM down")
        if self.return_empty:
            return ""
        return self.reply


class _RecordingTGSender:
    """Mimics `TelegramSender`. Records every send_message call
    and reports `enabled=True` for the listener-handler checks."""

    def __init__(self):
        self.enabled = True
        self.sent: list[dict] = []

    def send_message(self, *, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return {"message_id": len(self.sent)}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _mk_task(s, **kw) -> Task:
    base = dict(
        title="t",
        status=TaskStatus.todo,
        owner_user_id="111",  # numeric → Telegram
        priority=TaskPriority.medium,
        is_current_week=True,
    )
    base.update(kw)
    t = Task(**base)
    s.add(t)
    s.flush()
    # FR-CR-05-66 — auto-seed has_started_bot for the owner so
    # the evening-status recipient filter doesn't drop them.
    if t.owner_user_id and str(t.owner_user_id).lstrip("-").isdigit():
        _mark_started_bot(s, int(t.owner_user_id))
    return t


def _mark_started_bot(s, user_id: int) -> None:
    """FR-CR-05-66 — seed the `telegram_chat_members` row that
    flags this uid as has_started_bot=True so the recipient
    filter lets them through."""
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


def _mk_done_history(s, *, task_id: int, when: datetime, by: str = "111") -> None:
    s.add(
        TaskStatusHistory(
            task_id=task_id,
            from_status=TaskStatus.in_progress,
            to_status=TaskStatus.done,
            changed_by_slack_user_id=by,
            at=when,
        )
    )


# --------------------------------------------------------------------------- #
# Group selection
# --------------------------------------------------------------------------- #


def test_evening_status_groups_done_in_progress_todo(
    patched_session_scope, SessionFactory
):
    """Single user, one task in each of Done/InProgress/Todo —
    the report has three sections, each labelled with its
    count, and the LLM is called once per task."""
    today = date(2026, 4, 29)
    midnight = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    with SessionFactory() as s:
        t_done = _mk_task(s, title="closed today", status=TaskStatus.done)
        _mk_done_history(s, task_id=t_done.id, when=midnight + timedelta(hours=10))
        t_ip = _mk_task(s, title="in flight", status=TaskStatus.in_progress)
        t_todo = _mk_task(s, title="planned", status=TaskStatus.todo)
        s.commit()

        sender = _RecordingTGSender()
        llm = _StubLLM(reply="Описание прогресса.")
        report = send_evening_status_report(
            s, sender=sender, llm=llm, today=today,
            include_admin_overview=False,
        )
        s.commit()

    assert report.recipients == 1
    assert report.tasks_described == 3
    # One LLM call per task.
    assert len(llm.calls) == 3
    # FR-CR-05-83 — two DMs now: the status digest + the
    # tomorrow-plan follow-up. The status digest is the first
    # message, then the plan is the second.
    assert len(sender.sent) == 2
    body = sender.sent[0]["text"]
    assert "✅ Done today" in body
    assert "🚀 In progress" in body
    assert "📋 Todo" in body
    assert "closed today" in body and "in flight" in body and "planned" in body
    # The tomorrow-plan follow-up names the same open tasks.
    plan_body = sender.sent[1]["text"]
    assert "Plan for tomorrow" in plan_body
    assert "in flight" in plan_body and "planned" in plan_body
    assert report.tomorrow_plans_sent == 1


def test_evening_status_subscriber_only_user_still_gets_dm(
    patched_session_scope, SessionFactory
):
    """A user who owns nothing but subscribes to one open task
    still receives the report with a 👀 Watching section."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        owner = _mk_task(s, title="someone else's task", owner_user_id="222")
        s.add(TaskSubscription(task_id=owner.id, slack_user_id="333"))
        # Subscriber 333 isn't an owner anywhere → auto-seed
        # has_started_bot manually so the FR-CR-05-66 filter
        # lets them through.
        _mark_started_bot(s, 333)
        s.commit()

        sender = _RecordingTGSender()
        report = send_evening_status_report(
            s, sender=sender, llm=_StubLLM(),
            today=today, include_admin_overview=False,
        )
        s.commit()

    # Two recipients: owner 222 + subscriber 333.
    assert report.recipients == 2
    chats = sorted(m["chat_id"] for m in sender.sent)
    assert 222 in chats and 333 in chats
    sub_dm = next(m for m in sender.sent if m["chat_id"] == 333)
    assert "👀 Watching" in sub_dm["text"]


def test_evening_status_skips_user_with_no_tasks(
    patched_session_scope, SessionFactory
):
    """An owner whose only open task gets soft-deleted has nothing
    to report; the function records the audit row anyway so a
    re-run still skips."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        # No active rows for uid 111 — _telegram_owner_ids() returns
        # empty list → no recipients at all.
        sender = _RecordingTGSender()
        report = send_evening_status_report(
            s, sender=sender, llm=_StubLLM(),
            today=today, include_admin_overview=False,
        )
        s.commit()
    assert report.recipients == 0
    assert sender.sent == []


# --------------------------------------------------------------------------- #
# LLM integration / fallback
# --------------------------------------------------------------------------- #


def test_compose_narrative_falls_back_when_llm_raises(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        t = _mk_task(s, title="x", priority=TaskPriority.high, due_date=today)
        s.commit()
        out = compose_status_narrative(
            llm=_StubLLM(raise_on_call=True), session=s, task=t, today=today
        )
    # Deterministic fallback contains "статус todo" (we set status
    # to todo by default in _mk_task).
    assert "статус todo" in out
    assert "приоритет high" in out


def test_compose_narrative_falls_back_when_llm_returns_empty(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        t = _mk_task(s, title="x")
        s.commit()
        out = compose_status_narrative(
            llm=_StubLLM(return_empty=True), session=s, task=t, today=today
        )
    assert "статус" in out  # fallback shape


def test_compose_narrative_truncates_long_response(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        t = _mk_task(s, title="x")
        s.commit()
        long = "слово " * 200  # ~1200 chars
        out = compose_status_narrative(
            llm=_StubLLM(reply=long), session=s, task=t, today=today
        )
    assert len(out) <= 240
    assert out.endswith("…")


def test_evening_status_works_without_llm(
    patched_session_scope, SessionFactory
):
    """Caller can pass `llm=None` (no API key configured); the
    narrative falls back to deterministic 1-liners but the DM
    still ships."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(s, title="planned", status=TaskStatus.todo)
        s.commit()
        sender = _RecordingTGSender()
        report = send_evening_status_report(
            s, sender=sender, llm=None, today=today,
            include_admin_overview=False,
        )
        s.commit()
    assert report.recipients == 1
    # FR-CR-05-83 — status digest + tomorrow plan = 2 DMs.
    assert len(sender.sent) == 2
    assert "статус todo" in sender.sent[0]["text"]
    assert "Plan for tomorrow" in sender.sent[1]["text"]


# --------------------------------------------------------------------------- #
# Rendering / hyperlinks
# --------------------------------------------------------------------------- #


def test_evening_status_renders_title_as_hyperlink_to_bot_card(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-48 — title hyperlink points at the BOT'S task
    card (`tg://openmessage?...` deep link to the recipient's
    DM with the bot), NOT the source-message permalink the
    operator dislikes («ссылка именно на карточку с
    сообщением с задачей в боте, а не с сообщением в чате»)."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        t = _mk_task(
            s,
            title="follow-up call",
            owner_user_id="111",
            source_permalink="https://t.me/c/123/456",
        )
        # Recipient 111's card lives in their DM — message id 9001.
        t.extra = {"telegram_cards": [{"chat_id": 111, "message_id": 9001}]}
        s.commit()
        sender = _RecordingTGSender()
        # The sender's `_token` is what the renderer reads to derive
        # the bot user_id for the deep link.
        sender._token = "8675374199:fake-secret"
        send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()
    body = sender.sent[0]["text"]
    # Hyperlink points at the bot's DM card, not the chat permalink.
    # The `&` in the URL is HTML-escaped to `&amp;` (which Telegram
    # parses back into a working link client-side).
    assert "tg://openmessage?user_id=8675374199&amp;message_id=9001" in body
    assert "<b>follow-up call</b>" in body
    # Source permalink does NOT leak into the rendered title.
    assert "t.me/c/123/456" not in body


def test_evening_status_title_no_hyperlink_when_card_url_unknown(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-48 — when the recipient has no stored card (and
    the task isn't in a -100 supergroup), the title renders
    plain. We deliberately stop falling back to source_permalink
    so the operator never sees a chat-message link in the
    evening report."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(
            s,
            title="no card",
            owner_user_id="111",
            source_permalink="https://t.me/c/999/777",
        )
        s.commit()
        sender = _RecordingTGSender()
        sender._token = "8675374199:secret"
        send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()
    body = sender.sent[0]["text"]
    # Plain title, no <a> tag at all.
    assert "<b>no card</b>" in body
    assert "<a href=" not in body


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_evening_status_idempotent_per_user_per_day(
    patched_session_scope, SessionFactory
):
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        _mk_task(s, title="x")
        s.commit()
        sender = _RecordingTGSender()
        first = send_evening_status_report(
            s, sender=sender, llm=_StubLLM(),
            today=today, include_admin_overview=False,
        )
        s.commit()
    assert first.recipients == 1
    sent_first = list(sender.sent)

    with SessionFactory() as s:
        second = send_evening_status_report(
            s, sender=sender, llm=_StubLLM(),
            today=today, include_admin_overview=False,
        )
        s.commit()
    # Re-run skipped — no new messages.
    assert second.recipients == 0
    assert second.skipped_idempotent == 1
    assert sender.sent == sent_first


# --------------------------------------------------------------------------- #
# Admin overview
# --------------------------------------------------------------------------- #


def test_evening_status_admin_gets_team_overview(
    patched_session_scope, SessionFactory, monkeypatch
):
    """When `include_admin_overview=True` (default), every admin
    uid additionally receives a `Сводка по команде` covering
    every task in the system."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
    # Settings is lru_cache'd, so flip the cache before the test
    # and after, to make sure the env var actually lands.
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        today = date(2026, 4, 29)
        with SessionFactory() as s:
            # Two different owners; admin owns nothing.
            _mk_task(s, title="task A", owner_user_id="111")
            _mk_task(s, title="task B", owner_user_id="222")
            s.commit()
            sender = _RecordingTGSender()
            report = send_evening_status_report(
                s, sender=sender, llm=_StubLLM(reply="ok"), today=today,
                include_admin_overview=True,
            )
            s.commit()

        chats = [m["chat_id"] for m in sender.sent]
        assert 999 in chats
        admin_dm = next(m for m in sender.sent if m["chat_id"] == 999)
        body = admin_dm["text"]
        assert "Team summary" in body
        assert "task A" in body and "task B" in body
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Message split
# --------------------------------------------------------------------------- #


def test_split_groups_packs_into_multiple_messages_under_cap():
    """Many lines → multiple messages, each ≤ cap; no message
    exceeds the Telegram 4096-char hard cap."""
    cap = 800  # small cap for test legibility
    g = _TaskGroup(title="✅ Done (50)", lines=[f"line-{i:02d} " * 10 for i in range(50)])
    msgs = _split_groups_into_messages(
        header="<b>HEADER</b>", groups=[g], cap=cap
    )
    assert len(msgs) > 1
    for m in msgs:
        assert len(m) <= cap + 200  # +200 for the section-header overhead
    # Continuation marker present on follow-up messages.
    assert any("(continued)" in m for m in msgs[1:])


def test_split_groups_single_message_when_short():
    g = _TaskGroup(title="🚀 1 task", lines=["• short line"])
    msgs = _split_groups_into_messages(
        header="<b>HEADER</b>", groups=[g], cap=3800
    )
    assert len(msgs) == 1
    assert "<b>HEADER</b>" in msgs[0]
    assert "🚀 1 task" in msgs[0]


def test_evening_status_splits_long_report_into_multiple_messages(
    patched_session_scope, SessionFactory
):
    """An owner with many tasks gets several DMs in sequence,
    none of which exceed the Telegram 4096-char cap."""
    today = date(2026, 4, 29)
    with SessionFactory() as s:
        for i in range(80):
            _mk_task(s, title=f"task-{i:02d} " * 8)
        s.commit()
        sender = _RecordingTGSender()
        # LLM returns a long-ish reply per task to fatten things up.
        llm = _StubLLM(reply="Прогресс задачи довольно подробный, в несколько слов." * 4)
        send_evening_status_report(
            s, sender=sender, llm=llm, today=today,
            include_admin_overview=False,
        )
        s.commit()
    assert len(sender.sent) >= 2
    for m in sender.sent:
        assert len(m["text"]) <= 4096


# --------------------------------------------------------------------------- #
# FR-CR-05-83 — tomorrow plan as a second message
# --------------------------------------------------------------------------- #


def test_evening_tomorrow_plan_lists_tasks_for_next_day(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-83 — operator: «след сообщением пост со списком
    задач моих на завтра с гиперссылками на эти задачи в боте».
    The second evening message must list the user's open tasks
    scheduled for tomorrow (due_date == tomorrow), with title
    hyperlinks pointing at the BOT'S task cards."""
    today = date(2026, 4, 29)
    tomorrow = today + timedelta(days=1)
    with SessionFactory() as s:
        # Three tasks for tomorrow + one far-away (excluded).
        t1 = _mk_task(s, title="ship demo", due_date=tomorrow)
        t2 = _mk_task(s, title="review PR", due_date=tomorrow)
        _mk_task(s, title="next month", due_date=tomorrow + timedelta(days=20))
        # Hook a stored bot card so the title hyperlinks.
        t1.extra = {"telegram_cards": [{"chat_id": 111, "message_id": 9001}]}
        s.commit()
        sender = _RecordingTGSender()
        sender._token = "8675374199:secret"
        report = send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()

    # 1 status digest + 1 tomorrow plan = 2 DMs.
    assert len(sender.sent) == 2
    plan_body = sender.sent[1]["text"]
    assert f"Plan for tomorrow ({tomorrow.isoformat()})" in plan_body
    assert "ship demo" in plan_body
    assert "review PR" in plan_body
    assert "next month" not in plan_body
    # Hyperlink for the task with a stored bot card.
    assert "tg://openmessage?user_id=8675374199&amp;message_id=9001" in plan_body
    assert report.tomorrow_plans_sent == 1


def test_evening_tomorrow_plan_includes_overdue_today(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-83 — overdue tasks (due_date < tomorrow,
    status != done) roll forward into the tomorrow plan with
    the 🚨 bullet, since they're work that didn't close
    today."""
    today = date(2026, 4, 29)
    yesterday = today - timedelta(days=1)
    with SessionFactory() as s:
        _mk_task(s, title="rolling", due_date=yesterday)
        s.commit()
        sender = _RecordingTGSender()
        send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()
    plan_body = sender.sent[1]["text"]
    assert "rolling" in plan_body
    # 🚨 bullet on the overdue line + count line at the top.
    assert "🚨" in plan_body
    assert "Rolling over from today: 1" in plan_body


def test_evening_tomorrow_plan_skipped_when_no_tasks(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-83 — when the user has nothing for tomorrow, the
    bot does NOT send a second empty message."""
    today = date(2026, 4, 29)
    far = today + timedelta(days=30)
    with SessionFactory() as s:
        # User 111 owns one far-future task (qualifies them as a
        # recipient via _telegram_owner_ids → status digest sends),
        # but the task does NOT match the tomorrow selector
        # (due_date is too far, not in_progress, not
        # is_current_week-with-no-due-date).
        _mk_task(
            s, title="next month", due_date=far, is_current_week=False,
        )
        s.commit()
        sender = _RecordingTGSender()
        report = send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()
    # Only the status digest, no tomorrow plan.
    assert len(sender.sent) == 1
    assert "Plan for tomorrow" not in sender.sent[0]["text"]
    assert report.tomorrow_plans_sent == 0


def test_evening_tomorrow_plan_long_list_splits_into_multiple_messages(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-83 — when an owner has too many tomorrow tasks
    to fit under the 4096-char Telegram cap, the plan splits at
    task boundaries. Every sent chunk stays ≤4096 chars and a
    `(continued)` marker shows on follow-ups."""
    today = date(2026, 4, 29)
    tomorrow = today + timedelta(days=1)
    with SessionFactory() as s:
        for i in range(80):
            _mk_task(s, title=f"task-{i:02d} " * 8, due_date=tomorrow)
        s.commit()
        sender = _RecordingTGSender()
        send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()
    # All chunks (status digest + tomorrow plan) ≤4096 chars.
    for m in sender.sent:
        assert len(m["text"]) <= 4096
    # At least one tomorrow-plan continuation appeared.
    plan_chunks = [m for m in sender.sent if "Plan for tomorrow" in m["text"] or "(continued)" in m["text"]]
    assert len(plan_chunks) >= 2


# --------------------------------------------------------------------------- #
# FR-CR-05-91 — self-owner badge skip + admin per-person plan
# --------------------------------------------------------------------------- #


def test_evening_tomorrow_plan_drops_owner_badge_when_recipient_is_owner(
    patched_session_scope, SessionFactory
):
    """FR-CR-05-91 — operator: «тут если для меня то не пиши».
    The «👤 owner» line on a task line is redundant when the
    recipient owns that task — drop it. Subscribed-task lines
    keep the owner badge (it's somebody else's task)."""
    today = date(2026, 4, 29)
    tomorrow = today + timedelta(days=1)
    with SessionFactory() as s:
        # Recipient 111 owns task A. They don't own task B.
        _mk_task(s, title="my own task", owner_user_id="111", due_date=tomorrow)
        s.commit()
        sender = _RecordingTGSender()
        send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()

    plan_body = sender.sent[1]["text"]
    assert "my own task" in plan_body
    # No «👤 …» line for the recipient's own task.
    assert "👤" not in plan_body


def test_evening_admin_tomorrow_plan_groups_per_person(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-91 — operator: «выводи админу план всех людей
    на завтра, прям в разрезе людей». Admin's tomorrow plan
    must be a per-person breakdown, not the admin's own task
    list."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        today = date(2026, 4, 29)
        tomorrow = today + timedelta(days=1)
        with SessionFactory() as s:
            _mk_task(s, title="report A", owner_user_id="111", due_date=tomorrow)
            _mk_task(s, title="report B", owner_user_id="111", due_date=tomorrow)
            _mk_task(s, title="email C", owner_user_id="222", due_date=tomorrow)
            s.commit()
            # Add nice display names so the per-person headers render
            # something useful.
            s.add(
                TeamMember(
                    slack_user_id=None,
                    telegram_user_id=111,
                    real_name="Андрей",
                    active=True,
                )
            )
            s.add(
                TeamMember(
                    slack_user_id=None,
                    telegram_user_id=222,
                    real_name="Ирина",
                    active=True,
                )
            )
            s.commit()
            sender = _RecordingTGSender()
            send_evening_status_report(
                s, sender=sender, llm=_StubLLM(reply="ok"), today=today,
                include_admin_overview=True,
            )
            s.commit()

        admin_dms = [m for m in sender.sent if m["chat_id"] == 999]
        # Find the per-person plan DM — the one with «Team plan for tomorrow».
        plan_dms = [m for m in admin_dms if "Team plan for tomorrow" in m["text"]]
        assert len(plan_dms) >= 1
        body = plan_dms[0]["text"]
        # Per-person section headers present.
        assert "👤 <b>Андрей</b>" in body
        assert "👤 <b>Ирина</b>" in body
        # Counts per person.
        assert "2 tasks" in body
        assert "1 task" in body
        # Task titles present.
        assert "report A" in body and "report B" in body and "email C" in body
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_evening_admin_audit_payload_carries_per_person_plan_task_ids(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-91 — the admin audit row must persist the
    per-person task ID map so the morning admin diff can
    compute deltas."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "999")
    from app.config import get_settings
    from app.models import AuditLog

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        today = date(2026, 4, 29)
        tomorrow = today + timedelta(days=1)
        with SessionFactory() as s:
            _mk_task(s, title="A", owner_user_id="111", due_date=tomorrow)
            _mk_task(s, title="B", owner_user_id="222", due_date=tomorrow)
            s.commit()
            sender = _RecordingTGSender()
            send_evening_status_report(
                s, sender=sender, llm=_StubLLM(reply="ok"), today=today,
                include_admin_overview=True,
            )
            s.commit()
        with SessionFactory() as s:
            row = (
                s.query(AuditLog)
                .filter_by(
                    category="telegram_evening_status",
                    action="admin",
                    actor="999",
                )
                .one()
            )
        payload = row.payload or {}
        per_person = payload.get("per_person_plan_task_ids") or {}
        assert "111" in per_person and "222" in per_person
        assert len(per_person["111"]) == 1 and len(per_person["222"]) == 1
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# FR-CR-05-49 — overdue badge
# --------------------------------------------------------------------------- #


def test_evening_status_overdue_task_renders_with_alarm_bullet(
    patched_session_scope, SessionFactory
):
    """Tasks whose `due_date` has passed get a 🚨 bullet that
    overrides the priority colour. Closed tasks are unaffected
    (✅ wins). Tasks without a `due_date` are never overdue."""
    from datetime import timedelta

    today = date(2026, 4, 29)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    with SessionFactory() as s:
        _mk_task(s, title="overdue", due_date=yesterday)
        _mk_task(s, title="future", due_date=tomorrow)
        _mk_task(s, title="no-due")
        # Done tasks aren't overdue even if due_date is in the past.
        _mk_task(
            s, title="done-yesterday", due_date=yesterday,
            status=TaskStatus.done,
        )
        s.commit()
        sender = _RecordingTGSender()
        send_evening_status_report(
            s, sender=sender, llm=_StubLLM(reply="ok"),
            today=today, include_admin_overview=False,
        )
        s.commit()

    body = sender.sent[0]["text"]
    # Overdue task carries the alarm bullet.
    assert "🚨" in body
    # The line for "overdue" specifically is prefixed with 🚨,
    # not the priority emoji 🟡.
    overdue_idx = body.find("overdue")
    assert overdue_idx > 0
    assert "🚨" in body[max(0, overdue_idx - 10) : overdue_idx]
    # Future task and no-due aren't flagged.
    future_idx = body.find("future")
    no_due_idx = body.find("no-due")
    for idx in (future_idx, no_due_idx):
        if idx > 0:
            assert "🚨" not in body[max(0, idx - 10) : idx]
    # Done task uses ✅, NOT 🚨, even though due_date is in the past.
    done_idx = body.find("done-yesterday")
    if done_idx > 0:
        assert "✅" in body[max(0, done_idx - 10) : done_idx]
