"""FR-CR-05-165 — LLM compose step (kept separate from `service`
so unit tests of the pure logic don't drag in OpenAI).

The runner calls ``compose_agenda(candidate, llm_backend, model)``
once per candidate. Errors are caught and logged — the runner
must NOT crash the tick loop on a single bad event.

LLM backend interface: we accept anything that either
  (a) exposes ``complete_json(messages, model=...)`` returning a
      ``dict``, OR
  (b) exposes a ``._client`` attribute that's an OpenAI SDK
      client (we call ``chat.completions.create`` with
      ``response_format=json_object`` and parse ``message.content``
      ourselves).

(b) is the path the production OpenAIBackend supports — it doesn't
have a ``complete_json`` method, just a ``_client`` reference.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.agenda.service import AgendaCandidate
from app.logging_setup import get_logger

log = get_logger(__name__)


_AGENDA_SYSTEM_PROMPT = """Ты — операционный ассистент CEO. Составляешь короткую повестку к повторяющейся встрече в стиле post-meeting summary оператора. БЕЗ ЭМОДЗИ.

Структура output (JSON):

- previous_recap: список 2-4 коротких предложений. Это будет отрисовано как сплошной абзац «На прошлой встрече: <предложение>. <предложение>.» — формулируй каждый item как отдельное предложение, БЕЗ маркеров, БЕЗ нумерации, БЕЗ эмодзи. Передавай суть последней встречи + ключевые договорённости. Если prior_recordings пусто — верни пустой список.

- tasks_checklist: ВСЕ open_tasks как объекты {task_id, title, description, status, owner, due}. Сохрани task_id, owner, due, status ровно как пришло во input — passthrough. title — обычно совпадает с input, можно слегка переформулировать (без потери смысла) если нужно для читаемости. description — короткое (≤220 символов) объяснение что нужно сделать. Если в input description пусто — выведи пустую строку, ничего не выдумывай.

- open_questions: 0-2 дополнительных пункта повестки (не дубликаты задач) — например «утром начать outreach», «уточнить timeline pre-seed». Только если они НЕ покрыты в tasks_checklist. Без эмодзи, без маркеров.

- doc_body_md: markdown body для Google Doc. Структура:
  ```
  ## На прошлой встрече
  <recap абзацем>
  ## К обсуждению
  | # | Задача | Описание | Статус | Owner | Due |
  |---|--------|----------|--------|-------|-----|
  | 1 | ... | ... | ... | ... | DD.MM.YYYY |
  ```
  (без эмодзи)

ВЕРНИ только JSON. Без префиксов / без комментариев / без ```json``` блоков."""


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


def _attendees_to_csv(items: list[Any]) -> str:
    """Calendar API gives attendees as
    ``[{email, displayName, ...}, ...]``; Apps Script proxy emits
    plain strings. Accept both, prefer displayName, fall back to
    email, drop garbage."""
    out: list[str] = []
    for it in items or []:
        if isinstance(it, str):
            s = it.strip()
            if s:
                out.append(s)
        elif isinstance(it, dict):
            name = (
                it.get("displayName")
                or it.get("name")
                or it.get("email")
                or ""
            ).strip()
            if name:
                out.append(name)
    return ", ".join(out) if out else "—"


def _build_user_prompt(candidate: AgendaCandidate) -> str:
    attendees_csv = _attendees_to_csv(candidate.attendees)
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


def _call_llm_json(
    llm_backend: Any, messages: list[dict[str, str]], *, model: str
) -> dict[str, Any] | None:
    """Try `complete_json` first (test fakes use it); fall back
    to the OpenAI SDK on `backend._client`."""
    if hasattr(llm_backend, "complete_json"):
        return llm_backend.complete_json(messages, model=model)
    client = getattr(llm_backend, "_client", None)
    if client is None:
        raise RuntimeError(
            "llm_backend has neither complete_json nor _client"
        )
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    content = ""
    if resp and resp.choices:
        content = (resp.choices[0].message.content or "").strip()
    if not content:
        return None
    return json.loads(content)


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
        raw = _call_llm_json(llm_backend, messages, model=model)
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
