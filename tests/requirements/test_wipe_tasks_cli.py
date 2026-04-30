"""FR-CR-05-91 — `ops/wipe_tasks.py` data-wipe CLI.

Operator: «давай обнулим все данные по задачам и начнем вести
их заново».
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.models import (
    ActionDraft,
    ActionDraftState,
    AuditLog,
    GoogleSheetsSync,
    IntentInference,
    Task,
    TaskStatus,
    TaskStatusHistory,
    TaskSubscription,
    TeamMember,
    TelegramChatMember,
)
from app.models.task import TaskPriority


def test_wipe_dry_run_keeps_all_rows(
    patched_session_scope, SessionFactory, monkeypatch
):
    """`--dry-run` (default without `--yes`) must not delete
    anything, only log the row counts."""
    import ops.wipe_tasks as mod

    with SessionFactory() as s:
        s.add(
            Task(
                title="t",
                owner_user_id="111",
                priority=TaskPriority.medium,
                status=TaskStatus.todo,
            )
        )
        s.commit()

    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.wipe_tasks", "--dry-run"])
    rc = mod.main()
    assert rc == 0
    with SessionFactory() as s:
        assert s.query(Task).count() == 1


def test_wipe_without_yes_flag_is_dry_run(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Bare `python -m ops.wipe_tasks` (no flags) must default to
    dry-run — `--yes` is required to actually wipe."""
    import ops.wipe_tasks as mod

    with SessionFactory() as s:
        s.add(
            Task(
                title="t",
                owner_user_id="111",
                priority=TaskPriority.medium,
                status=TaskStatus.todo,
            )
        )
        s.commit()

    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.wipe_tasks"])
    rc = mod.main()
    assert rc == 0
    with SessionFactory() as s:
        # Untouched.
        assert s.query(Task).count() == 1


def test_wipe_with_yes_clears_task_data_keeps_team_registry(
    patched_session_scope, SessionFactory, monkeypatch
):
    """`--yes` wipes tasks + drafts + history + sheets-sync +
    audit-logs (filtered) + subscriptions, but PRESERVES
    team_members, telegram_chat_members, processed_telegram_messages,
    meeting_recordings."""
    import ops.wipe_tasks as mod

    with SessionFactory() as s:
        # Create representative rows in every wiped + every
        # preserved table.
        t = Task(
            title="t",
            owner_user_id="111",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
        )
        s.add(t)
        s.flush()
        s.add(
            TaskStatusHistory(
                task_id=t.id,
                from_status=TaskStatus.todo,
                to_status=TaskStatus.in_progress,
                changed_by_slack_user_id="111",
                at=datetime(2026, 4, 30, tzinfo=timezone.utc),
            )
        )
        s.add(TaskSubscription(task_id=t.id, slack_user_id="222"))
        s.add(GoogleSheetsSync(task_id=t.id, spreadsheet_id="s", row_id=42))
        inf = IntentInference(
            intent="create_task",
            confidence=0.9,
            invocation_type="passive",
            raw={},
        )
        s.add(inf)
        s.flush()
        s.add(
            ActionDraft(
                inference_id=inf.id,
                intent="create_task",
                state=ActionDraftState.proposed,
                payload={"title": "x"},
            )
        )
        s.add(
            AuditLog(
                category="telegram_morning_cards",
                action="morning",
                entity_type="telegram_morning_cards",
                entity_id="2026-04-30",
                actor="111",
                payload={},
            )
        )
        s.add(
            AuditLog(
                category="team_sheet_sync",
                action="pull",
                entity_type="team_sheet_sync",
                entity_id="2026-04-30",
                actor="system",
                payload={},
            )
        )
        # Preserved tables.
        s.add(
            TeamMember(
                slack_user_id=None,
                telegram_user_id=111,
                real_name="Андрей",
                active=True,
            )
        )
        s.add(
            TelegramChatMember(chat_id=111, user_id=111, has_started_bot=True)
        )
        s.commit()

    monkeypatch.setattr(mod, "session_scope", patched_session_scope)
    monkeypatch.setattr(mod.sys, "argv", ["ops.wipe_tasks", "--yes"])
    rc = mod.main()
    assert rc == 0

    with SessionFactory() as s:
        # Wiped.
        assert s.query(Task).count() == 0
        assert s.query(TaskStatusHistory).count() == 0
        assert s.query(TaskSubscription).count() == 0
        assert s.query(GoogleSheetsSync).count() == 0
        assert s.query(ActionDraft).count() == 0
        # Wiped audit category.
        assert (
            s.query(AuditLog)
            .filter(AuditLog.category == "telegram_morning_cards")
            .count()
        ) == 0
        # Preserved audit category (team_sheet_sync is not in the
        # wipe list — operator-managed bookmark).
        assert (
            s.query(AuditLog)
            .filter(AuditLog.category == "team_sheet_sync")
            .count()
        ) == 1
        # Registry kept.
        assert s.query(TeamMember).count() == 1
        assert s.query(TelegramChatMember).count() == 1
