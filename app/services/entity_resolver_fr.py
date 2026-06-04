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
    contact: str = ""
    sources: list[str] = field(default_factory=list)
    fr_id: str = ""

    def lean_line(self) -> str:
        parts = [self.name]
        for v in (self.entity_type, self.status, self.industry):
            if v:
                parts.append(v)
        # contact_person — needed to resolve PEOPLE / intro contacts
        # («Йохан» → Jochen Rudat, «Самир» → Samer Zawaideh), which live in
        # this field, not the company name.
        if self.contact and self.contact.strip().lower() != self.name.strip().lower():
            parts.append("contact: " + self.contact)
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
                contact=_field(e, "Contact"),
                sources=_dedup_sources(_field(e, "Source")),
                fr_id=_field(e, "ID"),
            )
        )
    return out


def shard_catalog(entities: list[FrEntity], *, max_chars: int) -> list[list[FrEntity]]:
    """Split the catalog into shards whose lean text is ≤ `max_chars` each, so
    every map-call stays under the per-call context budget (FR-EC-CRITIC: 30K
    tokens). Greedy pack, order preserved. A single oversize line still gets
    its own shard (never dropped)."""
    shards: list[list[FrEntity]] = []
    cur: list[FrEntity] = []
    cur_len = 0
    for ent in entities:
        ln = len(ent.lean_line()) + 1
        if cur and cur_len + ln > max_chars:
            shards.append(cur)
            cur, cur_len = [], 0
        cur.append(ent)
        cur_len += ln
    if cur:
        shards.append(cur)
    return shards


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
    "form using a fundraising CRM. The CRM list is AUTHORITATIVE for spelling — "
    "always prefer the CRM's exact form over the transcript's.\n"
    "Rules:\n"
    "- Match across languages/transliteration (Russian «Киван» = «Key 1 Capital», "
    "«Химейн» = «HUMAIN», «Виндроботикс» = «Vinrobotics»); use meaning + context, "
    "not just string similarity.\n"
    "- A mention may be a COMPANY or a PERSON (intro contact / team member) — "
    "match people against the `contact:` field too («Йохан» → Jochen Rudat, "
    "«Самир» → Samer Zawaideh).\n"
    "- Prefer the MOST SPECIFIC / most complete matching CRM entry: «Accenture "
    "Ventures» over «Accenture», «Amazon Industrial Fund» over «Amazon», "
    "«Lingotto Investment Management» over «Lingotto».\n"
    "- Use type/status/industry/source to disambiguate between similar names.\n"
    "- The input is a raw meeting TRANSCRIPT with phonetic/garbled spellings "
    "(«мирая», «Сива», «Тесер»). When a garbled mention could match several CRM "
    "entries, weigh the meeting context AND prefer an ACTIVE / in-pipeline entry "
    "(status active/follow_up/meeting_*/nda_*/data_room_*) over a rejected or "
    "archived one. Better null than a confident wrong pick.\n"
    "- A mention that is clearly an INDIVIDUAL PERSON who is NOT a team member "
    "and NOT itself the name of a company/fund → set canonical=null (external "
    "people are resolved separately; never map a person to a company/fund, e.g. "
    "«Стеф» must NOT become «SDF»).\n"
    "- Return a canonical name ONLY when it is genuinely in the CRM; otherwise "
    "set canonical=null (do NOT invent). Calibrate confidence honestly: a clear "
    "cross-lingual match IS high confidence.\n"
    "- Output STRICT JSON: a list of "
    '{"mention","canonical","source","confidence"} (confidence 0..1). '
    "No prose, no markdown."
)

_SHARD_NOTE = (
    "\nNOTE: the CRM list below is ONE SLICE of a larger CRM. Only return matches "
    "you find in THIS slice; omit mentions you cannot match here (another slice "
    "may hold them). Do not guess outside this slice."
)

_CRITIC_SYSTEM = (
    "You are the final critic merging entity-resolution results from several "
    "parallel scans of DIFFERENT slices of one fundraising CRM, for ONE meeting.\n"
    "For each distinct mention, choose the SINGLE best canonical from the "
    "candidates the slices proposed, using the meeting context. Resolve "
    "conflicts; prefer the most specific/complete CRM form; keep canonical=null "
    "if no slice produced a trustworthy match. Do not invent names absent from "
    "the candidates.\n"
    "Output STRICT JSON: a list of "
    '{"mention","canonical","source","confidence"}. No prose, no markdown.'
)


def build_resolution_messages(
    *,
    meeting_title: str | None,
    participants: list[str] | None,
    transcript_or_summary: str,
    catalog_text: str,
    max_transcript_chars: int = 40000,
    is_shard: bool = False,
) -> list[dict[str, str]]:
    """Build chat messages for ONE map-call (full catalog or a shard). The big
    `catalog_text` is its OWN message so a caller can mark it for prompt-caching;
    the per-meeting transcript is separate and small. `is_shard` appends the
    slice note so the model omits (not guesses) mentions absent from this slice."""
    system = _SYSTEM + (_SHARD_NOTE if is_shard else "")
    ctx = f"Meeting: {meeting_title or '(untitled)'}"
    if participants:
        ctx += "\nParticipants: " + ", ".join(participants)
    body = (transcript_or_summary or "")[:max_transcript_chars]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "CRM (canonical | type | status | industry | contact | source):\n"
                                    + catalog_text},
        {"role": "user", "content": ctx + "\n\nTRANSCRIPT/SUMMARY:\n" + body
                                    + "\n\nReturn the JSON list now."},
    ]


def build_critic_messages(
    *,
    meeting_title: str | None,
    participants: list[str] | None,
    transcript_or_summary: str,
    partials: list[list["Decision"]],
    max_transcript_chars: int = 12000,
) -> list[dict[str, str]]:
    """Reduce step — feed the per-shard candidate resolutions to the critic so
    it picks one canonical per mention. Candidates are small (just the lists),
    so this call stays well under budget."""
    ctx = f"Meeting: {meeting_title or '(untitled)'}"
    if participants:
        ctx += "\nParticipants: " + ", ".join(participants)
    cand_lines: list[str] = []
    for i, part in enumerate(partials):
        for d in part:
            if d.canonical:
                cand_lines.append(
                    f"[slice {i}] {d.mention} -> {d.canonical} "
                    f"({d.confidence:.2f}) {d.source}"
                )
    cand_block = "\n".join(cand_lines) or "(no candidates proposed)"
    body = (transcript_or_summary or "")[:max_transcript_chars]
    return [
        {"role": "system", "content": _CRITIC_SYSTEM},
        {"role": "user", "content": "CANDIDATE RESOLUTIONS (from parallel CRM slices):\n"
                                    + cand_block},
        {"role": "user", "content": ctx + "\n\nTRANSCRIPT/SUMMARY:\n" + body
                                    + "\n\nReturn the merged JSON list now."},
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


def build_replacements(
    decisions: list[Decision], *, min_confidence: float = 0.7
) -> dict[str, str]:
    """Turn confident decisions into a {surface_form: canonical} map for
    `canonicalize_text`. A mention can carry several surface variants joined
    by '/' or ',' (e.g. «Ki One Capital India/Киван») — split them so EVERY
    variant present in the text gets rewritten to the canonical. Identity and
    empty mappings are skipped."""
    out: dict[str, str] = {}
    for d in decisions:
        if not d.canonical or d.confidence < min_confidence:
            continue
        canon = d.canonical.strip()
        if not canon:
            continue
        for part in re.split(r"[/,;]", d.mention or ""):
            key = part.strip()
            if key and key.lower() != canon.lower():
                out[key] = canon
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


# -- map-reduce orchestration (shards ≤ budget, parallel, critic merge) --------

def team_roster_text(rows: list[tuple[str, str, str]]) -> str:
    """Render the internal team roster as a candidate slice. `rows` are
    (real_name, role, aliases) — people like «Йохан» → Jochen Rudat resolve
    against OUR team_members table, not Viktor's CRM. Marked `team` so the
    critic knows the source."""
    lines: list[str] = []
    for name, role, aliases in rows:
        if not name:
            continue
        parts = [name, "team"]
        if role:
            parts.append(role)
        if aliases:
            parts.append("aliases: " + aliases)
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def shard_char_budget(
    *,
    max_context_tokens: int = 30000,
    transcript_chars: int = 0,
    overhead_tokens: int = 1200,
    chars_per_token: float = 3.5,
) -> int:
    """Char budget for ONE shard's catalog text so a map-call (system +
    transcript + shard) stays under `max_context_tokens`."""
    avail_tokens = max_context_tokens - overhead_tokens - int(transcript_chars / chars_per_token)
    return max(20000, int(avail_tokens * chars_per_token))


def assemble_shard_texts(
    catalog: list[FrEntity], roster_text: str, *, max_chars: int
) -> list[str]:
    """Render the candidate slices for the map step. CRM is sharded to
    `max_chars`; the team roster is FOLDED into the last shard when it fits, so
    a catalog that fits in one slice yields ONE shard_text → a SINGLE
    reasoning pass (no critic-merge variance). Only when the data exceeds one
    slice do we fall back to multiple shards + critic."""
    shards = [lean_catalog_text(sh) for sh in shard_catalog(catalog, max_chars=max_chars)]
    if roster_text:
        if shards and len(shards[-1]) + len(roster_text) + 1 <= max_chars:
            shards[-1] = shards[-1] + "\n" + roster_text
        else:
            shards.append(roster_text)
    if not shards and roster_text:
        shards = [roster_text]
    return shards


def merge_partials_deterministic(partials: list[list[Decision]]) -> list[Decision]:
    """Fallback reduce (no LLM): per mention, keep the proposal with a canonical
    and the highest confidence; preserve first-seen order."""
    best: dict[str, Decision] = {}
    order: list[str] = []
    for part in partials:
        for d in part:
            key = d.mention.strip().lower()
            if not key:
                continue
            if key not in best:
                order.append(key)
                best[key] = d
                continue
            cur = best[key]
            if (d.canonical and not cur.canonical) or (
                d.canonical and cur.canonical and d.confidence > cur.confidence
            ):
                best[key] = d
    return [best[k] for k in order]


def resolve_sharded(
    *,
    meeting_title: str | None,
    participants: list[str] | None,
    transcript_or_summary: str,
    shard_texts: list[str],
    call_map: Callable[[list[dict[str, str]]], list[Decision]],
    call_critic: Callable[[list[dict[str, str]]], list[Decision]] | None = None,
    max_workers: int = 4,
) -> list[Decision]:
    """Map-reduce resolve. Each `shard_texts` entry (CRM slice OR team roster)
    is mapped in PARALLEL against the transcript via `call_map`; if there is
    more than one shard the per-shard candidates are merged by `call_critic`
    (LLM) when provided, else deterministically. `call_map`/`call_critic` take
    chat messages and return Decisions — injected so the module stays free of
    any specific LLM SDK and is unit-testable."""
    from concurrent.futures import ThreadPoolExecutor

    multi = len(shard_texts) > 1

    def _one(arg: tuple[int, str]) -> list[Decision]:
        idx, text = arg
        msgs = build_resolution_messages(
            meeting_title=meeting_title, participants=participants,
            transcript_or_summary=transcript_or_summary, catalog_text=text,
            is_shard=multi,
        )
        try:
            return call_map(msgs)
        except Exception as e:  # noqa: BLE001
            log.warning("fr_shard_map_failed", shard=idx, error=str(e))
            return []

    jobs = list(enumerate(shard_texts))
    if not jobs:
        return []
    workers = max(1, min(max_workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        partials = list(pool.map(_one, jobs))

    if not multi:
        return partials[0] if partials else []
    if call_critic is not None:
        try:
            return call_critic(build_critic_messages(
                meeting_title=meeting_title, participants=participants,
                transcript_or_summary=transcript_or_summary, partials=partials,
            ))
        except Exception as e:  # noqa: BLE001
            log.warning("fr_critic_failed", error=str(e))
    return merge_partials_deterministic(partials)


__all__ = [
    "FrEntity",
    "Decision",
    "parse_fr_dump",
    "shard_catalog",
    "assemble_shard_texts",
    "lean_catalog_text",
    "team_roster_text",
    "shard_char_budget",
    "fetch_catalog",
    "reset_cache_for_tests",
    "build_resolution_messages",
    "build_critic_messages",
    "parse_resolution",
    "merge_partials_deterministic",
    "resolve_sharded",
    "build_replacements",
    "should_apply",
]
