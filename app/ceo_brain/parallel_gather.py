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
    from the data alone, no risk of MCP handshake hang."""
    data_sections: list[str] = []
    for name, text in (gathered or {}).items():
        if text and text.strip():
            data_sections.append(
                f"=== {name} ===\n{text.strip()}"
            )
    data_block = (
        "\n\n".join(data_sections) if data_sections else "(нет данных)"
    )
    user_msg = (
        "Ниже — собранные данные из tool-вызовов:\n\n"
        f"<DATA>\n{data_block}\n</DATA>\n\n"
        f"ОРИГИНАЛЬНЫЙ ВОПРОС ОПЕРАТОРА: {question}\n\n"
        "Напиши прямой ответ на оригинальный вопрос. Без преамбул. "
        "Не вызывай tools. Если данных недостаточно — честно скажи "
        "что именно отсутствует."
    )
    try:
        resp = anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system_prompt_text}],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_synthesis_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return ""
    return _harvest_response_text(resp)


def gather_via_direct_http(
    *,
    planned_calls: list[dict[str, Any]],
    mcp_servers: list[dict[str, Any]],
    per_call_timeout: float = 30.0,
) -> dict[str, str]:
    """FR-CB2-3.31 — execute planned tool calls in parallel via
    direct HTTP to n8n MCP endpoints, completely bypassing
    Anthropic-MCP.

    Returns ``{label: harvested_text}`` where label is
    ``"<mcp_name>::<tool_name>"`` so multiple calls to the same MCP
    don't collide.

    Each individual call goes through `mcp_client.call_tool`,
    which has its own session caching + retry. Threads + futures
    add a hard wall-timeout so a single misbehaving call can't
    block the entire gather.
    """
    from app.ceo_brain.mcp_client import call_tool

    name_to_url = {s.get("name"): s.get("url") for s in (mcp_servers or [])}
    out: dict[str, str] = {}
    if not planned_calls:
        return out

    def _runner(call: dict[str, Any]) -> tuple[str, str]:
        mcp_name = call.get("mcp") or "?"
        tool_name = call.get("tool") or "?"
        args = call.get("args") or {}
        url = name_to_url.get(mcp_name)
        label = f"{mcp_name}::{tool_name}"
        if not url:
            return label, ""
        ok, body = call_tool(
            url=url, tool_name=tool_name, arguments=args,
            timeout=per_call_timeout,
        )
        return label, body if ok else ""

    workers = min(len(planned_calls), 8)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_runner, c) for c in planned_calls]
        for fut in as_completed(futures, timeout=None):
            try:
                label, text = fut.result(
                    timeout=per_call_timeout + 5
                )
                out[label] = text
            except FuturesTimeout:
                log.warning("ceo_brain_direct_http_future_timeout")
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ceo_brain_direct_http_future_raised",
                    error=str(e),
                )
    return out


__all__ = [
    "gather_from_mcps",
    "gather_via_direct_http",
    "synthesize_final_answer",
]
