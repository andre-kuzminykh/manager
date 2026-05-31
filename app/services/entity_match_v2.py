"""FR-CR-05-222 — agentic entity matcher (LangGraph critic subgraph).

Given ONE extracted mention (a name from a Zoom/Fireflies transcript,
often phonetically garbled by Whisper) plus surrounding context, decide
WHICH directory entity it refers to — using pgvector top-K retrieval +
an LLM critic, instead of dumping the whole directory into the prompt.

Graph:

    START ─▶ retrieve ─▶ critique ─▶ confident?
                            ▲            │
                            │            ├── no  & attempts left ─▶ disambiguate ─┐
                            └────────────────────────────────────────────────────┘
                                         │
                                         └── yes / no-more-attempts ─▶ END

`retrieve_fn` (pgvector search) and `backend` (LLM) are injected so the
graph is unit-testable without a DB or OpenAI. The result is always
auditable: it carries the candidate list, the chosen id, a confidence
and the critic's reasoning — so the FR-CR-05-224 review harness can
explain «кого извлёк и почему».
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.logging_setup import get_logger

log = get_logger(__name__)

# Below this critic confidence we try one disambiguation pass (widen K).
CONF_THRESHOLD = 0.6
MAX_ATTEMPTS = 2
DEFAULT_K = 10
WIDEN_K = 25

# retrieve_fn(query_text, k) -> [{entity_id, text_repr, score}]
RetrieveFn = Callable[[str, int], list[dict[str, Any]]]


CRITIC_SYSTEM_PROMPT = """\
You decide which directory entity an extracted mention refers to.

You receive:
- mention: a name as it appeared in a meeting transcript. It may be
  phonetically garbled by speech-to-text («Адног» for ADNOC, «Голдман
  Сакс» for Goldman Sachs), abbreviated, or partial.
- context: the surrounding transcript / meeting context.
- candidates: the top vector-search matches from the directory, each
  with an id and a text description. ONLY these candidates are valid
  answers.

Rules:
1. Pick the candidate whose entity the mention most plausibly refers
   to, accounting for transcription errors and context.
2. matched_entity_id MUST be one of the candidate ids, or null if NONE
   of the candidates is a credible match. Never invent an id.
3. confidence in [0,1]: how sure you are. Use <0.6 when the mention is
   ambiguous, the candidates are all weak, or context doesn't help.
4. reasoning: one or two sentences. Quote the mention and name the
   candidate. If null, say why none fit.

Respond with a single JSON object via the provided tool.
"""

CRITIC_TOOL_NAME = "record_entity_match"
CRITIC_TOOL_DESCRIPTION = "Record which directory entity the mention matches."
CRITIC_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matched_entity_id": {
            "type": ["string", "null"],
            "description": "id of the chosen candidate, or null if none match.",
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
    },
    "required": ["matched_entity_id", "confidence", "reasoning"],
}


def build_critic_user_prompt(
    *, mention: str, context: str, candidates: list[dict[str, Any]]
) -> str:
    lines = [f"mention: {mention}", ""]
    if context and context.strip():
        lines.append("context:")
        lines.append(context.strip()[:1500])
        lines.append("")
    lines.append("candidates (id — description — vector_score):")
    for c in candidates:
        lines.append(
            f"- {c.get('entity_id')} — {(c.get('text_repr') or '')[:200]} "
            f"— {round(float(c.get('score', 0.0)), 3)}"
        )
    return "\n".join(lines)


class EntityMatchState(TypedDict, total=False):
    # inputs
    kind: str
    mention: str
    context: str
    retrieve_fn: RetrieveFn
    backend: Any  # LLMBackend with .call_tool(...)
    critic_model: Optional[str]
    k: int
    # working
    attempt: int
    candidates: list[dict[str, Any]]
    # output
    matched_entity_id: Optional[str]
    confidence: float
    reasoning: str


def _query_text(state: EntityMatchState) -> str:
    """Retrieval query = mention + a little context (helps disambiguate
    common surnames / abbreviations)."""
    mention = state.get("mention", "")
    ctx = (state.get("context") or "").strip()
    if ctx:
        return f"{mention}. {ctx[:300]}"
    return mention


def node_retrieve(state: EntityMatchState) -> dict[str, Any]:
    attempt = state.get("attempt", 0)
    k = WIDEN_K if attempt >= 1 else state.get("k", DEFAULT_K)
    fn = state["retrieve_fn"]
    cands = fn(_query_text(state), k)
    log.info(
        "entity_match_retrieve",
        kind=state.get("kind"), mention=state.get("mention"),
        attempt=attempt, k=k, n_candidates=len(cands),
    )
    return {"candidates": cands}


def _safe_call_tool(backend, **kw) -> dict[str, Any]:
    try:
        return backend.call_tool(**kw) or {}
    except Exception as e:  # noqa: BLE001 — degrade to no-match
        log.error("entity_match_critic_failed", error=str(e))
        return {}


def node_critique(state: EntityMatchState) -> dict[str, Any]:
    candidates = state.get("candidates") or []
    if not candidates:
        return {
            "matched_entity_id": None, "confidence": 0.0,
            "reasoning": "no candidates returned by retrieval",
            "attempt": state.get("attempt", 0) + 1,
        }
    data = _safe_call_tool(
        state["backend"],
        system_prompt=CRITIC_SYSTEM_PROMPT,
        user_prompt=build_critic_user_prompt(
            mention=state.get("mention", ""),
            context=state.get("context", ""),
            candidates=candidates,
        ),
        tool_name=CRITIC_TOOL_NAME,
        tool_description=CRITIC_TOOL_DESCRIPTION,
        tool_parameters=CRITIC_TOOL_PARAMETERS,
        model=state.get("critic_model"),
    )
    mid = data.get("matched_entity_id")
    # Guard: the critic must pick a real candidate id (or null).
    valid_ids = {str(c.get("entity_id")) for c in candidates}
    if mid is not None and str(mid) not in valid_ids:
        log.warning("entity_match_invalid_id", picked=mid, valid=list(valid_ids)[:10])
        mid = None
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {
        "matched_entity_id": str(mid) if mid is not None else None,
        "confidence": conf,
        "reasoning": data.get("reasoning") or "",
        "attempt": state.get("attempt", 0) + 1,
    }


def _route_after_critique(state: EntityMatchState) -> str:
    conf = state.get("confidence", 0.0)
    attempt = state.get("attempt", 0)
    matched = state.get("matched_entity_id")
    # Confident enough, or out of attempts → done.
    if conf >= CONF_THRESHOLD or attempt >= MAX_ATTEMPTS:
        return "final"
    # Low confidence and attempts remain → widen retrieval and re-judge.
    # (Even a non-null low-conf match gets a second look.)
    _ = matched
    return "disambiguate"


def node_disambiguate(state: EntityMatchState) -> dict[str, Any]:
    # No state change beyond keeping attempt; node_retrieve widens K
    # because attempt >= 1 now. Kept as an explicit node so the graph
    # is readable and future logic (query rewrite, ask-operator) has a
    # home.
    log.info(
        "entity_match_disambiguate",
        kind=state.get("kind"), mention=state.get("mention"),
        attempt=state.get("attempt"),
    )
    return {}


def _build_graph():
    g: StateGraph = StateGraph(EntityMatchState)
    g.add_node("retrieve", node_retrieve)
    g.add_node("critique", node_critique)
    g.add_node("disambiguate", node_disambiguate)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "critique")
    g.add_conditional_edges(
        "critique",
        _route_after_critique,
        {"final": END, "disambiguate": "disambiguate"},
    )
    g.add_edge("disambiguate", "retrieve")
    return g.compile()


_GRAPH = _build_graph()


def match_entity(
    *,
    kind: str,
    mention: str,
    context: str,
    retrieve_fn: RetrieveFn,
    backend: Any,
    critic_model: str | None = None,
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    """Run the matcher. Returns an auditable dict:
    {kind, mention, matched_entity_id, confidence, reasoning, candidates, attempts}.
    """
    initial: EntityMatchState = {
        "kind": kind,
        "mention": mention,
        "context": context,
        "retrieve_fn": retrieve_fn,
        "backend": backend,
        "critic_model": critic_model,
        "k": k,
        "attempt": 0,
    }
    final = _GRAPH.invoke(initial)
    return {
        "kind": kind,
        "mention": mention,
        "matched_entity_id": final.get("matched_entity_id"),
        "confidence": final.get("confidence", 0.0),
        "reasoning": final.get("reasoning", ""),
        "candidates": final.get("candidates", []),
        "attempts": final.get("attempt", 0),
    }


__all__ = [
    "match_entity",
    "build_critic_user_prompt",
    "CRITIC_SYSTEM_PROMPT",
    "CRITIC_TOOL_NAME",
    "CRITIC_TOOL_PARAMETERS",
    "CONF_THRESHOLD",
    "MAX_ATTEMPTS",
    "EntityMatchState",
]
