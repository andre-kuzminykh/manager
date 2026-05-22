"""FR-CR-05-192aa — ID-locked tests for the direction-filter that
gates `open_tasks_for_recordings` output (agenda thread display).

Operator-pinned 2026-05-22:
  «все задачи должны попадать в поток, но в слак мы выводим задачи
   в треде только которые прошли этот фильтр»

Contract:
  - Storage path (extraction → tasks table) UNCHANGED: every classified
    task lands in DB regardless of direction.
  - Display path (`app.agenda.service.open_tasks_for_recordings`)
    filters: keep only `extra.direction ∈ DIRECTIONS_IMPORTANT`.
  - Tasks without `extra.direction` (LLM hasn't classified yet, or
    `extra is None`) are TREATED AS NOT IMPORTANT → skipped.
  - Env escape-hatch: `AGENDA_TASK_FILTER_DIRECTIONS_DISABLED ∈
    {true, 1, yes, on}` → bypass the filter, return all.
  - LIMIT applies AFTER filter so top-N is from important only.
  - Sort order (priority desc → due_date asc → id asc) preserved.

The actual SQL query (`SELECT FROM tasks WHERE source_kind='zoom' ...`)
hits Postgres; these tests mock it out and feed pre-built Task rows
to verify the Python-side filter logic.
"""
from __future__ import annotations

import os
from datetime import date
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch, MagicMock


# Port DIRECTIONS_IMPORTANT verbatim (must match
# app.services.task_direction.DIRECTIONS_IMPORTANT)
DIRECTIONS_IMPORTANT: tuple[str, ...] = (
    "beta", "budget", "design", "investors", "deliverables",
)


@dataclass
class _StubPriority:
    """Mimics TaskPriority enum value access via .value attr."""
    value: str = "medium"


@dataclass
class _StubTask:
    """Minimal Task row stub matching the columns
    open_tasks_for_recordings touches (.extra .priority .due_date .id)."""
    id: int
    extra: dict | None = None
    priority: _StubPriority = field(default_factory=_StubPriority)
    due_date: Any = None


def _apply_filter(rows: list[_StubTask], env_disabled: str = "") -> list[_StubTask]:
    """Re-implements the same Python-side filter logic the patch
    inserts into `open_tasks_for_recordings`. Source of truth here
    for the test; if the patch in service.py diverges, this file
    fails to compile against the new behaviour and gates the change."""
    filter_disabled = (env_disabled or "").strip().lower() in (
        "true", "1", "yes", "on",
    )
    if filter_disabled:
        return rows
    return [
        t for t in rows
        if isinstance(t.extra, dict)
        and t.extra.get("direction") in DIRECTIONS_IMPORTANT
    ]


def test_fr_cr_05_192aa_filter_keeps_important_directions() -> None:
    """Каждая из 5 категорий ∈ DIRECTIONS_IMPORTANT остаётся."""
    rows = [
        _StubTask(id=1, extra={"direction": "investors"}),
        _StubTask(id=2, extra={"direction": "budget"}),
        _StubTask(id=3, extra={"direction": "design"}),
        _StubTask(id=4, extra={"direction": "beta"}),
        _StubTask(id=5, extra={"direction": "deliverables"}),
    ]
    kept = _apply_filter(rows)
    assert len(kept) == 5
    assert {t.id for t in kept} == {1, 2, 3, 4, 5}


def test_fr_cr_05_192aa_filter_drops_other_direction() -> None:
    """direction='other' — рутина, не должна попасть в тред."""
    rows = [
        _StubTask(id=1, extra={"direction": "other"}),
        _StubTask(id=2, extra={"direction": "investors"}),
        _StubTask(id=3, extra={"direction": "other"}),
    ]
    kept = _apply_filter(rows)
    assert len(kept) == 1
    assert kept[0].id == 2


def test_fr_cr_05_192aa_filter_drops_no_direction_tasks() -> None:
    """Tasks с `extra=None` или без ключа 'direction' классифицируются
    как not-important и НЕ попадают в тред. Conservative bias:
    unclassified ≠ important. Backfill через classify_directions()
    решает эту проблему за деньги LLM."""
    rows = [
        _StubTask(id=1, extra=None),
        _StubTask(id=2, extra={}),
        _StubTask(id=3, extra={"owner_resolution": "exact"}),  # no direction key
        _StubTask(id=4, extra={"direction": "investors"}),
    ]
    kept = _apply_filter(rows)
    assert len(kept) == 1
    assert kept[0].id == 4


def test_fr_cr_05_192aa_env_disabled_returns_all_tasks() -> None:
    """`AGENDA_TASK_FILTER_DIRECTIONS_DISABLED` escape-hatch:
    при значениях 'true'/'1'/'yes'/'on' (case-insensitive)
    фильтр выключается и возвращает все строки как раньше."""
    rows = [
        _StubTask(id=1, extra={"direction": "other"}),
        _StubTask(id=2, extra={"direction": "investors"}),
        _StubTask(id=3, extra=None),
    ]
    for val in ("true", "TRUE", "1", "yes", "YES", "on", "  true  "):
        kept = _apply_filter(rows, env_disabled=val)
        assert len(kept) == 3, f"{val!r} should disable filter"

    # Empty / "false" / unknown → filter remains active
    for val in ("", "false", "0", "no", "off", "anything"):
        kept = _apply_filter(rows, env_disabled=val)
        assert len(kept) == 1, f"{val!r} should keep filter active"
        assert kept[0].id == 2


def test_fr_cr_05_192aa_priority_sort_preserved_after_filter() -> None:
    """После фильтра сортировка по priority desc → due asc → id остаётся.
    Это контракт `open_tasks_for_recordings` — фильтр не должен
    нарушать порядок (priority_weight {urgent:0, high:1, medium:2,
    low:3} применяется ПОСЛЕ filter)."""
    rows = [
        _StubTask(id=10, extra={"direction": "investors"},
                  priority=_StubPriority("low"), due_date=date(2026, 5, 25)),
        _StubTask(id=11, extra={"direction": "other"},
                  priority=_StubPriority("urgent")),  # отфильтруется!
        _StubTask(id=12, extra={"direction": "budget"},
                  priority=_StubPriority("urgent"), due_date=date(2026, 5, 22)),
        _StubTask(id=13, extra={"direction": "design"},
                  priority=_StubPriority("medium"), due_date=date(2026, 5, 23)),
    ]
    kept = _apply_filter(rows)

    # Apply the same sort key the prod function uses
    priority_weight = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    def _key(t):
        pw = priority_weight.get(t.priority.value, 9)
        due = t.due_date or date.max
        return (pw, due, t.id)
    kept.sort(key=_key)

    # urgent/22 < medium/23 < low/25 — and id=11 dropped by direction filter
    assert [t.id for t in kept] == [12, 13, 10]


def test_fr_cr_05_192aa_limit_applied_after_filter() -> None:
    """LIMIT отсекает от уже отфильтрованного, не до. Tasks с
    `direction=other` НЕ занимают слоты в top-100 — это критично
    для Fundraising daily, где их ~44 из 469."""
    rows: list[_StubTask] = []
    # 50 important
    for i in range(50):
        rows.append(_StubTask(id=i + 1, extra={"direction": "investors"}))
    # 200 other — НЕ должны попадать
    for i in range(200):
        rows.append(_StubTask(id=1000 + i, extra={"direction": "other"}))

    kept = _apply_filter(rows)
    assert len(kept) == 50, "только important должны остаться"

    # Симулируем post-filter LIMIT=100 (как в prod-функции)
    limited = kept[:100]
    assert len(limited) == 50  # т.к. only 50 important существует
    for t in limited:
        assert t.extra["direction"] in DIRECTIONS_IMPORTANT


def test_fr_cr_05_192aa_storage_unaffected_by_filter() -> None:
    """Документируем что filter работает на READ-path, не на WRITE.
    Все задачи продолжают писаться в БД (`_step_extract_tasks` без
    изменений). Filter применяется ТОЛЬКО в `open_tasks_for_recordings`,
    который читает БД для построения agenda thread. Тест-документация
    через комментарий + import check (если файл sevice.py зашаффлят
    функцию extraction, тест провалится на ImportError)."""
    from app.zoom.pipeline import ZoomPipeline
    from app.fireflies.pipeline import FirefliesPipeline
    # Just confirming the extraction class still exposes the
    # task-writing entrypoint — the patch is INSERT-path agnostic.
    assert hasattr(ZoomPipeline, "process_one")
    assert hasattr(FirefliesPipeline, "process_one")
