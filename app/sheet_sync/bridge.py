"""FR-SS — bidirectional Sheet<->DB bridge: pure reconcile PLANNING core
(SPEC_SHEET_SYNC_v0.1 §4–§7). No DB, no Google I/O here — given parsed sheet
rows + DB task payloads + the row_uuid<->task_id links, compute what to do.
B1/B2 wrap this with the actual Sheets client and DB writes.

Determinism + no side effects make this fully unit-testable without prod.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Editable fields the human may change in the Sheet -> pulled to DB.
EDITABLE_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "status",
    "priority",
    "owner",
    "due_date",
    "category",
)


@dataclass
class RowView:
    """A parsed sheet row: identity (uuid from DeveloperMetadata) + values
    already mapped from headers to canonical field names + normalized."""

    row_uuid: str | None
    row_number: int
    values: dict[str, str]


@dataclass
class ReconcilePlan:
    creates: list[RowView] = field(default_factory=list)            # new row -> create task
    edits: list[tuple[int, dict[str, str]]] = field(default_factory=list)   # (task_id, sheet field changes)
    deletes: list[int] = field(default_factory=list)               # task_ids to soft-delete
    pushes: list[tuple[int, dict[str, str]]] = field(default_factory=list)  # (task_id, cells to write to sheet)
    appends: list[int] = field(default_factory=list)               # task_ids with no row yet
    errors: list[tuple[RowView, str]] = field(default_factory=list)
    abort: str | None = None


def payload_hash(values: dict[str, str]) -> str:
    """Stable hash over the editable fields only (order-independent)."""
    norm = {k: (values.get(k) or "") for k in EDITABLE_FIELDS}
    blob = json.dumps(norm, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def plan_reconcile(
    rows: list[RowView],
    task_payloads: dict[int, dict[str, str]],
    links: dict[str, tuple[int, str | None]],
    *,
    max_delete_pct: float = 0.2,
) -> ReconcilePlan:
    """Pure reconcile.

    Args:
      rows: parsed sheet rows (values normalized to EDITABLE_FIELDS).
      task_payloads: {task_id: {field: value}} — current DB editable values (live, non-deleted).
      links: {row_uuid: (task_id, last_payload_hash)} — known row<->task bridge.
      max_delete_pct: abort if more than this fraction of linked rows vanished
        at once (FR-SS-SAFE-2 mass-delete guard).

    Order = PULL (creates/edits/deletes) then PUSH (pushes/appends), so a
    human edit (edit) wins over a DB-side diff on the same field (FR-SS-CONF-1).
    """
    plan = ReconcilePlan()

    # FR-SS-SAFE-1: empty sheet while we have links = a broken/empty read,
    # never interpret as "delete everything".
    if not rows and links:
        plan.abort = "empty_sheet_with_links"
        return plan

    present_uuids: set[str] = set()

    for r in rows:
        # --- FR-SS-NEW: row without uuid -> create ---
        if not r.row_uuid:
            if not (r.values.get("title") or "").strip():
                plan.errors.append((r, "no_title"))
            else:
                plan.creates.append(r)
            continue

        present_uuids.add(r.row_uuid)
        link = links.get(r.row_uuid)
        if link is None:
            plan.errors.append((r, "uuid_not_linked"))
            continue
        task_id, last_hash = link
        db_pl = task_payloads.get(task_id)
        if db_pl is None:
            # linked task is deleted/absent in DB -> DB->Sheet side tombstones it
            continue

        cur_hash = payload_hash(r.values)
        human_edited = cur_hash != last_hash  # FR-SS-CONF-3: cell really changed

        # --- FR-SS-EDIT: sheet -> DB on fields the human changed & that differ ---
        edit: dict[str, str] = {}
        if human_edited:
            for f in EDITABLE_FIELDS:
                rv = r.values.get(f, "")
                if rv != db_pl.get(f, ""):
                    edit[f] = rv
        if edit:
            plan.edits.append((task_id, edit))

        # --- FR-SS-PUSH: DB -> sheet only where they differ AND the human
        #     didn't just edit that field (sheet-wins, FR-SS-CONF-1) ---
        push: dict[str, str] = {}
        for f in EDITABLE_FIELDS:
            dv = db_pl.get(f, "")
            if dv != r.values.get(f, "") and f not in edit:
                push[f] = dv
        if push:
            plan.pushes.append((task_id, push))

    # --- FR-SS-DEL: linked uuid no longer present -> soft-delete ---
    absent = [(u, tid) for u, (tid, _) in links.items() if u not in present_uuids]
    if links and (len(absent) / max(1, len(links))) > max_delete_pct:
        plan.abort = f"mass_delete_guard:{len(absent)}/{len(links)}"
        return plan
    plan.deletes = [tid for _, tid in absent if tid in task_payloads]

    # --- FR-SS-PUSH-2: live DB tasks with no row yet -> append ---
    linked_task_ids = {tid for _, (tid, _) in links.items()}
    plan.appends = [tid for tid in task_payloads if tid not in linked_task_ids]

    return plan


__all__ = ["RowView", "ReconcilePlan", "EDITABLE_FIELDS", "payload_hash", "plan_reconcile"]
