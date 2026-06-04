"""FR-EC-CRITIC — Fundraising entity resolver over Viktor's CRM (his n8n MCP).

Pipeline (SPEC_ENTITY_CRITIC_v0.1):

    Viktor MCP  humanoid_fr_search(query="", limit=2000)
        │   one text blob: '[{"response": "Found N...\\n\\n1. Name | ... | Source: ..."}]'
        ▼   parse_fr_dump            → list[FrEntity]   (lean: name/type/status/industry/sources)
        ▼   lean_catalog_text        → cached ~45K-token candidate list
        ▼   build_resolution_messages(meeting ctx + transcript + catalog)
        ▼   <reasoning LLM, ONE pass>
        ▼   parse_resolution         → list[Decision]   {mention, canonical, source, confidence}

No vector search. The catalog is cached module-side for
`ENTITY_FR_CATALOG_TTL_SECONDS` so a meeting costs one (cacheable) LLM call,
not a re-fetch. All pure functions are network-free and unit-tested against a
real MCP sample; `fetch_catalog` / `resolve` take injectable callables so the
tests never touch Google/OpenAI/n8n.

Everything here is OFF unless `ENTITY_FR_RESOLVER_ENABLED` — the module imports
cleanly with no creds and is only invoked behind the flag.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.logging_setup import get_logger

log = get_logger(__name__)

# Fields we keep from his verbose record — the rest (communication_log,
# last_update, contacts, aum, …) is dropped to keep the catalog ~45K tokens.
_WANT_KEYS = ("Type", "Status", "Industry")

# Split his response into per-company entries on a leading "N. ".
_ENTRY_RE = re.compile(r"(?m)^\d+\.\s")


@dataclass
class FrEntity:
    """One lean CRM record for the candidate catalog."""
    name: str
    entity_type: str = ""
    status: str = ""
    industry: str = ""
    sources: list[str] = field(default_factory=list)
    fr_id: str = ""

    def lean_line(self) -> str:
        parts = [self.name]
        for v in (self.entity_type, self.status, self.industry):
            if v:
                parts.append(v)
        if self.sources:
            parts.append(", ".join(self.sources))
        return " | ".join(parts)


def _unwrap_mcp_text(raw: str) -> str:
    """His MCP returns the search text wrapped as '[{"response": "..."}]'
    (or sometimes a bare dict). Return the inner `response` string; on any
    shape we don't recognise, return the raw text unchanged."""
    if not raw:
        return ""
    s = raw.strip()
    if not (s.startswith("[") or s.startswith("{")):
        return raw
    try:
        body = json.loads(s)
    except (ValueError, TypeError):
        return raw
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return str(body[0].get("response") or raw)
    if isinstance(body, dict):
        return str(body.get("response") or raw)
    return raw


def _field(entry: str, key: str) -> str:
    """Pull a `Key: value` token from one entry (value runs to the next
    ' | ' or end). Lean keys (Type/Status/Industry/Source) are short enums
    or names with no '|', so the non-greedy stop is safe."""
    m = re.search(rf"\b{key}:\s*([^|]+?)(?:\s*\||$)", entry)
    return m.group(1).strip() if m else ""


def _dedup_sources(raw_source: str) -> list[str]:
    """`Followers / Outreach, Followers / Outreach, List_Series_A / Pipeline_short`
    → ['Followers / Outreach', 'List_Series_A / Pipeline_short'] (order kept).
    His source_sheets array carries duplicates (one per merged row)."""
    out: list[str] = []
    seen: set[str] = set()
    for part in raw_source.split(","):
        p = part.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def parse_fr_dump(raw_mcp_text: str) -> list[FrEntity]:
    """Parse his humanoid_fr_search response into lean FrEntity records.

    Robust to the JSON wrapper, the "Found N results:" header, duplicate
    source sheets, and missing optional fields. Skips empty/headerless
    fragments.
    """
    resp = _unwrap_mcp_text(raw_mcp_text)
    if not resp:
        return []
    entries = _ENTRY_RE.split(resp)[1:]   # [0] = "Found N results:" preamble
    out: list[FrEntity] = []
    for e in entries:
        # Name is everything before the first ' | ' (strip trailing newlines).
        name = e.split(" | ", 1)[0].strip().strip("\n").strip()
        if not name:
            continue
        out.append(
            FrEntity(
                name=name,
                entity_type=_field(e, "Type"),
                status=_field(e, "Status"),
                industry=_field(e, "Industry"),
                sources=_dedup_sources(_field(e, "Source")),
                fr_id=_field(e, "ID"),
            )
        )
    return out


def lean_catalog_text(entities: list[FrEntity]) -> str:
    """Render the cached candidate list — one lean line per entity."""
    return "\n".join(ent.lean_line() for ent in entities)


# -- catalog fetch + TTL cache ------------------------------------------------

_CACHE: dict[str, tuple[float, list[FrEntity]]] = {}


def fetch_catalog(
    *,
    mcp_url: str,
    limit: int = 2000,
    timeout: float = 180.0,
    ttl_seconds: int = 3600,
    call_tool: Callable[..., tuple[bool, str]] | None = None,
    now: Callable[[], float] = time.time,
) -> list[FrEntity]:
    """Fetch + parse his CRM dump, cached per `mcp_url` for `ttl_seconds`.

    `call_tool` defaults to the real CEO-brain MCP client; tests inject a
    fake. Returns the cached list (never raises — on MCP failure returns
    the stale cache if present, else [])."""
    cached = _CACHE.get(mcp_url)
    if cached and (now() - cached[0]) < ttl_seconds:
        return cached[1]
    if call_tool is None:
        from app.ceo_brain.mcp_client import call_tool as call_tool  # noqa: PLC0415

    try:
        ok, txt = call_tool(
            url=mcp_url, tool_name="humanoid_fr_search",
            arguments={"query": "", "limit": str(limit)}, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("fr_catalog_fetch_failed", error=str(e))
        return cached[1] if cached else []
    if not ok:
        log.warning("fr_catalog_fetch_not_ok", error=txt[:200])
        return cached[1] if cached else []
    entities = parse_fr_dump(txt)
    if entities:
        _CACHE[mcp_url] = (now(), entities)
        log.info("fr_catalog_loaded", count=len(entities),
                 lean_chars=len(lean_catalog_text(entities)))
    return entities or (cached[1] if cached else [])


def reset_cache_for_tests() -> None:
    _CACHE.clear()


# -- prompt build + response parse (pure) -------------------------------------

_SYSTEM = (
    "You resolve entity mentions from a meeting transcript to their canonical "
    "form using a fundraising CRM. The CRM list is authoritative for spelling.\n"
    "Rules:\n"
    "- Match across languages/transliteration (e.g. Russian «Киван» may be the "
    "English «Ki One»); use meaning + context, not just string similarity.\n"
    "- Use type/status/industry/source to disambiguate between similar names.\n"
    "- Return a canonical name ONLY when reasonably confident it is in the CRM; "
    "otherwise set canonical=null (do NOT invent).\n"
    "- Output STRICT JSON: a list of "
    '{"mention","canonical","source","confidence"} (confidence 0..1). '
    "No prose, no markdown."
)


def build_resolution_messages(
    *,
    meeting_title: str | None,
    participants: list[str] | None,
    transcript_or_summary: str,
    catalog_text: str,
    max_transcript_chars: int = 24000,
) -> list[dict[str, str]]:
    """Build chat messages. The big `catalog_text` is its OWN message so a
    caller can mark it for prompt-caching; the per-meeting transcript is
    separate and small."""
    ctx = f"Meeting: {meeting_title or '(untitled)'}"
    if participants:
        ctx += "\nParticipants: " + ", ".join(participants)
    body = (transcript_or_summary or "")[:max_transcript_chars]
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "CRM (canonical | type | status | industry | source):\n"
                                    + catalog_text},
        {"role": "user", "content": ctx + "\n\nTRANSCRIPT/SUMMARY:\n" + body
                                    + "\n\nReturn the JSON list now."},
    ]


@dataclass
class Decision:
    mention: str
    canonical: str | None
    source: str = ""
    confidence: float = 0.0


def parse_resolution(text: str) -> list[Decision]:
    """Parse the model's JSON list. Tolerant of ```json fences and stray
    prose around the array; ignores malformed items. Never raises."""
    if not text:
        return []
    s = text.strip()
    # strip code fences
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    # grab the outermost [...] if there's surrounding prose
    if not s.startswith("["):
        i, j = s.find("["), s.rfind("]")
        if i != -1 and j != -1 and j > i:
            s = s[i:j + 1]
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[Decision] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        mention = str(it.get("mention") or "").strip()
        if not mention:
            continue
        canon = it.get("canonical")
        canon = str(canon).strip() if canon not in (None, "", "null") else None
        try:
            conf = float(it.get("confidence") or 0.0)
        except (ValueError, TypeError):
            conf = 0.0
        out.append(Decision(mention=mention, canonical=canon,
                            source=str(it.get("source") or "").strip(),
                            confidence=conf))
    return out


def should_apply(d: Decision, *, current: str | None, min_confidence: float = 0.7) -> bool:
    """Apply the resolver's pick only when confident, non-empty, and it
    actually changes the current form (identity-skip)."""
    if d.canonical is None or not d.canonical.strip():
        return False
    if d.confidence < min_confidence:
        return False
    if current is not None and d.canonical.strip().lower() == current.strip().lower():
        return False
    return True


__all__ = [
    "FrEntity",
    "Decision",
    "parse_fr_dump",
    "lean_catalog_text",
    "fetch_catalog",
    "reset_cache_for_tests",
    "build_resolution_messages",
    "parse_resolution",
    "should_apply",
]
