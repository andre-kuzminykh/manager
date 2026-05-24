"""Row validation for the Task entity (spec §20.4, §20.10, UC-TASK-*).

Validation is deterministic and DB-free: the assignee resolver is injected
(`resolve_assignee`) so this module stays unit-testable. The engine wires the
real `team_members` lookup.

Invalid rows produce row-addressable errors and MUST NOT mutate current state
(FR-GS-026/027, PR-005). Warnings are non-blocking.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.sheet_sync.config import (
    CATEGORY_NORMALIZED,
    HASHABLE_FIELDS,
    PRIORITY_NORMALIZED,
    STATUS_NORMALIZED,
)
from app.sheet_sync.identity import task_signature
from app.sheet_sync.normalize import DateTimeError, combine_datetime, norm_str, stable_hash

# resolve_assignee(display_name) -> (status, assignee_id)
#   status ∈ {"ok", "empty", "unknown", "ambiguous"}
AssigneeResolver = Callable[[str], "tuple[str, int | None]"]

_ALL_FIELDS = (
    "title", "description", "responsible", "status", "priority", "category",
    "start_date", "start_time", "deadline_date", "deadline_time",
    "completed_date", "completed_time", "comments",
)


@dataclass
class RowResult:
    is_empty: bool
    payload: dict | None
    payload_hash: str | None
    signature: str | None
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.is_empty and self.payload is not None and not self.errors


def _err(t: str, msg: str) -> dict:
    return {"error_type": t, "message": msg}


def validate_task_row(
    row: dict[str, str],
    *,
    resolve_assignee: AssigneeResolver,
    tz: str = "UTC",
) -> RowResult:
    """Validate one mapped task row (field → raw cell value)."""
    f = {k: norm_str(row.get(k)) for k in _ALL_FIELDS}

    if all(v is None for v in f.values()):
        return RowResult(is_empty=True, payload=None, payload_hash=None, signature=None)

    errors: list[dict] = []
    warnings: list[dict] = []

    # --- required + dropdown fields -----------------------------------------
    title = f["title"]
    if not title:
        errors.append(_err("missing_title", "Task title is required"))

    priority = None
    if not f["priority"]:
        errors.append(_err("missing_priority", "Priority is required"))
    else:
        priority = PRIORITY_NORMALIZED.get(f["priority"].lower())
        if priority is None:
            errors.append(_err(
                "invalid_priority",
                f"Priority must be one of High/Medium/Low, got {f['priority']!r}",
            ))

    status = None
    if not f["status"]:
        errors.append(_err("missing_status", "Status is required"))
    else:
        status = STATUS_NORMALIZED.get(f["status"].lower())
        if status is None:
            errors.append(_err(
                "invalid_status",
                f"Status {f['status']!r} is not an allowed status",
            ))

    # --- responsible (optional, but if present must resolve) ----------------
    assignee_id: int | None = None
    assignee_name: str | None = None
    if f["responsible"]:
        st, aid = resolve_assignee(f["responsible"])
        if st == "unknown":
            errors.append(_err(
                "unknown_responsible",
                f"Responsible {f['responsible']!r} is not in the team",
            ))
        elif st == "ambiguous":
            errors.append(_err(
                "ambiguous_responsible",
                f"Responsible {f['responsible']!r} matches multiple team members",
            ))
        else:
            assignee_id = aid
            assignee_name = f["responsible"]

    # --- date/time fields ---------------------------------------------------
    def _combine(dkey: str, tkey: str, etype: str) -> str | None:
        try:
            return combine_datetime(f[dkey], f[tkey], tz=tz)
        except DateTimeError:
            errors.append(_err(etype, f"{dkey}: time provided without a date"))
        except ValueError as e:
            errors.append(_err(etype, f"{dkey}: {e}"))
        return None

    start_at = _combine("start_date", "start_time", "invalid_start_datetime")
    deadline_at = _combine("deadline_date", "deadline_time", "invalid_deadline_datetime")
    completed_at = _combine("completed_date", "completed_time", "invalid_completed_datetime")

    # --- non-blocking warnings (spec §20.4) ---------------------------------
    if status == "done" and not completed_at:
        warnings.append(_err("done_without_completion", "Status is Done but no completion date/time"))
    if start_at and deadline_at and deadline_at < start_at:
        warnings.append(_err("deadline_before_start", "Deadline is earlier than start"))

    if errors:
        return RowResult(
            is_empty=False, payload=None, payload_hash=None, signature=None,
            errors=errors, warnings=warnings,
        )

    # Category → strategic direction key (or 'other'); empty stays None.
    category = (
        CATEGORY_NORMALIZED.get(f["category"].lower(), "other") if f["category"] else None
    )

    payload = {
        "title": title,
        "description": f["description"],
        "assignee_id": assignee_id,
        "assignee_name": assignee_name,
        "status": status,
        "priority": priority,
        "category": category,
        "start_at": start_at,
        "deadline_at": deadline_at,
        "completed_at": completed_at,
        "comments": f["comments"],
    }
    return RowResult(
        is_empty=False,
        payload=payload,
        payload_hash=stable_hash(payload, fields=HASHABLE_FIELDS),
        signature=task_signature(payload),
        errors=[],
        warnings=warnings,
    )


__all__ = ["RowResult", "AssigneeResolver", "validate_task_row"]
