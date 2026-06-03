"""FR-CR-05-242 — ONE canonical form per entity across detailed summary,
short summary and tasks.

The detailed summary is the single source of canonical entity forms. The
short summary is generated FROM it (FR-CR-05-241, no second canonicalise),
and task titles/descriptions are forced to the SAME forms via the detailed
summary's rewrite map. This kills the «Siva» (summary) vs «Ceva Logistics»
(task) desync seen on «Алина, Ирина» 2026-06-03.

These tests pin the mechanism the pipeline relies on: applying the
detailed's `{found: canonical}` map to task text yields the canonical
forms, longest-first and cascade-safe.
"""
from __future__ import annotations

from app.services.counterparty_match import canonicalize_text


def test_detail_map_forces_canonical_form_in_task() -> None:
    detail_map = {"Сива": "Ceva Logistics", "Тесер": "Tether"}
    title = "Проверить статус Сива и напомнить Тесер"
    out = canonicalize_text(title, detail_map)
    assert out == "Проверить статус Ceva Logistics и напомнить Tether"


def test_consistency_summary_and_task_match() -> None:
    # Same map applied to the detailed sentence and a task → identical form.
    detail_map = {"Miraya": "Mirae"}
    detailed = "По Miraya ждём ответ."
    task = "Miraya - дождаться ответа"
    assert canonicalize_text(detailed, detail_map) == "По Mirae ждём ответ."
    assert canonicalize_text(task, detail_map) == "Mirae - дождаться ответа"


def test_longest_first_no_partial_eat() -> None:
    # «Mirae Asset» must not be half-replaced by «Mirae».
    detail_map = {"Mirae": "Mirae", "Mirae Asset": "Mirae Asset Management"}
    out = canonicalize_text("Контакт Mirae Asset", detail_map)
    assert out == "Контакт Mirae Asset Management"


def test_empty_map_is_noop() -> None:
    assert canonicalize_text("Siva и Tether", {}) == "Siva и Tether"
    assert canonicalize_text(None, {"a": "b"}) is None


def test_identity_pair_skipped() -> None:
    # found == canonical must not cascade.
    out = canonicalize_text("Tether ok", {"Tether": "Tether"})
    assert out == "Tether ok"


__all__: list[str] = []
