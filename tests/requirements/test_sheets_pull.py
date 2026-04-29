"""FR-CR-05-11 — Sheet → DB pull (operator edits propagate back)."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from app.models import (
    Employee,
    Task,
    TaskPriority,
    TaskSourceKind,
    TaskStatus,
    TaskStatusHistory,
    TeamMember,
)
from app.sync.sheets import SheetsPullService


class _StubPull(SheetsPullService):
    """Bypass the googleapiclient build — feed rows directly."""

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self._spreadsheet_id = "stub"
        self._sheet_name = "Main"
        self._service = None  # noqa: SLF001

    def _read_all(self) -> list[list[str]]:  # type: ignore[override]
        return self._rows


def _mk_task(session, **kw) -> Task:
    base = dict(
        title="t",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        owner_user_id="111",
        owner_display_name="Petya",
        source_kind=TaskSourceKind.telegram,
    )
    base.update(kw)
    t = Task(**base)
    session.add(t)
    session.flush()
    return t


_HEADER = [
    "task_id", "title", "description", "owner", "priority", "category",
    "start_date", "start_time", "due_date", "due_time",
    "is_recurring", "recurring_weekdays",
    "recurring_start_time", "recurring_end_time",
    "status", "parent_task_id", "source", "source_permalink",
    "created_at", "updated_at", "deleted_at", "completion_artifact",
]


def _row_for(
    task_id: int,
    *,
    title="x",
    description="",
    owner="",
    priority="medium",
    category="",
    start_date="",
    start_time="",
    due_date="",
    due_time="",
    status="todo",
    completion_artifact="",
) -> list[str]:
    return [
        str(task_id), title, description, owner, priority, category,
        start_date, start_time, due_date, due_time,
        "", "", "", "",
        status, "", "", "",
        "", "", "", completion_artifact,
    ]


def test_pull_applies_editable_fields_only(session):
    t = _mk_task(session, title="old", priority=TaskPriority.medium)
    pull = _StubPull(
        [
            _HEADER,
            _row_for(
                t.id,
                title="new title",
                description="new desc",
                priority="high",
                category="ops",
                due_date="2026-05-10",
                due_time="14:30",
                completion_artifact="https://example.com/result",
                # Read-only columns — should be IGNORED even when populated.
                # We achieve that here by keeping `_row_for`'s defaults
                # for source / source_permalink / created_at / etc.
            ),
        ]
    )
    seen, changed, skipped = pull.pull(session)
    assert (seen, changed, skipped) == (1, 1, 0)
    session.refresh(t)
    assert t.title == "new title"
    assert t.description == "new desc"
    assert t.priority == TaskPriority.high
    assert t.category == "ops"
    assert t.due_date == date(2026, 5, 10)
    assert t.due_time == time(14, 30)
    assert t.completion_artifact == "https://example.com/result"


def test_pull_routes_status_change_through_transition_service(session):
    """Status flip from the sheet must produce a `task_status_history`
    row — same audit trail as a button click."""
    t = _mk_task(session, status=TaskStatus.todo)
    pull = _StubPull([_HEADER, _row_for(t.id, status="in_progress")])
    pull.pull(session)
    session.refresh(t)
    assert t.status == TaskStatus.in_progress
    history = (
        session.query(TaskStatusHistory)
        .filter_by(task_id=t.id)
        .all()
    )
    assert any(h.to_status == TaskStatus.in_progress for h in history)


def test_pull_drops_invalid_priority_silently(session):
    t = _mk_task(session, priority=TaskPriority.medium)
    pull = _StubPull([_HEADER, _row_for(t.id, priority="urgent!!!")])
    pull.pull(session)
    session.refresh(t)
    assert t.priority == TaskPriority.medium  # unchanged


def test_pull_drops_invalid_status_silently(session):
    t = _mk_task(session, status=TaskStatus.todo)
    pull = _StubPull([_HEADER, _row_for(t.id, status="lunch")])
    pull.pull(session)
    session.refresh(t)
    assert t.status == TaskStatus.todo


def test_pull_resolves_owner_by_uid_handle_and_realname(session):
    """Each of the three owner-resolution paths must hit the right
    `team_members` row and persist the right id."""
    session.add_all(
        [
            TeamMember(
                real_name="Andre Kuzminykh",
                telegram_user_id=222968032,
                telegram_username="andre_andreevich",
                active=True,
            ),
            TeamMember(
                real_name="Petya Pupkin",
                telegram_user_id=42,
                telegram_username="petya",
                active=True,
            ),
        ]
    )
    session.flush()

    t1 = _mk_task(session, title="t1", owner_user_id=None, owner_display_name=None)
    t2 = _mk_task(session, title="t2", owner_user_id=None, owner_display_name=None)
    t3 = _mk_task(session, title="t3", owner_user_id=None, owner_display_name=None)
    pull = _StubPull(
        [
            _HEADER,
            # Bare uid (Slack-style) — kept as-is.
            _row_for(t1.id, owner="U09SLACK99"),
            # @handle → resolve via telegram_username.
            _row_for(t2.id, owner="@petya"),
            # Real-name match — TG row.
            _row_for(t3.id, owner="Andre Kuzminykh"),
        ]
    )
    pull.pull(session)
    session.refresh(t1)
    session.refresh(t2)
    session.refresh(t3)
    assert t1.owner_user_id == "U09SLACK99"
    assert t2.owner_user_id == "42"
    assert t3.owner_user_id == "222968032"


def test_pull_keeps_unresolvable_owner_text_as_display_name(session):
    """Operator typed an unrecognised name. The pull keeps it as
    `owner_display_name` so their intent is visible on the next
    push, with `owner_user_id` cleared (unassigned)."""
    t = _mk_task(session, owner_user_id="111", owner_display_name="Petya")
    pull = _StubPull([_HEADER, _row_for(t.id, owner="John from Acme")])
    pull.pull(session)
    session.refresh(t)
    assert t.owner_user_id is None
    assert t.owner_display_name == "John from Acme"


def test_pull_skips_soft_deleted_and_missing_tasks(session):
    t_alive = _mk_task(session, title="alive")
    t_dead = _mk_task(session, title="dead")
    t_dead.deleted_at = datetime.now(timezone.utc)
    session.flush()
    pull = _StubPull(
        [
            _HEADER,
            _row_for(t_alive.id, title="renamed"),
            _row_for(t_dead.id, title="should-not-apply"),
            _row_for(99999, title="ghost"),  # task not in DB
            ["", "no_id", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        ]
    )
    seen, changed, skipped = pull.pull(session)
    # 4 rows seen; 1 changed (alive), 3 skipped (dead, missing, no_id).
    assert seen == 4
    assert changed == 1
    assert skipped == 3
    session.refresh(t_alive)
    assert t_alive.title == "renamed"
    session.refresh(t_dead)
    assert t_dead.title == "dead"


def test_pull_handles_empty_or_header_only_sheet(session):
    pull_empty = _StubPull([])
    assert pull_empty.pull(session) == (0, 0, 0)
    pull_header = _StubPull([_HEADER])
    assert pull_header.pull(session) == (0, 0, 0)


# --------------------------------------------------------------------------- #
# FR-CR-05-14 — dialogue column
# --------------------------------------------------------------------------- #


def test_task_row_includes_dialogue_from_extra(session):
    """`_task_row` reads the adaptive-context dialogue from
    `task.extra["context_dialogue"]` and lays it as the last
    column."""
    from app.sync.sheets import _HEADER_ROW, _task_row

    t = _mk_task(
        session,
        title="x",
        extra={"context_dialogue": "U1: давай отчёт\nU2: ок, к пятнице"},
    )
    row = _task_row(t, session=session)
    # Column count matches header.
    assert len(row) == len(_HEADER_ROW)
    # Last column is `dialogue`.
    assert _HEADER_ROW[-1] == "dialogue"
    assert row[-1] == "U1: давай отчёт\nU2: ок, к пятнице"


def test_task_row_dialogue_empty_when_no_extra(session):
    from app.sync.sheets import _task_row

    t = _mk_task(session, title="x", extra=None)
    row = _task_row(t, session=session)
    assert row[-1] == ""
