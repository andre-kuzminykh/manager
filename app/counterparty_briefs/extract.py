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

# FR-CR-05-168 polish 2026-05-18: операторские внутренние юрики,
# которых LLM иногда вытаскивает из attendees / title как
# «external counterparty». Sample: «SKL Robotics» (наша служебная
# entity для UK operations) попалась когда attendee имел email
# `@skl.vc`. Hard-code list of «we / our subsidiaries» so the
# extract step never marks one of these as the counterparty.
_INTERNAL_ORG_NAMES: set[str] = {
    "humanoid",
    "humanoid hq",
    "humanoid headquarters",
    "humanoid.ai",
    "thehumanoid.ai",
    "humanoidheadquarters",
    "skl robotics",
    "skl robotics ltd",
    "skl",
    "skl.vc",
}


def _is_internal_org(name: str | None) -> bool:
    if not name:
        return False
    key = name.strip().lower()
    key = key.replace("«", "").replace("»", "").replace('"', "")
    return key in _INTERNAL_ORG_NAMES


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
    # FR-CR-05-168 polish 2026-05-18: drop the org name when LLM
    # surfaced one of OUR own entities (Humanoid / SKL Robotics
    # / @skl.vc) — those are us, not external counterparties.
    if _is_internal_org(org_name):
        log.info(
            "brief_extract_dropped_internal_org",
            org_name=org_name,
        )
        org_name = None
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
    candidates.

    FR-CR-05-171 — operator-pinned 2026-05-20: «ты всегда присылаешь
    только по одному человеку, а надо по тем кто фигурирует во
    встрече». Previously this stage delegated 100% of the picking to
    the LLM, and the LLM frequently ignored the «bias attendees +
    initial_persons over leadership» rule, returning only company
    CEOs. Now the function:

      1. SEEDS the result with every external attendee that has a
         displayName, plus every Stage-0 `initial_person` extracted
         from title/description — these MUST appear, no LLM whim
         allowed.
      2. If the seed already fills `max_n` — return it, skip the
         LLM call entirely (cheaper + faster + deterministic).
      3. Otherwise asks the LLM to TOP UP from leadership only, up
         to the remaining slots; dedupes against the seed.

    Net effect: a meeting with «Baris Yildiz (Apple) <> Artem
    Sokolov» now always produces a brief about Baris, even if the
    LLM still wants Tim Cook — Cook lands in the remaining slots
    only after Baris is in.
    """
    seed: list[BeneficiaryCandidate] = []
    seen: set[str] = set()

    def _push(name: str, role: str | None, evidence: str) -> None:
        n = (name or "").strip()
        if not n:
            return
        key = n.lower()
        if key in seen:
            return
        seen.add(key)
        seed.append(BeneficiaryCandidate(
            person_name=n, person_role=role, evidence=evidence,
        ))

    # 1a. Seed initial_persons (Stage-0 extracted from title/desc).
    for p in initial_persons or []:
        if isinstance(p, PersonCandidate):
            _push(p.person_name, p.person_role,
                  "mentioned in event title/description")
        elif isinstance(p, dict):
            _push(
                p.get("person_name") or "",
                (p.get("person_role") or None) or None,
                "mentioned in event title/description",
            )
    # 1b. Seed external attendees with a real display name. Bare-email
    # attendees (no displayName) are dropped — operator-pinned: brief
    # without a real name is noise.
    external_attendees = _strip_internal_attendees(attendees or [])
    for a in external_attendees:
        if not isinstance(a, dict):
            continue
        name = (a.get("displayName") or a.get("name") or "").strip()
        email = (a.get("email") or "").strip()
        if not name:
            continue
        evidence = f"meeting attendee ({email})" if email else "meeting attendee"
        _push(name, None, evidence)

    max_n = max(1, int(max_n))
    if len(seed) >= max_n:
        log.info(
            "brief_beneficiaries_seed_fills_quota",
            seed_count=len(seed), max_n=max_n,
            names=[b.person_name for b in seed],
        )
        return seed[:max_n]

    # 2. Top up with leadership picks via LLM (only if we have
    # leadership to pick from AND still have empty slots).
    leadership: list[dict[str, str]] = []
    if org_research is not None:
        leadership = list(getattr(org_research, "leadership", None) or [])
    if not leadership:
        log.info(
            "brief_beneficiaries_no_leadership_to_top_up",
            seed_count=len(seed),
        )
        return seed

    remaining = max_n - len(seed)
    user_input = json.dumps(
        {
            "org_name": getattr(org_research, "name", None),
            "leadership": leadership,
            "already_included": [b.person_name for b in seed],
            "max_n": remaining,
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
        return seed
    if not isinstance(raw, dict):
        return seed
    rows = raw.get("beneficiaries") or []
    for r in rows[:remaining]:
        if not isinstance(r, dict):
            continue
        name = (r.get("person_name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        seed.append(BeneficiaryCandidate(
            person_name=name,
            person_role=(r.get("person_role") or None) or None,
            evidence=(r.get("evidence") or "").strip(),
        ))
    log.info(
        "brief_beneficiaries_picked",
        total=len(seed), max_n=max_n,
        names=[b.person_name for b in seed],
    )
    return seed[:max_n]


__all__ = [
    "BeneficiaryCandidate",
    "EventExtraction",
    "PersonCandidate",
    "extract_beneficiaries",
    "extract_event_counterparties",
]
