"""FR-CR-05-199 — ID-locked tests для Slack publish all-tasks-in-thread.

Operator-pinned 2026-05-22:
  «все задачи в поток а те кто в дирекции приоритетной - в слак идут»

Контракт:
  - `_build_todo_section(filter_by_direction=False)` возвращает ВСЕ tasks
    (включая direction='other'), не только DIRECTIONS_IMPORTANT.
  - `_build_todo_section(filter_by_direction=True)` (default) — только
    important (backwards-compat для legacy auto-publish).
  - `publish_zoom_recording_to_slack(all_tasks_in_thread=True)` шлёт:
      * parent: short_summary + важные tasks (TODO: trailer)
      * thread reply: ПОЛНЫЙ todo list (без фильтра)
  - `all_tasks_in_thread=False` (default) — legacy behavior (важные в обоих).
"""
from __future__ import annotations

from datetime import date as _date
from unittest.mock import MagicMock, patch


def _mock_task(*, id, title, direction):
    t = MagicMock()
    t.id = id
    t.title = title
    t.description = title  # стандартное минимальное описание
    t.owner_display_name = "Irina Shipilova"
    t.due_date = _date(2026, 5, 29)
    t.due_time = None
    t.deleted_at = None
    t.extra = {"direction": direction}
    return t


def _stub_session_with_tasks(tasks):
    """Stub SQLAlchemy session whose query().filter().filter().order_by().all()
    returns the given tasks."""
    session = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.order_by.return_value = q
    q.all.return_value = tasks
    session.query.return_value = q
    return session


def test_fr_cr_05_199_thread_contains_all_tasks() -> None:
    """С `filter_by_direction=False` _build_todo_section возвращает ВСЕ
    tasks включая direction='other'."""
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    tasks = [
        _mock_task(id=1, title="Update data room (investors)",
                   direction="investors"),
        _mock_task(id=2, title="Schedule design review (other)",
                   direction="other"),
        _mock_task(id=3, title="Beta sign-off", direction="beta"),
    ]
    session = _stub_session_with_tasks(tasks)
    out = _build_todo_section(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="ff_test",
        filter_by_direction=False,
    )
    # ВСЕ 3 должны попасть
    assert "Update data room" in out
    assert "Schedule design review" in out
    assert "Beta sign-off" in out


def test_fr_cr_05_199_parent_only_important() -> None:
    """Default `filter_by_direction=True` оставляет только important
    (DIRECTIONS_IMPORTANT = beta/budget/design/investors/deliverables).
    `other` отфильтровывается."""
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    tasks = [
        _mock_task(id=1, title="Update data room (investors)",
                   direction="investors"),
        _mock_task(id=2, title="Schedule design review (other)",
                   direction="other"),
        _mock_task(id=3, title="Beta sign-off", direction="beta"),
    ]
    session = _stub_session_with_tasks(tasks)
    out = _build_todo_section(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="ff_test",
        filter_by_direction=True,  # explicit для clarity
    )
    # 2 important должны попасть, "other" — нет
    assert "Update data room" in out
    assert "Beta sign-off" in out
    assert "Schedule design review" not in out


def test_fr_cr_05_199_backwards_compat_default_filter_only() -> None:
    """Без explicit filter param — default = True (filter применяется),
    backwards-compat для legacy callers."""
    from app.fireflies.pipeline import _build_todo_section
    from app.models import TaskSourceKind

    tasks = [
        _mock_task(id=1, title="Investors action", direction="investors"),
        _mock_task(id=2, title="Other action", direction="other"),
    ]
    session = _stub_session_with_tasks(tasks)
    # БЕЗ kwarg — default behavior
    out = _build_todo_section(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="ff_test",
    )
    assert "Investors action" in out
    assert "Other action" not in out
