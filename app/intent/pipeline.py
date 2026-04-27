"""Intent-extraction pipeline wired as a LangGraph state machine.

--- Spec (FR-CR-04-1 … FR-CR-04-5, NFR-CR-04-1) ---

Graph shape:

    START ─▶ detect ─▶ is_task?
                        │
                        ├── false ──▶ END (no_action)
                        │
                        └── true  ──▶ describe ┐
                                      owner    ├─▶ assemble ─▶ END
                                      date     ┘

* `detect` — one focused LLM call, yes/no + confidence.
* `describe` / `owner` / `date` — three parallel extractors.
* `date` — dedicated LLM call on a stronger model (gpt-4o by default)
  backed by a Python validator (date_resolver) that fills in when the
  LLM returned null and replaces obviously-broken answers.
* `assemble` — build IntentClassification.

Each extractor is isolated: a stage failing (network / malformed
output) leaves its field null so the downstream follow-up loop can
fill it in chat.
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.intent.date_prompt import (
    DATE_SYSTEM_PROMPT,
    DATE_TOOL_DESCRIPTION,
    DATE_TOOL_NAME,
    DATE_TOOL_PARAMETERS,
    build_date_user_prompt,
)
from app.intent.date_resolver import resolve_due_date, strip_date_phrase
from app.intent.detect_prompt import (
    DETECT_SYSTEM_PROMPT,
    DETECT_TOOL_DESCRIPTION,
    DETECT_TOOL_NAME,
    DETECT_TOOL_PARAMETERS,
    build_detect_user_prompt,
)
from app.intent.owner_prompt import (
    OWNER_SYSTEM_PROMPT,
    OWNER_TOOL_DESCRIPTION,
    OWNER_TOOL_NAME,
    OWNER_TOOL_PARAMETERS,
    build_owner_user_prompt,
)
from app.intent.title_prompt import (
    TITLE_SYSTEM_PROMPT,
    TITLE_TOOL_DESCRIPTION,
    TITLE_TOOL_NAME,
    TITLE_TOOL_PARAMETERS,
    build_title_user_prompt,
)
from app.logging_setup import get_logger
from app.schemas.intent import IntentClassification, IntentType, TaskDraft

log = get_logger(__name__)


class IntentState(TypedDict, total=False):
    # Inputs (populated once at graph entry).
    backend: Any
    source_text: str
    context_messages: list[dict]
    author_user_id: Optional[str]
    today: _date
    date_model: Optional[str]
    known_employees: list[dict]

    # Stage 1 output.
    is_task: bool
    detect_confidence: float
    detect_reasoning: Optional[str]

    # Stage 2 outputs.
    title: Optional[str]
    description: Optional[str]
    priority: str
    owner_user_id: Optional[str]
    owner_display_name: Optional[str]
    due_date: Optional[_date]

    # Stage 3 output.
    classification: IntentClassification


def _safe_call_tool(
    backend,
    *,
    system_prompt: str,
    user_prompt: str,
    tool_name: str,
    tool_description: str,
    tool_parameters: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any] | None:
    """Wrap backend.call_tool so any failure returns None instead of
    aborting the whole graph."""
    try:
        kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_name=tool_name,
            tool_description=tool_description,
            tool_parameters=tool_parameters,
        )
        if model:
            kwargs["model"] = model
        out = backend.call_tool(**kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("pipeline_stage_failed", tool=tool_name, error=str(e))
        return None
    return out if isinstance(out, dict) else None


# --------------------------------------------------------------------------- #
# Graph nodes
# --------------------------------------------------------------------------- #


def node_detect(state: IntentState) -> dict[str, Any]:
    data = _safe_call_tool(
        state["backend"],
        system_prompt=DETECT_SYSTEM_PROMPT,
        user_prompt=build_detect_user_prompt(
            source_text=state["source_text"],
            context_messages=state.get("context_messages", []),
        ),
        tool_name=DETECT_TOOL_NAME,
        tool_description=DETECT_TOOL_DESCRIPTION,
        tool_parameters=DETECT_TOOL_PARAMETERS,
    )
    if not data:
        return {
            "is_task": False,
            "detect_confidence": 0.0,
            "detect_reasoning": None,
        }
    return {
        "is_task": bool(data.get("is_task")),
        "detect_confidence": float(data.get("confidence", 0.0)),
        "detect_reasoning": data.get("reasoning"),
    }


def _route_after_detect(state: IntentState) -> list[str]:
    """Fan out the three extractors only when detection said yes."""
    if not state.get("is_task"):
        return ["assemble"]
    return ["describe", "owner", "date"]


def node_describe(state: IntentState) -> dict[str, Any]:
    data = _safe_call_tool(
        state["backend"],
        system_prompt=TITLE_SYSTEM_PROMPT,
        user_prompt=build_title_user_prompt(
            source_text=state["source_text"],
            context_messages=state.get("context_messages", []),
        ),
        tool_name=TITLE_TOOL_NAME,
        tool_description=TITLE_TOOL_DESCRIPTION,
        tool_parameters=TITLE_TOOL_PARAMETERS,
    ) or {}
    raw_title = (data.get("title") or state["source_text"][:120]).strip()
    title = strip_date_phrase(raw_title) or "(untitled)"
    raw_desc = data.get("description")
    if isinstance(raw_desc, str) and raw_desc.strip():
        description = strip_date_phrase(raw_desc.strip()) or None
    else:
        description = None
    return {
        "title": title,
        "description": description,
        "priority": data.get("priority") or "medium",
    }


def node_owner(state: IntentState) -> dict[str, Any]:
    known_employees: list[dict] = state.get("known_employees") or []
    data = _safe_call_tool(
        state["backend"],
        system_prompt=OWNER_SYSTEM_PROMPT,
        user_prompt=build_owner_user_prompt(
            source_text=state["source_text"],
            context_messages=state.get("context_messages", []),
            author_user_id=state.get("author_user_id"),
            known_employees=known_employees,
        ),
        tool_name=OWNER_TOOL_NAME,
        tool_description=OWNER_TOOL_DESCRIPTION,
        tool_parameters=OWNER_TOOL_PARAMETERS,
    ) or {}
    slack_user_id = data.get("slack_user_id") or None
    display_name = data.get("display_name") or None

    # Validate slack_user_id against the table — drop hallucinations.
    if known_employees and slack_user_id:
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        if slack_user_id not in valid_ids:
            slack_user_id = None

    # If LLM returned only a name, try to resolve it locally to a real
    # slack_user_id from the same table.
    if not slack_user_id and display_name and known_employees:
        from app.services.owners import resolve_owner_hint

        candidates = [
            {"slack_user_id": e["slack_user_id"], "display_name": e.get("display_name") or e["slack_user_id"]}
            for e in known_employees
            if e.get("slack_user_id")
        ]
        match = resolve_owner_hint(hint_text=display_name, allowed_owners=candidates)
        if match is not None:
            slack_user_id = match["slack_user_id"]
            display_name = match["display_name"]

    return {
        "owner_user_id": slack_user_id,
        "owner_display_name": display_name,
    }


def node_date(state: IntentState) -> dict[str, Any]:
    """Dedicated date extractor.

    Strategy (FR-CR-04-3, option B): LLM first on the stronger model,
    Python resolver as a validator / fallback.

      1. Run the LLM; expect an ISO string or null.
      2. Parse it. Accept only dates that parse and are strictly after
         today — unless source_text explicitly says "сегодня"/"today".
      3. If the LLM's answer is null OR rejected, fall back to
         resolve_due_date().
    """
    today: _date = state["today"]
    source_text: str = state["source_text"]
    data = _safe_call_tool(
        state["backend"],
        system_prompt=DATE_SYSTEM_PROMPT,
        user_prompt=build_date_user_prompt(
            source_text=source_text,
            current_date=today.isoformat(),
            current_weekday=today.strftime("%A"),
        ),
        tool_name=DATE_TOOL_NAME,
        tool_description=DATE_TOOL_DESCRIPTION,
        tool_parameters=DATE_TOOL_PARAMETERS,
        model=state.get("date_model"),
    ) or {}

    llm_iso = data.get("due_date")
    llm_reasoning = data.get("reasoning")
    picked: _date | None = None
    rejected: str | None = None
    if isinstance(llm_iso, str) and llm_iso:
        try:
            parsed = _date.fromisoformat(llm_iso)
        except ValueError:
            parsed = None
        if parsed is not None and _date_is_acceptable(parsed, source_text, today):
            picked = parsed
        else:
            rejected = llm_iso

    source_used = "llm"
    if picked is None:
        picked = resolve_due_date(source_text, today)
        source_used = "python_fallback" if picked else "none"

    log.info(
        "date_node_result",
        llm_iso=llm_iso,
        llm_reasoning=llm_reasoning,
        rejected=rejected,
        final=picked.isoformat() if picked else None,
        source=source_used,
        model=state.get("date_model"),
    )
    return {"due_date": picked}


def _date_is_acceptable(d: _date, source_text: str, today: _date) -> bool:
    """A date from the LLM is accepted only if it's strictly in the
    future OR the message explicitly says 'сегодня' / 'today'."""
    if d > today:
        return True
    if d == today:
        lo = source_text.lower()
        return "сегодня" in lo or "today" in lo
    return False


def node_assemble(state: IntentState) -> dict[str, Any]:
    if not state.get("is_task"):
        return {
            "classification": IntentClassification(
                intent=IntentType.no_action,
                confidence=float(state.get("detect_confidence", 0.0)),
                reasoning=state.get("detect_reasoning"),
            )
        }
    title = state.get("title") or state["source_text"][:120] or "(untitled)"
    return {
        "classification": IntentClassification(
            intent=IntentType.create_task,
            confidence=float(state.get("detect_confidence", 0.75)),
            reasoning=state.get("detect_reasoning"),
            task=TaskDraft(
                title=title,
                description=state.get("description"),
                priority=state.get("priority") or "medium",
                owner_user_id=state.get("owner_user_id"),
                owner_display_name=state.get("owner_display_name"),
                due_date=state.get("due_date"),
            ),
        )
    }


# --------------------------------------------------------------------------- #
# Compiled graph (single instance, thread-safe because nodes are pure).
# --------------------------------------------------------------------------- #


def _build_graph():
    g: StateGraph = StateGraph(IntentState)
    g.add_node("detect", node_detect)
    g.add_node("describe", node_describe)
    g.add_node("owner", node_owner)
    g.add_node("date", node_date)
    g.add_node("assemble", node_assemble)

    g.add_edge(START, "detect")
    g.add_conditional_edges(
        "detect",
        _route_after_detect,
        {"describe": "describe", "owner": "owner", "date": "date", "assemble": "assemble"},
    )
    g.add_edge("describe", "assemble")
    g.add_edge("owner", "assemble")
    g.add_edge("date", "assemble")
    g.add_edge("assemble", END)
    return g.compile()


_GRAPH = _build_graph()


def run_pipeline(
    *,
    backend,
    source_text: str,
    context_messages: list[dict],
    author_user_id: str | None,
    today: _date,
    date_model: str | None = None,
    known_employees: list[dict] | None = None,
) -> IntentClassification:
    initial: IntentState = {
        "backend": backend,
        "source_text": source_text,
        "context_messages": context_messages,
        "author_user_id": author_user_id,
        "today": today,
        "date_model": date_model,
        "known_employees": known_employees or [],
    }
    final = _GRAPH.invoke(initial)
    return final["classification"]


__all__ = ["run_pipeline", "IntentState"]
