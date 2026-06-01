"""FR-CB2-3.30 — per-MCP parallel gather architecture.

The previous flow (one `beta.messages.create` with all 6 MCP servers
in the request) is bottlenecked by Anthropic's parallel handshake
to all servers up-front; if any one stalls, the whole request hangs
2-5 minutes. Per the operator («определи куда ходить и оттуда верни
параллельно данные»), this module:

1. Takes a list of picked MCP servers (from the FR-CB2-3.25
   classifier) plus the operator's question.
2. Fires N parallel `beta.messages.create` calls — ONE server per
   call — through `concurrent.futures.ThreadPoolExecutor`. Each
   inner call's handshake is single-server, so it can't trip the
   parallel-handshake race.
3. Harvests text from each response (model's `text` blocks +
   `mcp_tool_result.content` blocks).
4. Returns a `{mcp_name: harvested_text}` map. Failures are caught
   per-MCP — one server's timeout doesn't take down the others.

A separate `synthesize_final_answer()` helper accepts the gathered
map and the operator's question, then runs ONE plain
`messages.create` (no MCP, no betas) with the data inlined. This
final synthesis is reliable because there's no MCP handshake to
fail.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


_DEFAULT_GATHER_MAX_TOKENS = 4096
_DEFAULT_SYNTH_MAX_TOKENS = 4096


def _harvest_response_text(resp: Any) -> str:
    """Pull every text-bearing block from a Messages response.
    Includes top-level `text` blocks AND `mcp_tool_result.content`
    sub-blocks (the actual tool output). Mirrors
    `responder._harvest_tool_result_text` but operates on a
    single response object instead of an accumulated stream."""
    if resp is None:
        return ""
    parts: list[str] = []
    for b in (getattr(resp, "content", None) or []):
        btype = getattr(b, "type", None) or (
            isinstance(b, dict) and b.get("type")
        )
        if btype == "text":
            txt = getattr(b, "text", None) or (
                isinstance(b, dict) and b.get("text") or ""
            )
            if txt:
                parts.append(str(txt))
        elif btype in {"mcp_tool_result", "tool_result"}:
            cont = getattr(b, "content", None)
            if isinstance(cont, str):
                parts.append(cont)
            elif cont is not None:
                for sub in cont:
                    sub_txt = getattr(sub, "text", None) or (
                        isinstance(sub, dict)
                        and sub.get("text") or ""
                    )
                    if sub_txt:
                        parts.append(str(sub_txt))
    return "\n\n".join(parts).strip()


def _call_single_mcp(
    *,
    anthropic_client: Any,
    server: dict[str, Any],
    sub_question: str,
    model: str,
    max_tokens: int,
    extra_system: str | None = None,
    max_attempts: int = 2,
) -> str:
    """One `beta.messages.create` with a SINGLE MCP server in
    `mcp_servers`. Returns harvested text (empty string on error
    after retries).

    Retries once on transient errors (APITimeoutError, MCP
    handshake glitch) — single-MCP calls can occasionally hit
    cold-start latency or transient timeouts on the n8n side.
    """
    name = server.get("name") or "?"
    last_error: str = ""
    for attempt in range(1, max_attempts + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "betas": ["mcp-client-2025-04-04"],
                "mcp_servers": [server],
                "messages": [{"role": "user", "content": sub_question}],
                # Longer per-call timeout for the parallel gather —
                # individual MCPs can legitimately take 60-120 sec
                # to fetch + return large transcripts.
                "timeout": 120.0,
            }
            if extra_system:
                kwargs["system"] = [
                    {"type": "text", "text": extra_system}
                ]
            resp = anthropic_client.beta.messages.create(**kwargs)
            text = _harvest_response_text(resp)
            log.info(
                "ceo_brain_parallel_gather_ok",
                mcp=name, chars=len(text), attempt=attempt,
            )
            return text
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {str(e)[:150]}"
            transient = (
                type(e).__name__ == "APITimeoutError"
                or "timed out" in str(e).lower()
                or "connection error while communicating with mcp" in str(e).lower()
            )
            if transient and attempt < max_attempts:
                log.info(
                    "ceo_brain_parallel_gather_retry",
                    mcp=name, attempt=attempt, error=last_error,
                )
                continue
            log.warning(
                "ceo_brain_parallel_gather_failed",
                mcp=name, error=last_error,
                error_type=type(e).__name__, attempt=attempt,
            )
            return ""
    return ""


def gather_from_mcps(
    *,
    question: str,
    picked_mcps: list[dict[str, Any]],
    anthropic_client: Any,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = _DEFAULT_GATHER_MAX_TOKENS,
    extra_system: str | None = None,
    sub_questions: dict[str, str] | None = None,
    per_call_wall_timeout: float = 90.0,
) -> dict[str, str]:
    """Fire one Anthropic+MCP call per MCP server in parallel.
    Returns ``{mcp_name: harvested_text}``. Empty string on
    per-server failure OR on hard wall-timeout.

    ``per_call_wall_timeout`` — hard upper bound (sec) we wait for
    each future. Anthropic SDK's own `timeout` kwarg isn't honoured
    for MCP-bearing calls (operator-observed 2026-05-20: a single
    call hung 6 min despite `timeout=120`), so we enforce it on the
    future. The underlying HTTP request can't be cancelled, but we
    stop waiting and treat the MCP as empty so the rest of the
    gather + synthesis can proceed.

    ``sub_questions`` lets the caller pass a per-MCP refined query
    (from the classifier); otherwise the operator's original
    question is used for every server.
    """
    if not picked_mcps:
        return {}
    workers = min(len(picked_mcps), 8)
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_name: dict = {}
        for srv in picked_mcps:
            name = srv.get("name") or "?"
            sub_q = (sub_questions or {}).get(name) or question
            fut = ex.submit(
                _call_single_mcp,
                anthropic_client=anthropic_client,
                server=srv,
                sub_question=sub_q,
                model=model,
                max_tokens=max_tokens,
                extra_system=extra_system,
            )
            future_to_name[fut] = name
        for fut in as_completed(future_to_name, timeout=None):
            name = future_to_name[fut]
            try:
                out[name] = fut.result(
                    timeout=per_call_wall_timeout,
                ) or ""
            except FuturesTimeout:
                log.warning(
                    "ceo_brain_parallel_gather_future_timeout",
                    mcp=name, wall_timeout=per_call_wall_timeout,
                )
                out[name] = ""
                # Best-effort cancel (no-op once thread is running,
                # but at least frees the future slot).
                fut.cancel()
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ceo_brain_parallel_gather_future_raised",
                    mcp=name, error=str(e),
                )
                out[name] = ""
    return out


def synthesize_final_answer(
    *,
    question: str,
    gathered: dict[str, str],
    anthropic_client: Any,
    system_prompt_text: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = _DEFAULT_SYNTH_MAX_TOKENS,
) -> str:
    """One plain `messages.create` (no MCP, no beta) with the
    gathered tool data inlined. The model writes a clean answer
    from the data alone, no risk of MCP handshake hang.

    Retries on transient errors (429 rate limit, APITimeoutError,
    connection drops) up to 3 attempts with exponential backoff —
    Anthropic's org-level 30K-input-tok/min limit can briefly
    block synthesis when prior tool results were large.
    """
    import time as _time
    data_sections: list[str] = []
    for name, text in (gathered or {}).items():
        if text and text.strip():
            data_sections.append(
                f"=== {name} ===\n{text.strip()}"
            )
    data_block = (
        "\n\n".join(data_sections) if data_sections else "(нет данных)"
    )
    # FR-CB2-3.31 hotfix #8 — cap DATA at ~80K chars so we stay
    # under Anthropic org's 30K-input-tok/min rate limit. One
    # synthesis call burns input_tokens = (system + data + user
    # prompt) tokens; 80K chars ≈ 20K tokens, leaving headroom
    # for retries / concurrent requests.
    if len(data_block) > 80_000:
        data_block = (
            data_block[:80_000]
            + f"\n…[truncated {len(data_block) - 80_000} chars]"
        )
    user_msg = (
        "Ниже — собранные данные из tool-вызовов:\n\n"
        f"<DATA>\n{data_block}\n</DATA>\n\n"
        f"ОРИГИНАЛЬНЫЙ ВОПРОС ОПЕРАТОРА: {question}\n\n"
        "Напиши прямой ответ на оригинальный вопрос. Без преамбул. "
        "Не вызывай tools. Если данных недостаточно — честно скажи "
        "что именно отсутствует."
    )
    last_error: str = ""
    for attempt in range(1, 4):
        try:
            resp = anthropic_client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system_prompt_text}],
                messages=[{"role": "user", "content": user_msg}],
            )
            return _harvest_response_text(resp)
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {str(e)[:200]}"
            err_text = str(e).lower()
            transient = (
                "429" in str(e)
                or "rate_limit" in err_text
                or "timed out" in err_text
                or type(e).__name__ in {
                    "APITimeoutError", "RateLimitError",
                }
            )
            if transient and attempt < 3:
                wait = 8 * attempt  # 8, 16 sec
                log.info(
                    "ceo_brain_synthesis_retry",
                    attempt=attempt, wait=wait, error=last_error,
                )
                _time.sleep(wait)
                continue
            log.warning(
                "ceo_brain_synthesis_failed",
                error=last_error, error_type=type(e).__name__,
            )
            return ""
    log.warning(
        "ceo_brain_synthesis_failed",
        error=last_error,
    )
    return ""


_ID_REGEXES = [
    # Zoom transcripts return: "Zoom ID: fs/KyHH5RL2x2oEeFjNC3Q=="
    (re.compile(r"Zoom ID:\s*([A-Za-z0-9+/=_-]+)"), "zoom_id"),
    # search_meetings returns: "ID: 6ce44e03-6c5d-4c52-ba24-..."
    (re.compile(r"\bID:\s*([A-Za-z0-9+/=_-]+)"), "meeting_id"),
]


_BAD_ID_TOKENS = {
    "undefined", "null", "none", "n/a", "tbd", "<id>", "<null>", "",
}


def _extract_ids_from_responses(
    pass1_results: dict[str, str],
) -> dict[str, list[str]]:
    """Parse n8n search responses for IDs. Returns
    ``{kind: [id1, id2, ...]}`` where kind is `zoom_id` or
    `meeting_id` — the arg key the dependent tool expects.

    Filters out placeholder tokens like `undefined` / `null` (n8n
    sometimes returns these when the meeting record has no proper
    id — calling get_zoom_transcript with `undefined` just returns
    «не найден»).
    """
    out: dict[str, list[str]] = {"zoom_id": [], "meeting_id": []}
    for body in pass1_results.values():
        if not body:
            continue
        for regex, kind in _ID_REGEXES:
            for m in regex.finditer(body):
                val = (m.group(1) or "").strip()
                if not val or val.lower() in _BAD_ID_TOKENS:
                    continue
                if len(val) < 6:
                    # Too short to be a real Zoom/meeting id.
                    continue
                if val not in out[kind]:
                    out[kind].append(val)
    return out


def _needs_id_fill(call: dict) -> str | None:
    """Returns the arg key (`zoom_id` or `meeting_id`) that should
    be auto-filled if missing. None if not a transcript-fetch."""
    tool = (call.get("tool") or "").lower()
    args = call.get("args") or {}
    if tool == "get_zoom_transcript" and not args.get("zoom_id"):
        return "zoom_id"
    if tool == "get_meeting" and not args.get("meeting_id"):
        return "meeting_id"
    return None


def gather_via_direct_http(
    *,
    planned_calls: list[dict[str, Any]],
    mcp_servers: list[dict[str, Any]],
    tools_by_mcp: dict[str, list[dict]] | None = None,
    per_call_timeout: float = 30.0,
    local_tool_executors: dict[str, Any] | None = None,
) -> dict[str, str]:
    """FR-CB2-3.31 — execute planned tool calls in parallel via
    direct HTTP to n8n MCP endpoints, completely bypassing
    Anthropic-MCP.

    Returns ``{label: harvested_text}``. Labels are uniquified with
    a counter (``mcp_name::tool_name#N``) so multiple calls of the
    same tool don't collide and lose data.

    Each individual call goes through `mcp_client.call_tool`,
    which has its own session caching + retry + schema-based args
    coercion (FR-CB2-3.31 hotfix: n8n needs `limit:"5"`, planner
    often produces `limit:5` int → schema validation 400). Threads
    + futures add a hard wall-timeout so a single misbehaving call
    can't block the entire gather.
    """
    from app.ceo_brain.mcp_client import call_tool

    name_to_url = {s.get("name"): s.get("url") for s in (mcp_servers or [])}
    schemas_by_name: dict[tuple[str, str], dict] = {}
    if tools_by_mcp:
        for mcp_name, tools in tools_by_mcp.items():
            for t in tools or []:
                schemas_by_name[(mcp_name, t.get("name") or "")] = (
                    t.get("inputSchema") or {}
                )
    if not planned_calls:
        return {}

    def _runner(call: dict[str, Any], idx: int) -> tuple[str, str]:
        mcp_name = call.get("mcp") or "?"
        tool_name = call.get("tool") or "?"
        args = call.get("args") or {}
        # Unique label per planned call to avoid bucket collision.
        label = f"{mcp_name}::{tool_name}#{idx}"
        # FR-CB2-3.32 / FR-CB2-4.7 / FR-TV — virtual `slack_self` / `jira_self`
        # / `tasks_self` MCPs dispatch to a local Python executor
        # (build_executors output), no HTTP roundtrip.
        if mcp_name in ("slack_self", "jira_self", "tasks_self"):
            execs = local_tool_executors or {}
            executor = execs.get(tool_name)
            if executor is None:
                log.warning(
                    "ceo_brain_local_executor_missing",
                    tool=tool_name,
                )
                return label, ""
            try:
                result = executor(args)
                return label, str(result) if result else ""
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ceo_brain_local_executor_failed",
                    tool=tool_name, error=str(e),
                )
                return label, ""
        url = name_to_url.get(mcp_name)
        if not url:
            return label, ""
        schema = schemas_by_name.get((mcp_name, tool_name))
        ok, body = call_tool(
            url=url, tool_name=tool_name, arguments=args,
            timeout=per_call_timeout,
            input_schema=schema,
        )
        return label, body if ok else ""

    def _run_calls_parallel(
        calls_with_idx: list[tuple[int, dict]],
    ) -> dict[str, str]:
        bucket: dict[str, str] = {}
        if not calls_with_idx:
            return bucket
        workers = min(len(calls_with_idx), 8)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(_runner, c, i) for i, c in calls_with_idx
            ]
            for fut in as_completed(futures, timeout=None):
                try:
                    label, text = fut.result(
                        timeout=per_call_timeout + 5
                    )
                    bucket[label] = text
                except FuturesTimeout:
                    log.warning("ceo_brain_direct_http_future_timeout")
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "ceo_brain_direct_http_future_raised",
                        error=str(e),
                    )
        return bucket

    # FR-CB2-3.31 hotfix #5 — split into 2 phases by dependency:
    #   pass 1: everything without missing required ID
    #   pass 2: transcript-fetches that need IDs extracted from
    #           pass-1 results
    pass1: list[tuple[int, dict]] = []
    pass2: list[tuple[int, dict]] = []
    for i, c in enumerate(planned_calls):
        if _needs_id_fill(c):
            pass2.append((i, c))
        else:
            pass1.append((i, c))

    out: dict[str, str] = {}
    out.update(_run_calls_parallel(pass1))

    if pass2:
        ids_pool = _extract_ids_from_responses(out)
        log.info(
            "ceo_brain_direct_http_pass2_ids",
            zoom_ids=ids_pool.get("zoom_id"),
            meeting_ids=ids_pool.get("meeting_id"),
            pass2_count=len(pass2),
        )
        filled_pass2: list[tuple[int, dict]] = []
        for idx, c in pass2:
            kind = _needs_id_fill(c)
            if not kind:
                filled_pass2.append((idx, c))
                continue
            pool = ids_pool.get(kind) or []
            if not pool:
                log.info(
                    "ceo_brain_direct_http_skip_no_id",
                    tool=c.get("tool"), need=kind,
                )
                label = f"{c.get('mcp')}::{c.get('tool')}#{idx}"
                out[label] = ""
                continue
            # FR-CB2-3.31 hotfix #7 — fan out across top-3 IDs in
            # parallel instead of picking only the first one. n8n's
            # search returns by score, but the highest-score
            # meeting can be a 1-min empty placeholder while the
            # real 60-min one is third in the list. Trying multiple
            # in parallel + keeping all non-empty results lets
            # synthesis use whichever has real content.
            base_label = f"{c.get('mcp')}::{c.get('tool')}#{idx}"
            for j, val in enumerate(pool[:3]):
                filled = dict(c)
                filled["args"] = {
                    **(c.get("args") or {}), kind: val,
                }
                # Use sub-idx to keep labels unique.
                sub_idx = idx + (j * 100)
                filled_pass2.append((sub_idx, filled))
                log.info(
                    "ceo_brain_direct_http_filled_id",
                    tool=c.get("tool"), kind=kind, value=val,
                    candidate=j + 1, of=min(len(pool), 3),
                )
        out.update(_run_calls_parallel(filled_pass2))

    # FR-CB2-3.31 hotfix #6 — auto-inject get_zoom_transcript when
    # search calls produced zoom_ids but planner did NOT include any
    # transcript fetch. Cap at 2 to keep latency bounded.
    has_any_transcript = any(
        (c.get("tool") or "").lower() in {"get_zoom_transcript", "get_meeting"}
        for c in planned_calls
    )
    if not has_any_transcript:
        ids_pool = _extract_ids_from_responses(out)
        zoom_ids = ids_pool.get("zoom_id") or []
        if zoom_ids:
            calendar_mcp = next(
                (
                    c.get("mcp") for c in planned_calls
                    if (c.get("tool") or "").startswith("search_zoom_meetings")
                ),
                None,
            )
            if calendar_mcp:
                base_idx = len(planned_calls)
                injected: list[tuple[int, dict]] = []
                # Try top-3 zoom_ids in parallel (FR-CB2-3.31
                # hotfix #7). Synthesis uses whichever has real
                # content; empty/short responses get ignored.
                for j, zid in enumerate(zoom_ids[:3]):
                    injected.append((
                        base_idx + j,
                        {
                            "mcp": calendar_mcp,
                            "tool": "get_zoom_transcript",
                            "args": {"zoom_id": zid},
                        },
                    ))
                log.info(
                    "ceo_brain_direct_http_auto_transcript",
                    zoom_ids=zoom_ids[:3],
                )
                out.update(_run_calls_parallel(injected))

    return out


__all__ = [
    "gather_from_mcps",
    "gather_via_direct_http",
    "synthesize_final_answer",
]
