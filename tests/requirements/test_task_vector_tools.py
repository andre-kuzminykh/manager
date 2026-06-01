"""FR-TV — Task Vector CEO-brain tools contract (see docs/SPEC_TASK_VECTOR_v0.1.md).

P0 skeleton: encodes the search / status-update tool contract and SKIPS until
P2 lands app.ceo_brain.task_tools. Exercised with FAKE search + FAKE session
(no network, no DB) once the module exists.
"""
from __future__ import annotations

import json

import pytest

tt = pytest.importorskip("app.ceo_brain.task_tools")


def _executors(**kw):
    """Build the tool executor map with injected fakes. Expected factory:
    build_task_executors(session_factory, settings, *, search_fn=None)."""
    return tt.build_task_executors(**kw)


# T-FR-TV-070-a — schemas + executor map present and well-formed (6 tools)
def test_schemas_and_executors_present():
    names = {s["name"] for s in tt.TASK_TOOL_SCHEMAS}
    assert {"search_tasks", "get_task", "resolve_person",
            "update_task_status", "update_task_due", "update_task_owner"} <= names
    for s in tt.TASK_TOOL_SCHEMAS:
        assert s.get("description") and s.get("input_schema")


# T-FR-TV-046-a — every update tool returns {field, from, to} for generic undo
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_updates_return_field_from_to_for_undo():
    raise pytest.skip.Exception


# T-FR-TV-047-a — due update: valid ISO sets+syncs; invalid date → no write
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_update_due_valid_and_invalid():
    raise pytest.skip.Exception


# T-FR-TV-048-a — owner update: sets owner + returns {field:'owner',...}
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_update_owner_sets_and_reports():
    raise pytest.skip.Exception


# T-FR-TV-024-a — resolve_person ranks the team; [] below τ_low; ambiguity flagged
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_resolve_person_over_team():
    raise pytest.skip.Exception


# T-FR-TV-... — verb→status map is a reviewable constant
def test_verb_status_map_constant():
    vsm = getattr(tt, "VERB_STATUS_MAP", None)
    if vsm is None:
        pytest.skip("VERB_STATUS_MAP not implemented (P2)")
    # done-verbs and in_progress-verbs present, values are valid statuses
    assert set(vsm.values()) <= {"backlog", "todo", "in_progress", "done"}


# T-FR-TV-020-a — search returns LIVE fields, ordered by score, no fabrication
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_search_tasks_returns_live_fields(monkeypatch):
    # contract: search_tasks(query,k) → JSON list of
    # {task_id,title,status,owner_display_name,due_date,score}
    raise pytest.skip.Exception  # placeholder until P2 wires fakes


# T-FR-TV-041-a — unknown task id → not_found, NO write
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_update_unknown_id_no_write():
    raise pytest.skip.Exception


# T-FR-TV-042-a — invalid status / disallowed transition → no write
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_update_invalid_transition_no_write():
    raise pytest.skip.Exception


# T-FR-TV-044-a — update to current status → no-op success, no dup history
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_update_idempotent_noop():
    raise pytest.skip.Exception


# T-FR-TV-060-a — non-allow-listed actor refused + audited
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_update_forbidden_for_unlisted_actor():
    raise pytest.skip.Exception


# T-SC-TV-10 / NFR-TV-005 — vector down → {error}, never raises
@pytest.mark.skipif(not hasattr(tt, "build_task_executors"),
                    reason="build_task_executors not implemented (P2)")
def test_search_never_raises_on_backend_failure():
    raise pytest.skip.Exception
