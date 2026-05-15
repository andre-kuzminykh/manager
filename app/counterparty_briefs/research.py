"""FR-CR-05-168 — deep research wrappers.

Stage 1: ``research_org`` — OpenAI ``o4-mini-deep-research`` call
with the web search tool. Returns a structured ``OrgResearch``.
Stage 3: ``research_person`` — same model, per-beneficiary
deep-research → ``PersonResearch`` (operator-pinned §6.2 schema).

Cost control:
  * Per-call cost estimator (``_estimate_cost_usd``) gates the
    call against the per-event budget passed in by the caller.
  * Per-counterparty TTL cache via
    ``research_org_with_cache`` / ``research_person_with_cache`` —
    reuse the existing ``counterparty_briefs.research_payload``
    within ``cache_ttl_days``.

Failures are silent (return None) — the runner moves on to the
next candidate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.counterparty_briefs.extract import BeneficiaryCandidate
from app.logging_setup import get_logger
from app.models import CounterpartyBrief

log = get_logger(__name__)


@dataclass
class OrgResearch:
    name: str = ""
    official_name: str | None = None
    website: str | None = None
    headquarters: str | None = None
    type: str | None = None
    sector_focus: list[str] = field(default_factory=list)
    leadership: list[dict[str, Any]] = field(default_factory=list)
    portfolio_highlights: list[dict[str, Any]] = field(default_factory=list)
    recent_news: list[dict[str, Any]] = field(default_factory=list)
    overview_paragraph: str = ""
    cached: bool = False


@dataclass
class PersonResearch:
    photo_url: str | None = None
    personal_information: dict[str, Any] = field(default_factory=dict)
    profile_overview: str = ""
    current_positions: list[dict[str, Any]] = field(default_factory=list)
    previous_positions: list[dict[str, Any]] = field(default_factory=list)
    investment_highlights: dict[str, Any] = field(default_factory=dict)
    investments: list[dict[str, Any]] = field(default_factory=list)
    exits: str = ""
    achievements: list[str] = field(default_factory=list)
    honors_awards: list[str] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    publications: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[dict[str, Any]] = field(default_factory=list)
    cached: bool = False


_ORG_SYSTEM_PROMPT = """Ты — research-аналитик. Собирай deep research брифинг про компанию (юрлицо). Используй web search. Стиль operator: фактологически, без воды, без эмодзи.

Output JSON со схемой:
{
  "name": "<canonical short name>",
  "official_name": "<full legal name or null>",
  "website": "https://...",
  "headquarters": "City, Country",
  "type": "Sovereign Wealth Fund | VC | Corporate | ...",
  "sector_focus": ["..."],
  "leadership": [
    {"name": "...", "role": "CEO|CIO|CFO|Board|Founder|Partner|...",
     "linkedin_url": "...", "evidence_url": "..."}
  ],
  "portfolio_highlights": [{"name": "...", "deal_size": "...", "year": "..."}],
  "recent_news": [{"date": "YYYY-MM-DD", "title": "...", "url": "..."}],
  "overview_paragraph": "...150-250 words..."
}

ВЕРНИ ТОЛЬКО валидный JSON. Без markdown / без эмодзи / без префиксов."""


_PERSON_SYSTEM_PROMPT = """Ты — research-аналитик. Собирай deep research брифинг про конкретного человека (физлицо) в роли при компании. Используй web search. Стиль operator: фактологически, без воды, без эмодзи.

Output JSON со схемой (operator-pinned §6.2):
{
  "photo_url": "https://... (public URL or null)",
  "personal_information": {
    "name": "...", "role": "...", "location": "...",
    "linkedin_url": "...", "company_website": "...",
    "emails": ["..."], "phone": "..."
  },
  "profile_overview": "...150-250 words...",
  "current_positions": [
    {"role": "...", "company": "...", "duration": "<from> – <to>", "focus": "..."}
  ],
  "previous_positions": [
    {"role": "...", "company": "...", "duration": "<from> – <to>", "focus": "..."}
  ],
  "investment_highlights": {
    "entity_types": "...", "investor_type": "...", "investor_status": "Active|Inactive",
    "total_investments": "N or N/A",
    "active_portfolio": "N or N/A", "exits": "N or N/A",
    "median_round_amount": "$NM", "median_valuation": "$NM",
    "firm_wide_investments": "N/A",
    "investment_preferences": "..."
  },
  "investments": [
    {"company": "...", "deal_date": "...", "deal_type": "...",
     "deal_size": "$NM", "company_stage": "...", "industry": "..."}
  ],
  "exits": "...",
  "achievements": ["..."],
  "honors_awards": ["..."],
  "education": [{"institution": "...", "degree": "...", "years": "..."}],
  "publications": [{"title": "...", "url": "..."}],
  "skills": ["..."],
  "languages": [{"language": "...", "level": "..."}]
}

ВЕРНИ ТОЛЬКО валидный JSON. Без markdown / без эмодзи / без префиксов."""


def _estimate_cost_usd(*, model: str, prompt_chars: int) -> float:
    """Conservative cost estimator. o4-mini-deep-research пока без
    публичных pricing API — берём worst-case оценку $0.80 per
    call (включая web search). Можно переопределить через ENV
    `COUNTERPARTY_BRIEFS_PER_CALL_COST_USD_OVERRIDE` если operator
    хочет более жёсткий лимит.
    """
    import os
    override = os.getenv(
        "COUNTERPARTY_BRIEFS_PER_CALL_COST_USD_OVERRIDE", ""
    ).strip()
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return 0.8  # USD per deep-research call (conservative)


def _call_llm_json(
    llm_backend: Any, messages: list[dict[str, str]], *, model: str
) -> Any:
    """Dispatch to the right OpenAI endpoint based on the model.

    `o4-mini-deep-research` and the rest of the deep-research
    family is only exposed via the **Responses API**
    (`POST /v1/responses`) — calling it through
    `chat.completions` 404s with «This model is only supported
    in v1/responses». Detect by substring «deep-research» in the
    model name and route accordingly. Other models continue to
    use the regular JSON-mode chat completion.
    """
    if hasattr(llm_backend, "complete_json"):
        return llm_backend.complete_json(messages, model=model)
    client = getattr(llm_backend, "_client", None)
    if client is None:
        raise RuntimeError(
            "llm_backend has neither complete_json nor _client"
        )
    if "deep-research" in (model or "").lower():
        return _call_openai_responses_json(client, messages, model=model)
    return _call_openai_chat_json(client, messages, model=model)


def _call_openai_chat_json(
    client: Any, messages: list[dict[str, str]], *, model: str
) -> Any:
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


def _call_openai_responses_json(
    client: Any, messages: list[dict[str, str]], *, model: str
) -> Any:
    """Call the OpenAI Responses API with the web-search tool and
    extract a JSON object from the model's output.

    Responses API uses `input` (a single concatenated string or a
    structured list) instead of `messages`. We concatenate the
    system + user prompts and append a strict «return JSON only»
    reminder so the parser has something to work with.
    """
    prompt_parts: list[str] = []
    for m in messages:
        role = m.get("role", "user").upper()
        prompt_parts.append(f"# {role}\n{m.get('content', '')}")
    prompt_parts.append(
        "# RESPONSE FORMAT\nReturn ONLY a single valid JSON object "
        "matching the schema above. No markdown, no commentary, "
        "no code fences."
    )
    prompt = "\n\n".join(prompt_parts)

    try:
        resp = client.responses.create(
            model=model,
            input=prompt,
            tools=[{"type": "web_search"}],
            reasoning={"effort": "medium"},
            background=False,
        )
    except TypeError:
        # Older SDKs may not accept `background` / `reasoning`.
        # Retry without the optional kwargs.
        resp = client.responses.create(
            model=model,
            input=prompt,
            tools=[{"type": "web_search"}],
        )

    text = getattr(resp, "output_text", None)
    if not text:
        # Walk the `.output` array — Responses API returns a list
        # of items (web-search calls, tool calls, message items).
        # We grab the text content of the message item(s).
        for item in getattr(resp, "output", None) or []:
            item_type = getattr(item, "type", None) or (
                isinstance(item, dict) and item.get("type")
            )
            if item_type != "message":
                continue
            content = (
                getattr(item, "content", None)
                if not isinstance(item, dict) else item.get("content")
            ) or []
            for piece in content:
                p_type = getattr(piece, "type", None) or (
                    isinstance(piece, dict) and piece.get("type")
                )
                if p_type not in ("output_text", "text"):
                    continue
                p_text = (
                    getattr(piece, "text", None)
                    if not isinstance(piece, dict) else piece.get("text")
                )
                if p_text:
                    text = (text or "") + p_text

    text = (text or "").strip()
    if not text:
        return None
    # Tolerate models that wrap the JSON in ```json fences anyway.
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    return json.loads(text)


def _coerce_org(payload: dict[str, Any]) -> OrgResearch:
    return OrgResearch(
        name=str(payload.get("name") or "").strip(),
        official_name=(payload.get("official_name") or None) or None,
        website=(payload.get("website") or None) or None,
        headquarters=(payload.get("headquarters") or None) or None,
        type=(payload.get("type") or None) or None,
        sector_focus=list(payload.get("sector_focus") or []),
        leadership=list(payload.get("leadership") or []),
        portfolio_highlights=list(payload.get("portfolio_highlights") or []),
        recent_news=list(payload.get("recent_news") or []),
        overview_paragraph=str(payload.get("overview_paragraph") or "").strip(),
    )


def _coerce_person(payload: dict[str, Any]) -> PersonResearch:
    return PersonResearch(
        photo_url=(payload.get("photo_url") or None) or None,
        personal_information=dict(payload.get("personal_information") or {}),
        profile_overview=str(payload.get("profile_overview") or "").strip(),
        current_positions=list(payload.get("current_positions") or []),
        previous_positions=list(payload.get("previous_positions") or []),
        investment_highlights=dict(payload.get("investment_highlights") or {}),
        investments=list(payload.get("investments") or []),
        exits=str(payload.get("exits") or "").strip(),
        achievements=list(payload.get("achievements") or []),
        honors_awards=list(payload.get("honors_awards") or []),
        education=list(payload.get("education") or []),
        publications=list(payload.get("publications") or []),
        skills=list(payload.get("skills") or []),
        languages=list(payload.get("languages") or []),
    )


def research_org(
    *,
    org_name: str,
    llm_backend: Any,
    model: str,
    budget_usd: float,
    spent_usd: float = 0.0,
) -> OrgResearch | None:
    """Stage 1 — org deep research. Returns None on failure or
    when cost would exceed the per-event budget."""
    user_prompt = json.dumps(
        {"org_name": org_name}, ensure_ascii=False, indent=2
    )
    messages = [
        {"role": "system", "content": _ORG_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    cost = _estimate_cost_usd(
        model=model, prompt_chars=len(user_prompt),
    )
    if spent_usd + cost > budget_usd:
        log.info(
            "brief_research_budget_exhausted",
            stage="org", org=org_name,
            spent_usd=spent_usd, cost_estimate=cost, budget=budget_usd,
        )
        return None
    try:
        raw = _call_llm_json(llm_backend, messages, model=model)
    except Exception as e:  # noqa: BLE001
        log.warning("brief_research_org_failed", org=org_name, error=str(e))
        return None
    if not isinstance(raw, dict):
        log.warning(
            "brief_research_org_bad_shape", org=org_name,
            type=type(raw).__name__,
        )
        return None
    try:
        return _coerce_org(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("brief_research_org_coerce_failed", error=str(e))
        return None


def research_person(
    *,
    beneficiary: BeneficiaryCandidate,
    org_name: str | None,
    llm_backend: Any,
    model: str,
    budget_usd: float,
    spent_usd: float = 0.0,
) -> PersonResearch | None:
    user_prompt = json.dumps(
        {
            "person_name": beneficiary.person_name,
            "person_role": beneficiary.person_role,
            "org_name": org_name,
            "evidence": beneficiary.evidence,
        },
        ensure_ascii=False,
        indent=2,
    )
    messages = [
        {"role": "system", "content": _PERSON_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    cost = _estimate_cost_usd(model=model, prompt_chars=len(user_prompt))
    if spent_usd + cost > budget_usd:
        log.info(
            "brief_research_budget_exhausted",
            stage="person", person=beneficiary.person_name,
            spent_usd=spent_usd, cost_estimate=cost, budget=budget_usd,
        )
        return None
    try:
        raw = _call_llm_json(llm_backend, messages, model=model)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "brief_research_person_failed",
            person=beneficiary.person_name, error=str(e),
        )
        return None
    if not isinstance(raw, dict):
        log.warning(
            "brief_research_person_bad_shape",
            person=beneficiary.person_name,
            type=type(raw).__name__,
        )
        return None
    try:
        return _coerce_person(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("brief_research_person_coerce_failed", error=str(e))
        return None


# -- Cache helpers -----------------------------------------------------------

@dataclass
class CachedOrgResearch:
    payload: OrgResearch
    google_doc_url: str | None
    google_doc_id: str | None
    cached: bool = True


def research_org_with_cache(
    *,
    org_name: str,
    session: Session,
    ttl_days: int,
    llm_backend: Any,
    model: str,
    budget_usd: float,
    spent_usd: float = 0.0,
) -> CachedOrgResearch | None:
    """TTL cache: re-use prior org research payload if a brief
    row exists within `ttl_days`. Returns a `CachedOrgResearch`
    with `cached=True` on hit (no LLM call). On miss, runs the
    research call and returns `cached=False`. None on failure."""
    from app.counterparty_briefs.lookup import normalise_counterparty_name

    key = normalise_counterparty_name(org_name)
    if key:
        row = (
            session.query(CounterpartyBrief)
            .filter(CounterpartyBrief.kind == "org")
            .filter(CounterpartyBrief.counterparty_key == key)
            .first()
        )
        if row is not None and row.researched_at is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
            researched_at = row.researched_at
            if researched_at.tzinfo is None:
                researched_at = researched_at.replace(tzinfo=timezone.utc)
            if researched_at >= cutoff and row.research_payload:
                return CachedOrgResearch(
                    payload=_coerce_org(row.research_payload),
                    google_doc_url=row.google_doc_url,
                    google_doc_id=row.google_doc_id,
                    cached=True,
                )

    fresh = research_org(
        org_name=org_name, llm_backend=llm_backend, model=model,
        budget_usd=budget_usd, spent_usd=spent_usd,
    )
    if fresh is None:
        return None
    return CachedOrgResearch(
        payload=fresh, google_doc_url=None, google_doc_id=None,
        cached=False,
    )


__all__ = [
    "CachedOrgResearch",
    "OrgResearch",
    "PersonResearch",
    "research_org",
    "research_org_with_cache",
    "research_person",
]
