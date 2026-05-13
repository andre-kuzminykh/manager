"""FR-CR-05-165 — LLM compose step (kept separate from `service`
so unit tests of the pure logic don't drag in OpenAI).

The runner calls ``compose_agenda(candidate, llm_backend, model)``
once per candidate. Errors are caught and logged — the runner
must NOT crash the tick loop on a single bad event.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.agenda.service import AgendaCandidate
from app.logging_setup import get_logger

log = get_logger(__name__)


_AGENDA_SYSTEM_PROMPT = """Ты — операционный ассистент CEO. Составляешь короткую повестку к повторяющейся встрече. Стиль операторский: коротко, по делу, без воды и эпитетов. Никаких эмодзи в JSON (slack отрисует сам).

Правила:
- previous_recap — 3-5 буллетов: что обсуждали и до чего договорились ИЗ последней встречи. Не пересказывай transcript целиком. Если prior_recordings пусто — верни пустой список.
- tasks_checklist — все open_tasks как чекбоксы, без сокращений. Сохрани task_id, owner, due ровно как пришло во input. Не переписывай статусы — это просто passthrough.
- open_questions — 2-4 буллета: что ОБЯЗАТЕЛЬНО обсудить сегодня исходя из open_tasks (особенно blocked / overdue) и предыдущих summary. НЕ дублируй чеклист задач.
- doc_body_md — markdown body для Google Doc. Три секции: «## Из прошлого раза», «## Задачи», «## К обсуждению». В секции «Задачи» — table Title / Status / Owner / Due.

ВЕРНИ только JSON, никаких префиксов, комментариев, ```json``` блоков."""


class LLMBackend(Protocol):
    """The subset of `app.intent.llm_backends` we lean on. Any
    object exposing `complete_json(messages, model)` works — keeps
    the agenda module decoupled from the intent pipeline."""

    def complete_json(
        self, messages: list[dict[str, str]], *, model: str
    ) -> dict[str, Any]: ...


@dataclass
class AgendaOutput:
    previous_recap: list[str]
    tasks_checklist: list[dict[str, Any]]
    open_questions: list[str]
    doc_body_md: str


def _build_user_prompt(candidate: AgendaCandidate) -> str:
    attendees_csv = ", ".join(candidate.attendees) if candidate.attendees else "—"
    prior_json = json.dumps(
        candidate.prior_recordings, ensure_ascii=False, indent=2
    )
    tasks_json = json.dumps(
        candidate.open_tasks, ensure_ascii=False, indent=2
    )
    return (
        f"Meeting: {candidate.title}\n"
        f"Scheduled: {candidate.scheduled_start_at.isoformat()}\n"
        f"Attendees: {attendees_csv}\n"
        f"Calendar description: {(candidate.description or '').strip()}\n\n"
        f"Prior recordings ({len(candidate.prior_recordings)} matches):\n"
        f"{prior_json}\n\n"
        f"Open tasks ({len(candidate.open_tasks)}):\n"
        f"{tasks_json}"
    )


def _coerce_output(payload: dict[str, Any]) -> AgendaOutput:
    """Be tolerant of LLM nits — empty fields default to []/«»,
    but the schema must mention all keys."""
    return AgendaOutput(
        previous_recap=list(payload.get("previous_recap") or [])[:5],
        tasks_checklist=list(payload.get("tasks_checklist") or []),
        open_questions=list(payload.get("open_questions") or [])[:4],
        doc_body_md=str(payload.get("doc_body_md") or "").strip(),
    )


def compose_agenda(
    candidate: AgendaCandidate,
    *,
    llm_backend: LLMBackend,
    model: str,
) -> AgendaOutput | None:
    """Run the LLM compose step. Returns None on any failure —
    runner logs and skips the event."""
    messages = [
        {"role": "system", "content": _AGENDA_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(candidate)},
    ]
    try:
        raw = llm_backend.complete_json(messages, model=model)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "agenda_llm_call_failed",
            calendar_event_id=candidate.calendar_event_id,
            error=str(e),
        )
        return None
    if not isinstance(raw, dict):
        log.warning(
            "agenda_llm_invalid_json",
            calendar_event_id=candidate.calendar_event_id,
            type=type(raw).__name__,
        )
        return None
    try:
        return _coerce_output(raw)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "agenda_llm_output_coerce_failed",
            calendar_event_id=candidate.calendar_event_id,
            error=str(e),
        )
        return None


__all__ = ["AgendaOutput", "compose_agenda"]
