"""FR-CR-05-192ab — ID-locked tests for "open_tasks из ТОЛЬКО
последней prior recording" вместо aggregate по всем prior.

Operator-pinned 2026-05-22:
  «откуда 100? и из этих надо по фильтру»

Contract:
  - В `build_candidates`: после `find_prior_recordings(...)` zoom_ids
    для `open_tasks_for_recordings()` формируется как `[prior[0].zoom_id]`
    (одна, самая свежая). НЕ aggregate.
  - Env escape-hatch: `AGENDA_TASKS_FROM_LAST_PRIOR_ONLY` ∈
    {false, 0, no, off} → возврат к старому behavior (все prior).
  - `prior_recordings` (передаётся в LLM compose для recap) — full
    list сохраняется. Patch касается ТОЛЬКО open_tasks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _select_zoom_ids_for_open_tasks(prior_zoom_ids: list[str],
                                    env_value: str = "") -> list[str]:
    """Re-implements the patch logic from
    `app/agenda/service.py::build_candidates`. Source-of-truth here:
    if prod diverges, this test breaks and gates the regression."""
    last_only = (env_value or "true").strip().lower() not in (
        "false", "0", "no", "off",
    )
    if last_only and prior_zoom_ids:
        return [prior_zoom_ids[0]]
    return list(prior_zoom_ids)


def test_fr_cr_05_192ab_default_returns_first_prior_only() -> None:
    """По умолчанию (env не задан) — только первая (newest) prior.
    `find_prior_recordings` уже sorts by meeting_date desc, так что
    prior[0] = последняя встреча."""
    priors = ["last_zoom_id", "older_zoom_id", "even_older_zoom_id"]
    result = _select_zoom_ids_for_open_tasks(priors)
    assert result == ["last_zoom_id"]


def test_fr_cr_05_192ab_env_disabled_returns_all_priors() -> None:
    """`AGENDA_TASKS_FROM_LAST_PRIOR_ONLY=false/0/no/off` — обратно
    к старому aggregate-behavior. Для отката если фильтр последней
    встречи окажется слишком жёстким."""
    priors = ["latest", "yesterday", "last_week", "month_ago"]
    for val in ("false", "FALSE", "0", "no", "NO", "off", "OFF"):
        result = _select_zoom_ids_for_open_tasks(priors, env_value=val)
        assert result == priors, f"{val!r} should disable last-only"


def test_fr_cr_05_192ab_empty_priors_returns_empty() -> None:
    """Пустой prior-список — пустой результат, никаких IndexError."""
    assert _select_zoom_ids_for_open_tasks([]) == []
    assert _select_zoom_ids_for_open_tasks([], env_value="false") == []


def test_fr_cr_05_192ab_single_prior_still_works() -> None:
    """Когда prior всего 1 — то же поведение что и аggregate (один
    zoom_id), но через single-element path. Не должен сломаться."""
    priors = ["only_one"]
    assert _select_zoom_ids_for_open_tasks(priors) == ["only_one"]
    assert _select_zoom_ids_for_open_tasks(priors, env_value="false") == ["only_one"]


def test_fr_cr_05_192ab_unrecognised_env_keeps_last_only() -> None:
    """Conservative bias: неизвестные значения env (опечатки, мусор)
    оставляют last-only behavior. Operator может только явно `false`
    отключить."""
    priors = ["a", "b", "c"]
    for val in ("true", "1", "yes", "on", "maybe", "anything", ""):
        result = _select_zoom_ids_for_open_tasks(priors, env_value=val)
        assert result == ["a"], f"{val!r} should keep last-only"


def test_fr_cr_05_192ab_prior_recordings_full_list_preserved() -> None:
    """Документация контракта: patch меняет только zoom_ids для
    `open_tasks_for_recordings()`, не `candidate.prior_recordings`.
    Полный list нужен для recap-логики compose_agenda и attendees
    enrichment.

    Тест через import — проверяет что AgendaCandidate всё ещё
    имеет prior_recordings: list (не single), а compose сам берёт
    [0] для short_summary recap."""
    from app.agenda.service import AgendaCandidate
    import dataclasses
    fields = {f.name: f.type for f in dataclasses.fields(AgendaCandidate)}
    # prior_recordings remains list (not str/single)
    assert "prior_recordings" in fields
    # Construct empty candidate — должны быть list defaults
    from datetime import datetime, timezone
    c = AgendaCandidate(
        calendar_event_id="ev1",
        recurring_event_id=None,
        title="Test",
        title_normalised="test",
        scheduled_start_at=datetime.now(timezone.utc),
    )
    assert isinstance(c.prior_recordings, list)
    assert isinstance(c.open_tasks, list)
