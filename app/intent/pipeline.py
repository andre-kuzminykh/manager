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
from app.intent.date_resolver import resolve_due_date
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
    # FR-CR-05-214 — model for the title/description stage (gpt-4o),
    # separate from the gpt-4o-mini intent stage which hallucinated names.
    title_model: Optional[str]
    known_employees: list[dict]

    # Stage 1 output.
    is_task: bool
    detect_confidence: float
    detect_reasoning: Optional[str]
    # FR-CR-05-05 — list of source-text spans, one per detected task.
    # When the message is a single-task one, this stays empty and the
    # downstream stages run on `source_text` as-is.
    task_chunks: list[str]

    # Stage 2 outputs (single-task path — kept for back-compat with
    # callers and test fixtures that operate on the legacy shape).
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


_TRANSCRIPT_PREFIX_RE = __import__("re").compile(
    # FR-CR-05-108/109 — operator regression after
    # FR-CR-05-89/94/104: gpt-5.5 keeps marking transcript
    # dumps + reflection statements as is_task=true despite
    # the detect prompt's explicit rejection rules. This is
    # the deliberate Python guard that runs BEFORE the LLM
    # call — sources matching these head-of-message patterns
    # are forced to is_task=false.
    r"^\s*("
    # Transcription dumps (FR-CR-05-108).
    r"на\s+(?:изображени|скрин|фото|картинк)|"
    r"обсужда[еюя]т[ьс]?|"
    r"в\s+(?:переписк|треде|диалог|чате)|"
    r"по\s+(?:переписк|обсуждени)|"
    r"сообщени[ея]\s+от\s+|"
    r"in\s+the\s+(?:image|screenshot|chat|thread)|"
    r"this\s+(?:image|screenshot)\s+(?:shows?|depicts?)|"
    r"on\s+the\s+screen|"
    # Reflection / observation / confusion (FR-CR-05-109).
    r"только\s+я\s+не\s+(?:понял|понимаю|уверен)|"
    r"я\s+не\s+(?:понял|понимаю|уверен)|"
    r"мне\s+(?:кажется|показалось)|"
    r"возможно[,.]|"
    r"странно[,.]?\s+что|"
    r"интересно[,.]?\s+что|"
    r"видимо[,.]|"
    r"i\s+(?:don'?t|didn'?t|do\s+not)\s+(?:get|understand|know)|"
    r"i'?m\s+not\s+sure|"
    r"i\s+think\s+(?:we|they|maybe|perhaps)|"
    r"it\s+seems\s+(?:like|that)"
    r")",
    flags=__import__("re").IGNORECASE | __import__("re").UNICODE,
)


def _looks_like_transcript_dump(source: str) -> bool:
    """FR-CR-05-108 — return True when the source starts with a
    phrase that DESCRIBES a screenshot / chat snippet rather than
    delegates work. Operator regressions on this pattern (single-
    handedly): «На изображении показано электронное письмо…»;
    «Обсуждают сообщения внутри группы…»; «На изображении
    показано сообщение от Chris Doran…»."""
    if not source:
        return False
    return _TRANSCRIPT_PREFIX_RE.match(source) is not None


def node_detect(state: IntentState) -> dict[str, Any]:
    # FR-CR-05-108 — fast-path: transcript-prefix sources are
    # never tasks, regardless of what the LLM says. Skip the
    # LLM call entirely.
    if _looks_like_transcript_dump(state["source_text"]):
        log.info(
            "detect_node_transcript_prefix_dropped",
            source=(state["source_text"] or "")[:120],
        )
        return {
            "is_task": False,
            "detect_confidence": 0.99,
            "detect_reasoning": "transcript-prefix source rejected by Python guard (FR-CR-05-108)",
            "task_chunks": [],
        }
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
            "task_chunks": [],
        }
    chunks_raw = data.get("task_chunks") or []
    if not isinstance(chunks_raw, list):
        chunks_raw = []
    chunks = [c.strip() for c in chunks_raw if isinstance(c, str) and c.strip()]
    # If the LLM said ``task_count > 1`` but didn't actually emit
    # multiple chunks, fall back to single-task — better that than
    # truncate the message.
    if len(chunks) < 2:
        chunks = []
    return {
        "is_task": bool(data.get("is_task")),
        "detect_confidence": float(data.get("confidence", 0.0)),
        "detect_reasoning": data.get("reasoning"),
        "task_chunks": chunks,
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
        model=state.get("title_model"),
    ) or {}
    # FR-CR-05-216 — operator wants the timeframe KEPT in the title
    # («Поставить встречу на пн/вт …») and FULL details in the
    # description (reversal of FR-CR-04-5 date-stripping). The LLM
    # already emits a tight title carrying the deadline + purpose and
    # a complete description, so we keep both verbatim.
    title = (data.get("title") or state["source_text"][:120]).strip() or "(untitled)"
    raw_desc = data.get("description")
    description = (
        raw_desc.strip() if isinstance(raw_desc, str) and raw_desc.strip() else None
    )
    return {
        "title": title,
        "description": description,
        "priority": data.get("priority") or "medium",
    }


def node_owner(state: IntentState) -> dict[str, Any]:
    known_employees: list[dict] = state.get("known_employees") or []
    source_text: str = state.get("source_text") or ""
    context_messages: list[dict] = state.get("context_messages") or []
    author_user_id: str | None = state.get("author_user_id")

    data = _safe_call_tool(
        state["backend"],
        system_prompt=OWNER_SYSTEM_PROMPT,
        user_prompt=build_owner_user_prompt(
            source_text=source_text,
            context_messages=context_messages,
            author_user_id=author_user_id,
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
    # slack_user_id from the same table. Match against BOTH display_name
    # and real_name — the LLM sometimes echoes the user's full real name
    # (e.g. "Андре Кузьминых") even though our display_name is shorter
    # (e.g. "Andre").
    if not slack_user_id and display_name and known_employees:
        from app.services.owners import resolve_owner_hint

        candidates: list[dict[str, str]] = []
        for e in known_employees:
            sid = e.get("slack_user_id")
            if not sid:
                continue
            primary = e.get("display_name") or sid
            candidates.append({"slack_user_id": sid, "display_name": primary})
            real = e.get("real_name")
            if real and real != primary:
                # A second pseudo-row lets resolve_owner_hint match on
                # the real name without needing a second pass.
                candidates.append({"slack_user_id": sid, "display_name": real})
        match = resolve_owner_hint(hint_text=display_name, allowed_owners=candidates)
        if match is not None:
            slack_user_id = match["slack_user_id"]
            # Re-pick the canonical display_name for the row.
            for e in known_employees:
                if e.get("slack_user_id") == slack_user_id:
                    display_name = e.get("display_name") or e.get("real_name") or slack_user_id
                    break

    # Hallucination guard (FR-CR-04-22): the LLM occasionally returns the
    # author's full real name (e.g. "Андре Кузьминых") as `display_name`
    # even though the prompt forbids self-assignment, and even though the
    # message uses "we"/"мы" with no explicit doer. When the unresolved
    # name (a) doesn't appear anywhere in the source / context and
    # (b) the author IS in the employees table, we drop the
    # hallucinated name so the downstream "quiet author fallback" kicks
    # in instead of producing an unanswerable "I couldn't find X" prompt.
    if (
        not slack_user_id
        and display_name
        and author_user_id
        and known_employees
    ):
        author_in_table = any(
            e.get("slack_user_id") == author_user_id for e in known_employees
        )
        if author_in_table and not _name_present(
            display_name, source_text=source_text, context_messages=context_messages
        ):
            display_name = None

    # FR-CR-05-108 — operator: «проверь как вызывается поиск
    # контакта». Log what the owner stage actually saw and
    # decided so the operator can grep `owner_node_result`
    # in the container logs to diagnose mis-attributions.
    log.info(
        "owner_node_result",
        author_user_id=author_user_id,
        candidate_count=len(known_employees),
        candidate_ids=[
            e.get("slack_user_id") for e in known_employees[:20]
        ],
        candidate_roles=[
            f"{e.get('display_name') or '?'}={e.get('role') or '-'}"
            for e in known_employees[:20]
        ],
        llm_picked_uid=data.get("slack_user_id"),
        llm_picked_name=data.get("display_name"),
        llm_reasoning=(data.get("reasoning") or "")[:200],
        final_uid=slack_user_id,
        final_name=display_name,
    )
    return {
        "owner_user_id": slack_user_id,
        "owner_display_name": display_name,
    }


def _name_present(
    name: str, *, source_text: str, context_messages: list[dict]
) -> bool:
    """True iff `name` (or any of its space-separated tokens of length >= 3)
    appears, case-insensitively, in the source message or any context line.
    Used to tell "the LLM extracted a real name from the text" from "the
    LLM hallucinated the author's name from the metadata header"."""
    needle = (name or "").strip().lower()
    if not needle:
        return False
    haystacks: list[str] = [source_text or ""]
    for m in context_messages or []:
        t = m.get("text") or ""
        if t:
            haystacks.append(t)
    blob = " \n ".join(haystacks).lower()
    if needle in blob:
        return True
    # Token match: any 3+ char fragment of the name appearing in text.
    for token in needle.split():
        if len(token) >= 3 and token in blob:
            return True
    return False


_TEMPORAL_ANCHOR_RE = __import__("re").compile(
    # FR-CR-05-112 — operator: «либо в описание добавляй пруф,
    # либо сегодня». An LLM-extracted due_date is accepted
    # only when the source text contains an explicit temporal
    # anchor near the date phrase. Otherwise the «date» is
    # likely a meeting/event time, not a task deadline (e.g.
    # «встреча в понедельник» — Monday is the meeting slot,
    # not the deadline for setting up the meeting).
    r"(?:^|[\s,.])"
    r"("
    # Russian deadline anchors
    r"к\s+\d|к\s+(?:понедельник|вторник|сред|четверг|пятниц|субботе|воскресенье|концу)|"
    r"до\s+\d|до\s+(?:понедельник|вторник|сред|четверг|пятниц|субботе|воскресенье|конца)|"
    r"к\s+(?:января|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)|"
    r"до\s+(?:января|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)|"
    r"дедлайн|"
    r"крайн[ие]й\s+срок|"
    r"срок\s+до|"
    r"завтра|послезавтра|сегодня|"
    # English deadline anchors
    r"by\s+\d|by\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|end\s+of)|"
    r"by\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"before\s+\d|before\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"deadline|due\s+(?:by|on)|tomorrow|today"
    r")",
    flags=__import__("re").IGNORECASE | __import__("re").UNICODE,
)


def _has_explicit_temporal_anchor(source_text: str) -> bool:
    """FR-CR-05-112 — return True when the source text contains
    a deadline-shaped temporal anchor («к понедельнику», «до
    5 мая», «by Friday», «дедлайн», «завтра», etc.). When
    False, an LLM-extracted due_date is likely a meeting-slot
    date and gets dropped."""
    if not source_text:
        return False
    return _TEMPORAL_ANCHOR_RE.search(source_text) is not None


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
            # FR-CR-05-218 — single-task path: chunk == full source,
            # but still hand chat history so anchors like «как
            # договорились в чате» resolve.
            prior_context=state.get("context_messages"),
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

    # FR-CR-05-112 — operator: «либо в описание добавляй
    # пруф либо сегодня». Drop the LLM-picked date when the
    # source text doesn't contain an explicit temporal anchor
    # («к понедельнику», «до 5 мая», «by Friday», «дедлайн»,
    # «завтра», etc.). Without an anchor, the date the LLM
    # extracted is almost always a meeting/event slot
    # (operator regression: «понедельник или вторник» = slot
    # for the meeting being set up, not the deadline for
    # setting it up).
    if picked is not None and not _has_explicit_temporal_anchor(source_text):
        log.info(
            "date_node_dropped_no_temporal_anchor",
            picked=picked.isoformat(),
            source_excerpt=(source_text or "")[:200],
        )
        rejected = picked.isoformat()
        picked = None

    source_used = "llm"
    if picked is None:
        # FR-CR-05-101 — when the LLM call succeeded with an
        # explicit null + reasoning, TRUST it. Operator
        # regression: the LLM correctly judged «8 мая» as a
        # meeting-slot date (not the task's deadline) and
        # returned null with reasoning, but the Python
        # fallback re-extracted «8 мая» from the source text
        # and emitted 2026-05-08, undoing the FR-CR-05-87/89
        # filtering. Same pattern hallucinated 2027-04-30
        # for sources mentioning «30 апреля» (today). Skip
        # fallback whenever the LLM provided ANY reasoning —
        # that means the call succeeded and the null is
        # intentional, not an infrastructure error.
        llm_was_intentional = (
            llm_iso is None
            and rejected is None
            and isinstance(llm_reasoning, str)
            and llm_reasoning.strip()
        )
        if not llm_was_intentional:
            picked = resolve_due_date(source_text, today)
            source_used = "python_fallback" if picked else "none"
        else:
            source_used = "llm_null"

    # FR-CR-05-218 — operator policy «дедлайн должен стоять в
    # любом случае»: when no anchor was found in source, chat
    # history, or by the python resolver, fall back to TODAY
    # rather than emitting None. The proof-quote rule still
    # applies upstream — by the time we get here the LLM
    # already had its chance to find an anchor.
    if picked is None:
        picked = today
        source_used = "today_fallback"

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


def _extract_one_task(
    *,
    backend,
    chunk_text: str,
    context_messages: list[dict],
    author_user_id: str | None,
    today: _date,
    date_model: str | None,
    title_model: str | None,
    known_employees: list[dict],
    full_source: str | None = None,
) -> TaskDraft:
    """Run the 2a / 2b / 2c stages on a single chunk and assemble a
    ``TaskDraft``. Used both by ``node_assemble`` (per chunk for the
    multi-task path) and reused for the single-task path."""
    # Title / description / priority.
    data_t = _safe_call_tool(
        backend,
        system_prompt=TITLE_SYSTEM_PROMPT,
        user_prompt=build_title_user_prompt(
            source_text=chunk_text,
            context_messages=context_messages,
        ),
        tool_name=TITLE_TOOL_NAME,
        tool_description=TITLE_TOOL_DESCRIPTION,
        tool_parameters=TITLE_TOOL_PARAMETERS,
        model=title_model,
    ) or {}
    # FR-CR-05-216 — keep timeframe in title + full description (no strip).
    title = (data_t.get("title") or chunk_text[:120]).strip() or "(untitled)"
    raw_desc = data_t.get("description")
    description = (
        raw_desc.strip() if isinstance(raw_desc, str) and raw_desc.strip() else None
    )
    priority = data_t.get("priority") or "medium"

    # Owner.
    data_o = _safe_call_tool(
        backend,
        system_prompt=OWNER_SYSTEM_PROMPT,
        user_prompt=build_owner_user_prompt(
            source_text=chunk_text,
            context_messages=context_messages,
            author_user_id=author_user_id,
            known_employees=known_employees,
        ),
        tool_name=OWNER_TOOL_NAME,
        tool_description=OWNER_TOOL_DESCRIPTION,
        tool_parameters=OWNER_TOOL_PARAMETERS,
    ) or {}
    slack_user_id = data_o.get("slack_user_id") or None
    display_name = data_o.get("display_name") or None
    if known_employees and slack_user_id:
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        if slack_user_id not in valid_ids:
            slack_user_id = None
    if not slack_user_id and display_name and known_employees:
        from app.services.owners import resolve_owner_hint

        candidates: list[dict[str, str]] = []
        for e in known_employees:
            sid = e.get("slack_user_id")
            if not sid:
                continue
            primary = e.get("display_name") or sid
            candidates.append({"slack_user_id": sid, "display_name": primary})
            real = e.get("real_name")
            if real and real != primary:
                candidates.append({"slack_user_id": sid, "display_name": real})
        match = resolve_owner_hint(hint_text=display_name, allowed_owners=candidates)
        if match is not None:
            slack_user_id = match["slack_user_id"]
            for e in known_employees:
                if e.get("slack_user_id") == slack_user_id:
                    display_name = e.get("display_name") or e.get("real_name") or slack_user_id
                    break
    if (
        not slack_user_id
        and display_name
        and author_user_id
        and known_employees
    ):
        author_in_table = any(
            e.get("slack_user_id") == author_user_id for e in known_employees
        )
        if author_in_table and not _name_present(
            display_name, source_text=chunk_text, context_messages=context_messages
        ):
            display_name = None

    # Date.
    data_d = _safe_call_tool(
        backend,
        system_prompt=DATE_SYSTEM_PROMPT,
        user_prompt=build_date_user_prompt(
            source_text=chunk_text,
            current_date=today.isoformat(),
            current_weekday=today.strftime("%A"),
            # FR-CR-05-218 — multi-task path: hand the LLM the
            # whole message so it can resolve dependencies like
            # «а до этого пусть X сделает Y», and the chat
            # history for «как договорились» style anchors.
            wider_source=full_source,
            prior_context=context_messages,
        ),
        tool_name=DATE_TOOL_NAME,
        tool_description=DATE_TOOL_DESCRIPTION,
        tool_parameters=DATE_TOOL_PARAMETERS,
        model=date_model,
    ) or {}
    llm_iso = data_d.get("due_date")
    picked: _date | None = None
    if isinstance(llm_iso, str) and llm_iso:
        try:
            parsed = _date.fromisoformat(llm_iso)
        except ValueError:
            parsed = None
        # FR-CR-05-218 — anchor check against the wider source too,
        # because the chunk may not state «к/до …» itself but the
        # full message does. Same for date-is-acceptable.
        anchor_text = full_source or chunk_text
        if (
            parsed is not None
            and _date_is_acceptable(parsed, anchor_text, today)
        ):
            picked = parsed
    if picked is None:
        picked = resolve_due_date(chunk_text, today) or resolve_due_date(
            full_source or "", today
        )
    # FR-CR-05-218 — operator: «дедлайн должен стоять в любом случае».
    if picked is None:
        picked = today

    return TaskDraft(
        title=title,
        description=description,
        priority=priority,
        owner_user_id=slack_user_id,
        owner_display_name=display_name,
        due_date=picked,
    )


def node_assemble(state: IntentState) -> dict[str, Any]:
    if not state.get("is_task"):
        return {
            "classification": IntentClassification(
                intent=IntentType.no_action,
                confidence=float(state.get("detect_confidence", 0.0)),
                reasoning=state.get("detect_reasoning"),
            )
        }
    chunks: list[str] = state.get("task_chunks") or []
    confidence = float(state.get("detect_confidence", 0.75))
    reasoning = state.get("detect_reasoning")

    if not chunks:
        # Single-task path: re-use the per-stage outputs already
        # produced by `describe` / `owner` / `date` nodes.
        title = state.get("title") or state["source_text"][:120] or "(untitled)"
        return {
            "classification": IntentClassification(
                intent=IntentType.create_task,
                confidence=confidence,
                reasoning=reasoning,
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

    # Multi-task path (FR-CR-05-05): re-run 2a / 2b / 2c per chunk.
    # The fan-out single-task stages already ran, but their output
    # was based on the whole message — discard and recompute per
    # chunk. This costs a few extra LLM calls (Nx for N chunks) but
    # keeps each task's owner / date crisp.
    tasks: list[TaskDraft] = []
    for chunk in chunks:
        tasks.append(
            _extract_one_task(
                backend=state["backend"],
                chunk_text=chunk,
                context_messages=state.get("context_messages") or [],
                author_user_id=state.get("author_user_id"),
                today=state["today"],
                date_model=state.get("date_model"),
                title_model=state.get("title_model"),
                known_employees=state.get("known_employees") or [],
                # FR-CR-05-218 — pass the WHOLE message so the chunk's
                # date stage can resolve cross-chunk dependencies
                # («а до этого Андрей …» refers to the meeting slot
                # in chunk 1).
                full_source=state["source_text"],
            )
        )
    return {
        "classification": IntentClassification(
            intent=IntentType.create_task,
            confidence=confidence,
            reasoning=reasoning,
            tasks=tasks,
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
    title_model: str | None = None,
    known_employees: list[dict] | None = None,
) -> IntentClassification:
    initial: IntentState = {
        "backend": backend,
        "source_text": source_text,
        "context_messages": context_messages,
        "author_user_id": author_user_id,
        "today": today,
        "date_model": date_model,
        "title_model": title_model,
        "known_employees": known_employees or [],
    }
    final = _GRAPH.invoke(initial)
    return final["classification"]


__all__ = ["run_pipeline", "IntentState"]
