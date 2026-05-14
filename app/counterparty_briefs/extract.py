"""FR-CR-05-168 — extraction stages.

Stage 0: ``extract_event_counterparties`` — LLM reads the
Calendar event (title + description + attendees) and returns
`{org_name, initial_persons}`. Internal `@thehumanoid.ai`
attendees are stripped before the model sees them; if all
attendees are internal AND title carries no external name,
returns the empty extraction.

Stage 2: ``extract_beneficiaries`` — cheap LLM picks ≤ N
beneficiaries from `OrgResearch.leadership ∪ initial_persons ∪
attendees`. The model returns a list of
`{person_name, person_role, evidence}` — at most `max_n` items.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


_INTERNAL_EMAIL_SUFFIX = "@thehumanoid.ai"


@dataclass
class PersonCandidate:
    person_name: str
    person_role: str | None = None


@dataclass
class EventExtraction:
    org_name: str | None = None
    initial_persons: list[PersonCandidate] = field(default_factory=list)


@dataclass
class BeneficiaryCandidate:
    person_name: str
    person_role: str | None = None
    evidence: str = ""


_EXTRACT_SYSTEM_PROMPT = """Ты — оператор-ассистент, готовящий справки к встречам.

На вход — Calendar event (title, description, attendees). На выход — JSON со структурой:
{
  "org_name": "<external organisation name or null>",
  "initial_persons": [
    {"person_name": "<full name>", "person_role": "<role or null>"}
  ]
}

Правила:
- org_name — внешняя компания. Если в встрече нет внешнего org (только internal sync) — null.
- initial_persons — список людей упомянутых в title/description/attendees (НЕ internal — НЕ те у кого email @thehumanoid.ai).
- Если список пуст — верни [].
- Без эмодзи, без markdown, ТОЛЬКО валидный JSON."""


def _call_llm_json(
    llm_backend: Any, messages: list[dict[str, str]], *, model: str
) -> Any:
    """Same shim as agenda.compose — accept either
    `complete_json(messages, model=)` or an OpenAI-SDK
    `_client` attribute on the backend."""
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


def _strip_internal_attendees(items: list[Any]) -> list[Any]:
    out: list[Any] = []
    for it in items or []:
        email = ""
        if isinstance(it, dict):
            email = (it.get("email") or "").strip().lower()
        elif isinstance(it, str):
            email = it.strip().lower()
        if email and email.endswith(_INTERNAL_EMAIL_SUFFIX):
            continue
        out.append(it)
    return out


def _build_extract_user_prompt(event: dict[str, Any]) -> str:
    attendees = _strip_internal_attendees(event.get("attendees") or [])
    return (
        f"Title: {event.get('title') or ''}\n"
        f"Description: {(event.get('description') or '').strip()[:2000]}\n"
        f"Attendees (external only): "
        f"{json.dumps(attendees, ensure_ascii=False)}\n"
    )


def extract_event_counterparties(
    *,
    event: dict[str, Any],
    llm_backend: Any,
    model: str,
) -> EventExtraction:
    """Stage 0 extraction. Returns an empty `EventExtraction` on
    any failure or unparseable LLM output — runner should skip
    the event in that case."""
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": _build_extract_user_prompt(event)},
    ]
    try:
        raw = _call_llm_json(llm_backend, messages, model=model)
    except Exception as e:  # noqa: BLE001
        log.warning("brief_extract_call_failed", error=str(e))
        return EventExtraction()
    if not isinstance(raw, dict):
        log.warning(
            "brief_extract_bad_shape", type=type(raw).__name__
        )
        return EventExtraction()
    org_name = (raw.get("org_name") or None) or None
    persons_raw = raw.get("initial_persons") or []
    persons: list[PersonCandidate] = []
    for p in persons_raw:
        if not isinstance(p, dict):
            continue
        name = (p.get("person_name") or "").strip()
        if not name:
            continue
        role = (p.get("person_role") or "").strip() or None
        persons.append(PersonCandidate(person_name=name, person_role=role))
    if isinstance(org_name, str):
        org_name = org_name.strip() or None
    return EventExtraction(
        org_name=org_name,
        initial_persons=persons,
    )


_BENEFICIARY_SYSTEM_PROMPT = """Ты — оператор-ассистент. Выбери до N ключевых бенефициаров встречи из списка leadership компании + initial_persons + attendees.

Вход JSON:
{
  "org_name": "...",
  "leadership": [{"name": "...", "role": "..."}],
  "initial_persons": [{"person_name": "...", "person_role": "..."}],
  "attendees": [{"email": "..."}]
}

Выход JSON:
{
  "beneficiaries": [
    {"person_name": "<full name>",
     "person_role": "<role or null>",
     "evidence": "<short 1-line why this person matters>"}
  ]
}

Правила:
- Не более N (см. max_n). Bias: attendees + initial_persons > leadership-роль (CEO/CIO/CFO/Board) > остальные.
- Дедуплицируй по имени.
- Внутренние сотрудники (@thehumanoid.ai) НЕ включаются.
- Только валидный JSON, без эмодзи / без markdown."""


def extract_beneficiaries(
    *,
    org_research: Any,
    attendees: list[Any],
    initial_persons: list[PersonCandidate | dict[str, Any]],
    max_n: int = 5,
    llm_backend: Any,
    model: str,
) -> list[BeneficiaryCandidate]:
    """Stage 2 — beneficiary picker. Returns up to `max_n`
    candidates, empty list on any error."""
    leadership: list[dict[str, str]] = []
    if org_research is not None:
        leadership = list(getattr(org_research, "leadership", None) or [])
    initial_payload: list[dict[str, Any]] = []
    for p in initial_persons or []:
        if isinstance(p, PersonCandidate):
            initial_payload.append({
                "person_name": p.person_name,
                "person_role": p.person_role,
            })
        elif isinstance(p, dict):
            initial_payload.append(p)
    user_input = json.dumps(
        {
            "org_name": getattr(org_research, "name", None)
            if org_research is not None else None,
            "leadership": leadership,
            "initial_persons": initial_payload,
            "attendees": _strip_internal_attendees(attendees),
            "max_n": max(1, int(max_n)),
        },
        ensure_ascii=False,
        indent=2,
    )
    messages = [
        {"role": "system", "content": _BENEFICIARY_SYSTEM_PROMPT},
        {"role": "user",   "content": user_input},
    ]
    try:
        raw = _call_llm_json(llm_backend, messages, model=model)
    except Exception as e:  # noqa: BLE001
        log.warning("brief_beneficiary_extract_failed", error=str(e))
        return []
    if not isinstance(raw, dict):
        return []
    rows = raw.get("beneficiaries") or []
    out: list[BeneficiaryCandidate] = []
    seen: set[str] = set()
    for r in rows[: max(1, int(max_n))]:
        if not isinstance(r, dict):
            continue
        name = (r.get("person_name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(BeneficiaryCandidate(
            person_name=name,
            person_role=(r.get("person_role") or None) or None,
            evidence=(r.get("evidence") or "").strip(),
        ))
    return out


__all__ = [
    "BeneficiaryCandidate",
    "EventExtraction",
    "PersonCandidate",
    "extract_beneficiaries",
    "extract_event_counterparties",
]
