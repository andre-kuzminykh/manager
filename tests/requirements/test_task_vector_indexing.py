"""FR-TV — Task Vector indexing contract (see docs/SPEC_TASK_VECTOR_v0.1.md).

P0 skeleton: these encode the indexing contract and SKIP until P1 lands
`build_text_repr_task` (+ task-aware refresh) in app.services.entity_embeddings.
Pure-function tests run without a DB; pgvector-gated cases skip without
TEST_PG_VECTOR_URL (house style — see test_entity_embeddings_service.py).
"""
from __future__ import annotations

import datetime as dt

import pytest

ee = pytest.importorskip("app.services.entity_embeddings")
_build = getattr(ee, "build_text_repr_task", None)
if _build is None:  # P1 not landed yet
    pytest.skip("build_text_repr_task not implemented (FR-TV-010, P1)",
                allow_module_level=True)


# T-FR-TV-010-a — content-only, deterministic text_repr
def test_text_repr_is_content_only_and_deterministic():
    a = _build(title="Отправить договор", description="юр-проверка",
               owner_display_name="Семён", status="todo",
               due_date=dt.date(2026, 6, 10), category="legal")
    b = _build(title="Отправить договор", description="юр-проверка",
               owner_display_name="Семён", status="todo",
               due_date=dt.date(2026, 6, 10), category="legal")
    assert a == b and a.strip()                     # deterministic, non-empty
    assert "Отправить договор" in a and "Семён" in a  # content present


# T-FR-TV-014-a — a status-only change must NOT change the embedded text
def test_status_only_change_does_not_change_text_repr():
    base = dict(title="Отправить договор", description="юр-проверка",
                owner_display_name="Семён", due_date=dt.date(2026, 6, 10),
                category="legal")
    todo = _build(status="todo", **base)
    done = _build(status="done", **base)
    assert ee.text_repr_hash(todo) == ee.text_repr_hash(done)


# T-FR-TV-010-b — empty content is not embeddable
def test_empty_content_not_embeddable():
    out = _build(title="", description=None, owner_display_name=None,
                 status="todo", due_date=None, category=None)
    assert not (out or "").strip()


# T-FR-TV-013a-a — REGRESSION: a `--since` window MUST NOT prune.
# A windowed scan is a deliberate subset; pruning would delete every row
# outside the window (observed: --since once pruned 3361/3655 rows).
# We assert the prune flag handed to _apply_embeddings is derived from
# task_created_since, without needing a real pgvector DB.
def test_since_window_disables_prune(monkeypatch):
    captured = {}

    monkeypatch.setattr(ee, "collect_entity_texts",
                        lambda *a, **k: [("task", "1", "repr")])

    def _fake_apply(session, rows, *, embed_fn, kinds, model, batch_size,
                    prune=True):
        captured["prune"] = prune
        return {"scanned": len(rows), "embedded": 0, "skipped": len(rows),
                "pruned": 0}

    monkeypatch.setattr(ee, "_apply_embeddings", _fake_apply)

    # full scan (no window) → prune ON (reconciles deletions)
    ee.refresh_embeddings_cross_db(
        object(), object(), embed_fn=lambda t: [[0.0]], kinds=["task"],
        task_created_since=None)
    assert captured["prune"] is True

    # windowed scan (--since) → prune OFF (never deletes outside the window)
    ee.refresh_embeddings_cross_db(
        object(), object(), embed_fn=lambda t: [[0.0]], kinds=["task"],
        task_created_since=dt.date(2026, 6, 1))
    assert captured["prune"] is False
