"""FR-SS — pure reconcile planner (SPEC_SHEET_SYNC_v0.1 §4–§7).
Covers: create / edit / delete / push / append, idempotency, sheet-wins
conflict, mass-delete guard, empty-sheet abort. No DB / no Google.
"""
from __future__ import annotations

from app.sheet_sync.bridge import RowView, payload_hash, plan_reconcile


def _row(uuid, n, **vals) -> RowView:
    base = {"title": "", "status": "", "priority": "", "description": "",
            "owner": "", "due_date": "", "category": ""}
    base.update(vals)
    return RowView(row_uuid=uuid, row_number=n, values=base)


def test_new_row_creates() -> None:
    rows = [_row(None, 2, title="Новая задача", status="todo")]
    plan = plan_reconcile(rows, task_payloads={}, links={})
    assert len(plan.creates) == 1
    assert plan.creates[0].values["title"] == "Новая задача"
    assert not plan.edits and not plan.deletes


def test_new_row_no_title_is_error() -> None:
    rows = [_row(None, 2, status="todo")]  # no title
    plan = plan_reconcile(rows, task_payloads={}, links={})
    assert plan.creates == []
    assert plan.errors and plan.errors[0][1] == "no_title"


def test_human_edit_pulls_to_db() -> None:
    r = _row("u1", 2, title="T", status="done")
    links = {"u1": (1, "stale-hash")}                 # last_hash != current -> edited
    task_payloads = {1: {"title": "T", "status": "todo"}}
    plan = plan_reconcile([r], task_payloads, links)
    assert plan.edits == [(1, {"status": "done"})]
    assert plan.pushes == []                          # title unchanged


def test_no_change_is_idempotent() -> None:
    r = _row("u1", 2, title="T", status="todo")
    links = {"u1": (1, payload_hash(r.values))}        # last_hash == current -> not edited
    task_payloads = {1: {"title": "T", "status": "todo"}}
    plan = plan_reconcile([r], task_payloads, links)
    assert plan.edits == [] and plan.pushes == [] and plan.deletes == []


def test_db_change_pushes_to_sheet() -> None:
    # human did NOT edit (hash matches), but DB differs -> push DB value to sheet
    r = _row("u1", 2, title="T", status="todo")
    links = {"u1": (1, payload_hash(r.values))}
    task_payloads = {1: {"title": "T", "status": "done"}}   # DB moved to done (chat/meeting)
    plan = plan_reconcile([r], task_payloads, links)
    assert plan.edits == []
    assert plan.pushes == [(1, {"status": "done"})]


def test_sheet_wins_on_simultaneous_edit() -> None:
    # both changed `status`; human edit (hash differs) wins, no push
    r = _row("u1", 2, title="T", status="in_progress")
    links = {"u1": (1, "stale")}
    task_payloads = {1: {"title": "T", "status": "done"}}
    plan = plan_reconcile([r], task_payloads, links)
    assert plan.edits == [(1, {"status": "in_progress"})]
    assert plan.pushes == []


def test_delete_when_row_absent() -> None:
    rows = [_row(f"u{i}", i, title=f"T{i}") for i in range(1, 6)]   # u1..u5 present
    links = {f"u{i}": (i, payload_hash(rows[0].values)) for i in range(1, 7)}  # u1..u6 linked
    task_payloads = {i: {"title": f"T{i}"} for i in range(1, 7)}
    plan = plan_reconcile(rows, task_payloads, links)            # u6 vanished (1/6 ≈ 16% < 20%)
    assert plan.deletes == [6]
    assert plan.abort is None


def test_mass_delete_guard_aborts() -> None:
    rows = [_row("u1", 1, title="T1")]                # only 1 of 3 present
    links = {"u1": (1, "h"), "u2": (2, "h"), "u3": (3, "h")}
    task_payloads = {1: {"title": "T1"}, 2: {"title": "T2"}, 3: {"title": "T3"}}
    plan = plan_reconcile(rows, task_payloads, links)
    assert plan.abort and plan.abort.startswith("mass_delete_guard")
    assert plan.deletes == []


def test_empty_sheet_with_links_aborts() -> None:
    plan = plan_reconcile([], task_payloads={1: {"title": "T"}}, links={"u1": (1, "h")})
    assert plan.abort == "empty_sheet_with_links"


def test_append_live_task_without_row() -> None:
    plan = plan_reconcile([], task_payloads={5: {"title": "T5"}}, links={})
    assert plan.appends == [5]


__all__: list[str] = []
