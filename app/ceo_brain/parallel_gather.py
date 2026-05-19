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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
) -> str:
    """One `beta.messages.create` with a SINGLE MCP server in
    `mcp_servers`. Returns harvested text (empty string on error)."""
    name = server.get("name") or "?"
    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "betas": ["mcp-client-2025-04-04"],
            "mcp_servers": [server],
            "messages": [{"role": "user", "content": sub_question}],
        }
        if extra_system:
            kwargs["system"] = [
                {"type": "text", "text": extra_system}
            ]
        resp = anthropic_client.beta.messages.create(**kwargs)
        text = _harvest_response_text(resp)
        log.info(
            "ceo_brain_parallel_gather_ok",
            mcp=name, chars=len(text),
        )
        return text
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_parallel_gather_failed",
            mcp=name, error=str(e), error_type=type(e).__name__,
        )
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
) -> dict[str, str]:
    """Fire one Anthropic+MCP call per MCP server in parallel.
    Returns ``{mcp_name: harvested_text}``. Empty string on
    per-server failure.

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
        for fut in as_completed(future_to_name):
            name = future_to_name[fut]
            try:
                out[name] = fut.result() or ""
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


__all__ = [
    "gather_from_mcps",
    "synthesize_final_answer",
]
