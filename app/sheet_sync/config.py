"""FR-TASK-* — Google Sheets Versioned Sync: Task entity configuration.

Stdlib-only on purpose: the deterministic core (config / normalize / validation
/ identity) MUST be importable and unit-testable without DB or network. The
sync engine, Sheets I/O, runner and seed wire these into the rest of the app.

This is an ISOLATED feature (separate `gs_` tables, separate runner container,
`SHEET_SYNC_ENABLED` flag). It never touches the existing `tasks` /
`action_drafts` tables.
"""
from __future__ import annotations

# Visible Sheet columns for the Task entity (spec §20.2 display order).
# (header, internal_field). Internal id is NEVER a column (PR-001).
TASK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Task title", "title"),
    ("Description", "description"),
    ("Responsible", "responsible"),
    ("Status", "status"),
    ("Priority", "priority"),
    ("Category", "category"),
    ("Start date", "start_date"),
    ("Start time", "start_time"),
    ("Deadline date", "deadline_date"),
    ("Deadline time", "deadline_time"),
    ("Completion date", "completed_date"),
    ("Completion time", "completed_time"),
    ("Comments", "comments"),
    ("Added at", "added_at"),
    # FR-CR-05-205 — read-only / export-only context columns. The export
    # (ops/sheet_sync_export_tasks.py) fills these from the task's source;
    # the (currently disabled) bidirectional sync must NOT pull them back
    # into Task — they are display-only.
    ("Источник", "source"),
    ("Ссылка", "source_link"),
)

TASK_HEADERS: tuple[str, ...] = tuple(h for h, _ in TASK_COLUMNS)
FIELD_BY_HEADER: dict[str, str] = {h: f for h, f in TASK_COLUMNS}

# Required business fields for a valid task row (spec US-TASK-001).
REQUIRED_FIELDS: frozenset[str] = frozenset({"title", "status", "priority"})

# Priority dropdown (spec §20.3): visible → normalized.
PRIORITY_NORMALIZED: dict[str, str] = {
    "high": "high",
    "medium": "medium",
    "low": "low",
}
PRIORITY_DISPLAY: tuple[str, ...] = ("High", "Medium", "Low")

# Status dropdown (spec §20.3): accepts display or normalized → normalized.
STATUS_NORMALIZED: dict[str, str] = {
    "backlog": "backlog",
    "to do": "todo",
    "todo": "todo",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "blocked": "blocked",
    "done": "done",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
STATUS_DISPLAY: tuple[str, ...] = (
    "Backlog", "To Do", "In Progress", "Blocked", "Done", "Cancelled",
)
# Reverse: normalized status/priority key → Sheet display (DB → Sheet feed).
STATUS_DISPLAY_BY_KEY: dict[str, str] = {
    "backlog": "Backlog", "todo": "To Do", "in_progress": "In Progress",
    "blocked": "Blocked", "done": "Done", "cancelled": "Cancelled",
}
PRIORITY_DISPLAY_BY_KEY: dict[str, str] = {
    "low": "Low", "medium": "Medium", "high": "High", "urgent": "High",
}

# Category dropdown — operator 2026-05-24: «Category из стратегических фильтров
# либо other». Mirrors task_direction.DIRECTIONS_IMPORTANT (+ other). Kept as a
# literal here to keep this module stdlib-only/testable; must stay in sync.
CATEGORY_NORMALIZED: dict[str, str] = {
    "investors": "investors",
    "budget": "budget",
    "design": "design",
    "beta": "beta",
    "deliverables": "deliverables",
    "other": "other",
}
CATEGORY_DISPLAY: tuple[str, ...] = (
    "Investors", "Budget", "Design", "Beta", "Deliverables", "Other",
)

# Fields included in the payload HASH (change detection). assignee_name and
# any display-only / volatile field are excluded so renames of the same
# resolved person or cosmetic edits don't create spurious states (NFR-GS-008).
HASHABLE_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "assignee_id",
    "status",
    "priority",
    "category",
    "start_at",
    "deadline_at",
    "completed_at",
    "comments",
)

# Fields used for the fallback task_signature when row lineage is lost
# (spec §20.5). Deliberately small + identity-bearing.
SIGNATURE_FIELDS: tuple[str, ...] = (
    "title",
    "assignee_id",
    "start_at",
    "deadline_at",
    "category",
)

__all__ = [
    "TASK_COLUMNS",
    "TASK_HEADERS",
    "FIELD_BY_HEADER",
    "REQUIRED_FIELDS",
    "PRIORITY_NORMALIZED",
    "PRIORITY_DISPLAY",
    "STATUS_NORMALIZED",
    "STATUS_DISPLAY",
    "CATEGORY_DISPLAY",
    "CATEGORY_NORMALIZED",
    "HASHABLE_FIELDS",
    "SIGNATURE_FIELDS",
]
