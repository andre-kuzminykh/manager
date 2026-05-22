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

- previous_recap: список 2-4 коротких предложений. Это будет отрисовано как сплошной абзац — renderer САМ добавит префикс «На прошлой встрече: » один раз. Каждый item — отдельное предложение, НЕ начинай с «На прошлой встрече» / «На предыдущей встрече» / «В прошлый раз» / любых аналогов (renderer уже это написал). Без маркеров, без нумерации, без эмодзи. Передавай суть последней встречи + ключевые договорённости целиком — не фильтруй по attendees. Если prior_recordings пусто — верни пустой список.

- open_questions: 0-2 дополнительных пункта повестки (не дубликаты задач) — что важно обсудить сегодня помимо открытых задач. Без эмодзи, без маркеров. Если в open_tasks всё уже покрыто — верни пустой список.

- tasks_checklist: ВСЕ open_tasks как объекты {task_id, title, description, status, owner, due}. Сохрани task_id, owner, due, status ровно как пришло во input — passthrough. title — обычно совпадает с input, можно слегка переформулировать (без потери смысла) если нужно для читаемости. description — короткое (≤220 символов) объяснение что нужно сделать. Если в input description пусто — выведи пустую строку, ничего не выдумывай. НЕ выкидывай задачи и НЕ переписывай owner.

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


def _strip_prior_short_summary_body(short_summary: str) -> str:
    """FR-CR-05-192u — extract the body paragraph(s) from a stored
    `short_summary`. Drops the `<a href>` title wrapper on line 1
    and the «Участники: …» line, returning only the meeting
    recap text the operator typed last time.

    The stored shape (FR-CR-05-127 + FR-CR-05-156):

        <a href="…">DD/MM - Title</a>
        ↵
        Участники: A, B, C
        ↵
        Recap body paragraph one.\\nRecap body paragraph two.
        ↵
        TODO:                ← if any tasks; optional
        N) … — Owner • DD.MM.YYYY HH:MM

    We strip the title line (everything up to first `\\n\\n`), the
    «Участники:» line, and any trailing «TODO:» block — leaving
    just the recap body. HTML entities (`&lt;` etc.) are decoded.
    """
    import html as _html
    import re as _re

    if not short_summary:
        return ""
    text = short_summary
    # Decode HTML entities the slack mrkdwn would have applied to
    # the stored form (FR-CR-05-127 wraps + html.escape's the body).
    if "<a href=" in text or "&lt;" in text or "&amp;" in text:
        # Strip the <a href="…">…</a> first-line wrapper, then unescape
        m = _re.match(r'<a href="[^"]*">([^<]+)</a>(.*)', text, _re.DOTALL)
        if m:
            # Drop the title line entirely.
            text = m.group(2)
        text = _html.unescape(text)
    # Drop the «Участники: …» line wherever it sits, plus any
    # preceding/trailing blank lines around it.
    lines = text.split("\n")
    kept: list[str] = []
    for ln in lines:
        if ln.strip().startswith("Участники:"):
            continue
        kept.append(ln)
    body = "\n".join(kept).strip()
    # Strip the trailing TODO block — operator wants the recap, not
    # last week's tasks. Match both «To-Do:» and «TODO:» markers.
    for marker in ("TODO:", "To-Do:"):
        idx = body.find("\n" + marker)
        if idx >= 0:
            body = body[:idx].rstrip()
        elif body.startswith(marker):
            body = ""
    return body.strip()


def _build_lite_doc_body_md(
    title: str,
    recap: str,
    prior_recordings: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]],
) -> str:
    """FR-CR-05-192u — deterministic markdown body for the agenda
    Google Doc. The runner / doc writer wraps this with its own
    H1 header «# DD/MM — Повестка ко встрече «<title>»» and a
    «## Подробно по прошлой встрече\\n- DD/MM — <url>» block, so
    `doc_body_md` MUST start at «## На прошлой встрече» —
    duplicating the wrapper's preamble would render two H1s in
    the same doc. The LLM compose path obeys the same contract
    via its system prompt (see `_AGENDA_SYSTEM_PROMPT` § doc_body_md).
    """
    out: list[str] = []
    out.append("## На прошлой встрече")
    out.append(recap or "(нет данных по прошлой встрече)")
    out.append("")
    out.append("## К обсуждению")
    out.append("| # | Задача | Описание | Статус | Owner | Due |")
    out.append("|---|--------|----------|--------|-------|-----|")
    for i, t in enumerate(open_tasks, start=1):
        title_cell = (t.get("title") or "").replace("|", "\\|")
        desc_cell = (t.get("description") or "").replace("|", "\\|")
        status_cell = (t.get("status") or "").replace("|", "\\|")
        owner_cell = (t.get("owner") or "").replace("|", "\\|")
        due_cell = (t.get("due") or t.get("due_date") or "").replace("|", "\\|")
        out.append(
            f"| {i} | {title_cell} | {desc_cell} | {status_cell} | "
            f"{owner_cell} | {due_cell} |"
        )
    return "\n".join(out)


def _compose_lite(candidate: AgendaCandidate) -> AgendaOutput:
    """FR-CR-05-192u — operator-pinned 2026-05-22 «никакого LLM,
    просто `На прошлой встрече:` и предыдущее саммари как есть».
    Returns AgendaOutput built deterministically from the candidate
    without ANY LLM call:

      - previous_recap: single-element list with the prior recording's
        stored short_summary body (title line + Участники line
        stripped). Slack renderer prepends «На прошлой встрече: »
        once per agenda — see app/agenda/slack_format.py.
      - open_questions: empty list (operator: «остальное оставим
        как есть» — agenda doesn't manufacture discussion items).
      - tasks_checklist: open_tasks passthrough, no rewrites of
        title/description/owner/due/status.
      - doc_body_md: deterministic markdown (no LLM compose).

    Total wall time: <50 ms (no network, no LLM). Compare to the
    LLM compose path which is 30-180 sec on gpt-5.5.
    """
    prior = candidate.prior_recordings[0] if candidate.prior_recordings else {}
    recap = _strip_prior_short_summary_body(prior.get("short_summary") or "")
    tasks_checklist: list[dict[str, Any]] = []
    for t in candidate.open_tasks:
        tasks_checklist.append({
            "task_id": t.get("task_id") or t.get("id"),
            "title": t.get("title") or "",
            "description": t.get("description") or "",
            "status": t.get("status") or "",
            "owner": t.get("owner") or t.get("owner_display_name") or "",
            "due": t.get("due") or t.get("due_date") or "",
        })
    return AgendaOutput(
        previous_recap=[recap] if recap else [],
        tasks_checklist=tasks_checklist,
        open_questions=[],
        doc_body_md=_build_lite_doc_body_md(
            candidate.title, recap,
            candidate.prior_recordings, tasks_checklist,
        ),
    )


def compose_agenda(
    candidate: AgendaCandidate,
    *,
    llm_backend: LLMBackend,
    model: str,
) -> AgendaOutput | None:
    """FR-CR-05-192u — operator-pinned 2026-05-22 «никакого LLM».
    Always runs `_compose_lite` — verbatim passthrough of the
    prior short_summary + open tasks. The `llm_backend` and `model`
    parameters are kept in the signature for source-compatibility
    with the existing runner; both are ignored.
    """
    try:
        return _compose_lite(candidate)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "agenda_compose_lite_failed",
            calendar_event_id=candidate.calendar_event_id,
            error=str(e),
        )
        return None


def _compose_agenda_llm_legacy(
    candidate: AgendaCandidate,
    *,
    llm_backend: LLMBackend,
    model: str,
) -> AgendaOutput | None:
    """Original LLM-driven compose (kept for tests + future toggle).
    Not called from production — see `compose_agenda` above."""
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
