"""FR-CB2-3.x — CEO Brain Bot Claude responder.

When the operator @mentions the bot or DMs it, this module forwards
the conversation to the Anthropic Messages API along with the list
of MCP servers configured for their claude.ai account. Claude
decides which tools to call (Slack, Gmail, Calendar, etc.) and
streams an answer back, which we surface in the same Slack thread
via repeated ``chat_update`` calls.

Build with the official ``anthropic`` SDK — never raw HTTP. Use
``client.messages.stream(...)`` so we get incremental tokens and
can render them in Slack as they arrive (FR-CB2-3.8).

Prompt caching: system prompt + frozen MCP-server list go in front
of every request with a ``cache_control`` breakpoint. Per-request
volatile content (the operator's question, thread history) sits
*after* the breakpoint so the cached prefix stays hot
(FR-CB2-3.7).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.ceo_brain.config import get_mcp_servers
from app.config import get_settings
from app.logging_setup import get_logger
from app.models import ClaudeResponderRun

log = get_logger(__name__)


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_THREAD_LIMIT = 10
SLACK_UPDATE_INTERVAL_SEC = 1.0  # Slack chat.update rate-limit headroom

# Per-1M-token output cost for the cost cap calculation in
# `build_anthropic_request`. Sonnet-4.6 output = $15 / Mtok.
_SONNET_46_OUTPUT_USD_PER_MTOK = 15.0


# -- dispatch logic --------------------------------------------------------


def should_respond(
    *,
    event_type: str,
    channel_id: str,
    text: str,
    bot_user_id: str | None = None,
    channel_type: str | None = None,
) -> bool:
    """FR-CB2-3.1/3.2/3.3 — yes for @mention OR DM; no otherwise."""
    if event_type == "app_mention":
        return True
    if event_type == "message" and (channel_type == "im" or (
        channel_id and channel_id.startswith("D")
    )):
        return True
    return False


# -- Slack placeholder + streaming updates ---------------------------------


def post_placeholder(
    *,
    slack: Any,
    channel: str,
    thread_ts: str | None = None,
    text: str = "🤔 думаю…",
) -> str | None:
    """FR-CB2-3.4 — post the placeholder under 1s. Returns the
    placeholder ``ts`` so the streaming loop can update it."""
    kwargs: dict[str, Any] = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    resp = slack.chat_postMessage(**kwargs)
    if not resp.get("ok"):
        return None
    return resp.get("ts")


# -- prompt construction ---------------------------------------------------


def build_system_prompt(*, today: datetime | None = None) -> str:
    """FR-CB2-3.14 — system prompt with persona + today's date."""
    today = today or datetime.now(timezone.utc)
    today_iso = today.strftime("%Y-%m-%d")
    return (
        f"Ты — CEO Brain Bot, личный ассистент Артема Соколова "
        f"(CEO humanoid.ai). Сегодня {today_iso}.\n\n"
        f"ОСНОВНОЕ ПРАВИЛО: на любой вопрос про данные оператора "
        f"(встречи, сообщения, письма, документы, заметки, контакты, "
        f"задачи, инвесторов и т.д.) ТЫ ОБЯЗАТЕЛЬНО ДЁРГАЕШЬ "
        f"соответствующий MCP-tool и отвечаешь на основе РЕАЛЬНЫХ "
        f"данных. Не отвечай по памяти, не предполагай, не описывай "
        f"свои возможности — сразу ищи. Только если данных правда нет "
        f"и tool вернул empty — честно скажи «не нашёл».\n\n"
        f"Стиль ответов:\n"
        f"- Кратко, по делу, без воды и без эмодзи.\n"
        f"- На русском, если оператор пишет на русском; иначе на "
        f"языке вопроса.\n"
        f"- В конце добавляй компактный список источников "
        f"(каналы/treads/события/файлы), которые ты использовал.\n\n"
        f"Когда оператор пишет короткое приветствие («привет», "
        f"«hi», «hey») без конкретного запроса — отвечай коротко "
        f"приветствием без описания capabilities."
    )


def build_thread_history(
    messages: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_THREAD_LIMIT,
) -> list[dict[str, Any]]:
    """FR-CB2-3.10 — keep the last N messages, format for Anthropic.

    Input items may carry ``user``, ``text``, ``ts`` (Slack shape) or
    already be in Anthropic ``{role, content}`` form. We normalise
    everything to ``{role: "user", content: <text>}`` and trust the
    Anthropic system prompt + Claude to disambiguate authorship —
    Slack already shows who said what in the surrounding context.
    """
    if not messages:
        return []
    last_n = messages[-limit:] if limit > 0 else list(messages)
    out: list[dict[str, Any]] = []
    for m in last_n:
        if isinstance(m, dict) and "role" in m and "content" in m:
            out.append({"role": m["role"], "content": m["content"]})
            continue
        text = (m or {}).get("text") or ""
        if not text:
            continue
        # Only prefix with a HUMAN-readable author label, never the
        # raw Slack `Uxxxx` user ID — that's noise in the prompt.
        author = ((m or {}).get("user_display_name") or "").strip()
        prefix = f"[{author}] " if author else ""
        out.append({"role": "user", "content": prefix + text})
    return out


def build_anthropic_request(
    *,
    thread_history: list[dict[str, Any]] | None = None,
    today: datetime | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """FR-CB2-3.5/3.6/3.7 + NFR-CB2-C.2 — build the Messages API
    request payload with:

    * model = ``claude-sonnet-4-6`` (env-override ``CEO_BRAIN_MODEL``)
    * MCP servers from ``MCP_SERVERS`` env JSON
    * Prompt caching breakpoint on the system prompt
    * ``max_tokens`` capped by ``CEO_BRAIN_MAX_RUN_COST_USD``
    """
    settings = get_settings()
    model = settings.ceo_brain_model or DEFAULT_MODEL
    mcp_servers = get_mcp_servers()
    sys_prompt_text = build_system_prompt(today=today)

    # NFR-CB2-C.2 — translate per-run USD cap into a `max_tokens`
    # ceiling. Output dominates Anthropic cost on agentic workloads.
    cap_usd = max(0.01, float(settings.ceo_brain_max_run_cost_usd))
    max_tokens_from_cap = int(
        (cap_usd / _SONNET_46_OUTPUT_USD_PER_MTOK) * 1_000_000
    )
    max_tokens = min(DEFAULT_MAX_TOKENS, max_tokens_from_cap) or 1

    history = thread_history or []
    if not history:
        # Anthropic Messages API requires ≥1 user message. Add a
        # noop kickoff so prompt-caching tests / cost cap tests
        # can build the payload without supplying thread context.
        history = [{"role": "user", "content": "(ping)"}]

    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": sys_prompt_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": history,
    }
    if mcp_servers:
        request["mcp_servers"] = mcp_servers
    if tools:
        request["tools"] = list(tools)
    return request


# -- streaming run ----------------------------------------------------------


def _stream_text_to_slack(
    *,
    slack: Any,
    channel: str,
    placeholder_ts: str,
    text_buffer: list[str],
    last_update: list[float],
    force: bool = False,
) -> None:
    """Batched ``chat_update`` — FR-CB2-3.8.

    Called from inside the stream loop. We update at most once per
    ``SLACK_UPDATE_INTERVAL_SEC`` so we don't burst past Slack's
    chat.update tier-1 rate-limit (~50/min)."""
    now = time.monotonic()
    if not force and (now - last_update[0] < SLACK_UPDATE_INTERVAL_SEC):
        return
    body = "".join(text_buffer).strip()
    if not body:
        return
    try:
        slack.chat_update(channel=channel, ts=placeholder_ts, text=body)
    except Exception as e:  # noqa: BLE001
        # Don't let a transient Slack hiccup kill the responder
        # — we'll retry on the next streamed chunk.
        log.warning("ceo_brain_chat_update_failed", error=str(e))
        return
    last_update[0] = now


def describe_tool_use_for_slack(
    *,
    tool_name: str,
    input_arg: dict[str, Any] | None = None,
) -> str:
    """FR-CB2-3.15 — render a tool_use event as a Slack-readable
    progress line. Used for the streaming "🔍 ищу в Calendar…"
    placeholder updates so the operator sees what Claude is doing
    in real time."""
    pretty = tool_name.replace("_", " ").replace(".", " · ").strip()
    bits = pretty.split(" · ", 1)
    if len(bits) == 2:
        server, action = bits
        server = server.title()
        return f"🔍 {server}: {action}"
    return f"🔍 {pretty}"


def format_final_response(
    *,
    text: str,
    tool_uses: list[dict[str, Any]] | None = None,
) -> str:
    """FR-CB2-3.9 — final assistant text + ``Sources:`` block
    listing the tool_uses that fired during the run."""
    body = (text or "").rstrip()
    if not tool_uses:
        return body or "_(пустой ответ от Claude)_"
    lines = [body, "", "_Sources:_"]
    seen: set[str] = set()
    for tu in tool_uses:
        name = (tu or {}).get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        inp = (tu or {}).get("input") or {}
        # Show the most informative single field if present.
        hint = ""
        if isinstance(inp, dict):
            for k in ("query", "q", "channel", "thread_ts", "subject"):
                v = inp.get(k)
                if v:
                    hint = f" `{v}`"
                    break
        lines.append(f"• `{name}`{hint}")
    return "\n".join(lines)


def _scrub_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """NFR-CB2-S.3 — never persist MCP OAuth tokens to DB.

    Drop the ``auth`` field from each ``mcp_servers`` entry before
    writing the payload to ``claude_responder_runs.request_payload``.
    """
    scrubbed = json.loads(json.dumps(payload, default=str))
    servers = scrubbed.get("mcp_servers")
    if isinstance(servers, list):
        for s in servers:
            if isinstance(s, dict):
                s.pop("auth", None)
                s.pop("authorization_token", None)
                s.pop("oauth_token", None)
                s.pop("access_token", None)
                s.pop("token", None)
    return scrubbed


def persist_run(
    session: Session,
    *,
    slack_channel_id: str,
    slack_event_ts: str,
    request_payload: dict[str, Any],
    response_text: str,
    tool_uses: list[dict[str, Any]] | None,
    status: str,
    cost_usd: float | Decimal | int = 0,
    slack_placeholder_ts: str | None = None,
    error: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> ClaudeResponderRun:
    """FR-CB2-3.11 / FR-CB2-4.5 — single row per responder attempt
    (success or failure). Payload is scrubbed of MCP auth tokens
    before storage (NFR-CB2-S.3)."""
    row = ClaudeResponderRun(
        slack_channel_id=slack_channel_id,
        slack_event_ts=slack_event_ts,
        slack_placeholder_ts=slack_placeholder_ts,
        request_payload=_scrub_request_payload(request_payload or {}),
        response_text=response_text,
        tool_uses=tool_uses or [],
        status=status,
        error=error,
        cost_usd=Decimal(str(cost_usd or 0)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        started_at=started_at,
        completed_at=completed_at,
    )
    session.add(row)
    session.flush()
    return row


_RETRYABLE_429_RE = re.compile(r"429|too many requests|rate.?limit", re.IGNORECASE)


def _is_429(exc: BaseException) -> bool:
    return bool(_RETRYABLE_429_RE.search(str(exc) or ""))


def _retry_after_seconds(exc: BaseException, default: float = 5.0) -> float:
    headers = getattr(exc, "headers", None) or {}
    if isinstance(headers, dict):
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    return default


def _serialise_content_blocks(blocks: Any) -> list[dict[str, Any]]:
    """Convert SDK content blocks back to the wire-shape required for
    the next ``messages`` turn. SDK blocks expose ``.model_dump()``;
    if not, fall back to a manual best-effort copy."""
    out: list[dict[str, Any]] = []
    for b in (blocks or []):
        if isinstance(b, dict):
            out.append(b)
            continue
        if hasattr(b, "model_dump"):
            try:
                out.append(b.model_dump(exclude_none=True))
                continue
            except Exception:  # noqa: BLE001
                pass
        d: dict[str, Any] = {"type": getattr(b, "type", "text")}
        for attr in ("text", "name", "input", "id"):
            v = getattr(b, attr, None)
            if v is not None:
                d[attr] = v
        out.append(d)
    return out


_FINAL_RENDER_MAX_ATTEMPTS = 3
_FINAL_RENDER_BACKOFF_SEC = (1.0, 2.0, 4.0)


def _is_slack_rate_limited(resp_or_exc: Any) -> tuple[bool, float]:
    """Inspect a Slack response dict OR a slack_sdk exception and
    return ``(is_rate_limited, retry_after_seconds)``."""
    # Dict-like response (ok=False, error="ratelimited")
    if isinstance(resp_or_exc, dict):
        err = (resp_or_exc.get("error") or "").lower()
        if err in {"ratelimited", "rate_limited"} or "rate" in err:
            ra = resp_or_exc.get("retry_after") or resp_or_exc.get(
                "Retry-After"
            ) or 1.0
            try:
                return True, max(0.0, float(ra))
            except (TypeError, ValueError):
                return True, 1.0
        return False, 0.0
    # slack_sdk SlackApiError exposes `.response` (dict-like).
    inner = getattr(resp_or_exc, "response", None)
    if isinstance(inner, dict) or hasattr(inner, "get"):
        try:
            return _is_slack_rate_limited(dict(inner))
        except Exception:  # noqa: BLE001
            pass
    # Generic exception — fall back to the message regex.
    if _is_429(resp_or_exc):
        return True, _retry_after_seconds(resp_or_exc, default=1.0)
    return False, 0.0


def _final_render_to_slack(
    *,
    slack: Any,
    channel: str,
    placeholder_ts: str,
    thread_ts: str | None,
    text: str,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """FR-CB2-3.18 — robust final placeholder update.

    On Slack `ratelimited`, retry with exponential backoff up to 3
    attempts. After exhaustion, post a NEW message in the same
    thread via ``chat_postMessage`` so the synthesized answer
    reaches the operator even when ``chat.update`` is exhausted
    (Tier-1 limit ≈ 50/min — easy to hit with multi-tool flows
    streaming many progressive edits).
    """
    last_failure_reason: str = ""
    for attempt in range(1, _FINAL_RENDER_MAX_ATTEMPTS + 1):
        backoff = _FINAL_RENDER_BACKOFF_SEC[
            min(attempt - 1, len(_FINAL_RENDER_BACKOFF_SEC) - 1)
        ]
        try:
            resp = slack.chat_update(
                channel=channel, ts=placeholder_ts, text=text,
            )
        except Exception as exc:  # noqa: BLE001
            is_rl, ra = _is_slack_rate_limited(exc)
            last_failure_reason = (
                f"exc:{type(exc).__name__}:{exc}"
            )
            if is_rl and attempt < _FINAL_RENDER_MAX_ATTEMPTS:
                sleep(max(ra, backoff))
                continue
            break
        # Slack returns dict-like; .get('ok')
        ok = bool(resp.get("ok") if hasattr(resp, "get") else False)
        if ok:
            return
        is_rl, ra = _is_slack_rate_limited(
            dict(resp) if hasattr(resp, "get") else {}
        )
        last_failure_reason = f"resp_not_ok:{resp.get('error') if hasattr(resp,'get') else resp}"
        if is_rl and attempt < _FINAL_RENDER_MAX_ATTEMPTS:
            sleep(max(ra, backoff))
            continue
        break

    # All retries exhausted — fall back to a fresh thread message.
    log.warning(
        "ceo_brain_final_render_chat_update_exhausted",
        reason=last_failure_reason,
    )
    try:
        slack.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts or placeholder_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("ceo_brain_final_render_fallback_new_message_posted")
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_final_render_fallback_failed",
            error=str(e), error_type=type(e).__name__,
        )


def _collect_local_tool_uses(
    final_message: Any,
    tool_executors: dict[str, Callable[[dict[str, Any]], str]],
) -> list[Any]:
    """Pick out tool_use blocks whose name matches a local executor.
    MCP tool_use blocks (``mcp_tool_use``) are handled server-side
    by Anthropic and don't need a follow-up turn from us."""
    if not tool_executors:
        return []
    pending: list[Any] = []
    for b in (getattr(final_message, "content", None) or []):
        btype = getattr(b, "type", None) or (
            isinstance(b, dict) and b.get("type")
        )
        if btype != "tool_use":
            continue
        name = getattr(b, "name", None) or (
            isinstance(b, dict) and b.get("name")
        ) or ""
        if name in tool_executors:
            pending.append(b)
    return pending


def run_responder(
    *,
    slack: Any,
    anthropic_client: Any,
    channel: str,
    placeholder_ts: str,
    thread_history: list[dict[str, Any]],
    db_session: Session | None = None,
    slack_event_ts: str | None = None,
    today: datetime | None = None,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    slack_bot_client: Any | None = None,
    slack_user_client: Any | None = None,
    max_tool_loops: int = 10,
    placeholder_thread_ts: str | None = None,
) -> ClaudeResponderRun | None:
    """End-to-end Claude responder.

      1. Build the Anthropic request (system + MCP + caching).
      2. Open ``messages.stream``. On 429, retry up to ``max_retries``
         times with ``retry-after`` backoff (FR-CB2-3.13). On 5xx,
         persist as ``failed`` and surface a ":warning:" placeholder
         (FR-CB2-3.12).
      3. Stream text deltas to Slack via batched ``chat_update``.
         Record tool_use events as they fire (FR-CB2-3.15).
      4. On completion, append a ``Sources:`` block and do a final
         ``chat_update`` (FR-CB2-3.9).
      5. Persist the run row with usage / cost.
    """
    # FR-CB2-3.16 — local Slack tools. When the dispatcher supplies a
    # Slack ``WebClient``, we expose Slack capabilities as regular
    # Anthropic ``tools`` and execute them locally (no external MCP).
    tool_executors: dict[str, Callable[[dict[str, Any]], str]] = {}
    local_tool_schemas: list[dict[str, Any]] = []
    if slack_bot_client is not None:
        from app.ceo_brain.slack_tools import (
            SLACK_TOOL_SCHEMAS,
            build_executors,
        )
        tool_executors = build_executors(
            bot_client=slack_bot_client,
            user_client=slack_user_client,
        )
        local_tool_schemas = list(SLACK_TOOL_SCHEMAS)

    request = build_anthropic_request(
        thread_history=thread_history, today=today,
        tools=local_tool_schemas,
    )
    started_at = datetime.now(timezone.utc)

    # Hosted MCP connector lives in the beta namespace and needs
    # the `mcp-client-2025-04-04` beta header when `mcp_servers`
    # is passed to `messages.stream`. Without MCP servers, the
    # regular `messages.stream` is fine.
    if request.get("mcp_servers"):
        stream_factory = anthropic_client.beta.messages.stream
        request = {**request, "betas": ["mcp-client-2025-04-04"]}
    else:
        stream_factory = anthropic_client.messages.stream

    text_buffer: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    last_update = [0.0]
    final_message: Any = None

    # FR-CB2-3.16 — outer tool-use loop. Each iteration runs one
    # streamed Claude turn; if the turn ends with a `tool_use` block
    # for a *local* tool, execute it, append assistant+user turns,
    # and re-stream. MCP tool_uses are resolved by Anthropic
    # server-side so they don't trigger another iteration.
    final_response_failed = False
    for loop_idx in range(max(1, max_tool_loops)):
        stream_ok = False
        for attempt in range(max(1, max_retries)):
            try:
                with stream_factory(**request) as stream:
                    for event in stream:
                        etype = getattr(event, "type", None) or (
                            isinstance(event, dict) and event.get("type")
                        )
                        if etype == "content_block_start":
                            block = (
                                getattr(event, "content_block", None)
                                if not isinstance(event, dict)
                                else event.get("content_block")
                            )
                            btype = (
                                getattr(block, "type", None)
                                if not isinstance(block, dict)
                                else (block or {}).get("type")
                            )
                            if btype in {"tool_use", "server_tool_use", "mcp_tool_use"}:
                                tool_name = (
                                    getattr(block, "name", None)
                                    if not isinstance(block, dict)
                                    else (block or {}).get("name")
                                ) or ""
                                tool_input = (
                                    getattr(block, "input", None)
                                    if not isinstance(block, dict)
                                    else (block or {}).get("input")
                                ) or {}
                                tool_uses.append(
                                    {"name": tool_name, "input": tool_input}
                                )
                                note = describe_tool_use_for_slack(
                                    tool_name=tool_name,
                                    input_arg=tool_input,
                                )
                                text_buffer.append("\n" + note)
                                _stream_text_to_slack(
                                    slack=slack, channel=channel,
                                    placeholder_ts=placeholder_ts,
                                    text_buffer=text_buffer,
                                    last_update=last_update,
                                    force=True,
                                )
                            continue
                        if etype == "content_block_delta":
                            delta = (
                                getattr(event, "delta", None)
                                if not isinstance(event, dict)
                                else event.get("delta")
                            )
                            dtype = (
                                getattr(delta, "type", None)
                                if not isinstance(delta, dict)
                                else (delta or {}).get("type")
                            )
                            if dtype in {"text_delta", "text"}:
                                chunk = (
                                    getattr(delta, "text", None)
                                    if not isinstance(delta, dict)
                                    else (delta or {}).get("text")
                                ) or ""
                                if chunk:
                                    text_buffer.append(chunk)
                                    _stream_text_to_slack(
                                        slack=slack, channel=channel,
                                        placeholder_ts=placeholder_ts,
                                        text_buffer=text_buffer,
                                        last_update=last_update,
                                    )
                    final_message = stream.get_final_message()
                stream_ok = True
                break
            except BaseException as exc:  # noqa: BLE001
                if _is_429(exc) and attempt + 1 < max_retries:
                    wait = _retry_after_seconds(exc, default=2.0)
                    log.info(
                        "ceo_brain_responder_rate_limited_retry",
                        attempt=attempt + 1, wait_seconds=wait,
                    )
                    try:
                        slack.chat_update(
                            channel=channel, ts=placeholder_ts,
                            text=f"⏳ rate-limit, повторю через ~{int(wait or 1)} сек",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    sleep(wait)
                    continue
                log.warning(
                    "ceo_brain_responder_failed",
                    error=str(exc), error_type=type(exc).__name__,
                )
                try:
                    slack.chat_update(
                        channel=channel, ts=placeholder_ts,
                        text="⚠️ временная ошибка, попробуй через минуту",
                    )
                except Exception:  # noqa: BLE001
                    pass
                if db_session is not None:
                    persist_run(
                        db_session,
                        slack_channel_id=channel,
                        slack_event_ts=slack_event_ts or placeholder_ts,
                        slack_placeholder_ts=placeholder_ts,
                        request_payload=request,
                        response_text="",
                        tool_uses=tool_uses,
                        status=(
                            "rate_limited" if _is_429(exc) else "failed"
                        ),
                        error=str(exc),
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                    )
                final_response_failed = True
                break
        if not stream_ok:
            return None

        # If the turn produced local tool_use blocks, execute them
        # and extend the conversation with assistant + user turns.
        pending_local = _collect_local_tool_uses(
            final_message, tool_executors,
        )
        if not pending_local:
            break

        tool_result_blocks: list[dict[str, Any]] = []
        for tu in pending_local:
            name = getattr(tu, "name", None) or (
                isinstance(tu, dict) and tu.get("name")
            ) or ""
            tid = getattr(tu, "id", None) or (
                isinstance(tu, dict) and tu.get("id")
            ) or ""
            inp = getattr(tu, "input", None) or (
                isinstance(tu, dict) and tu.get("input")
            ) or {}
            try:
                result = tool_executors[name](inp)
                is_error = False
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "ceo_brain_local_tool_crashed",
                    tool=name, error=str(e),
                )
                result = json.dumps(
                    {"error": f"executor crashed: {e}"},
                    ensure_ascii=False,
                )
                is_error = True
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "content": result,
                **({"is_error": True} if is_error else {}),
            })

        assistant_content = _serialise_content_blocks(
            getattr(final_message, "content", None) or []
        )
        new_messages = list(request["messages"]) + [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": tool_result_blocks},
        ]
        request = {**request, "messages": new_messages}
    else:
        log.warning(
            "ceo_brain_responder_tool_loop_exhausted",
            max_tool_loops=max_tool_loops,
        )

    if final_response_failed:
        return None

    # Build the final response with Sources block and update Slack.
    final_text = "".join(text_buffer)

    def _extract_sdk_text(msg: Any) -> str:
        if msg is None or not getattr(msg, "content", None):
            return ""
        out = ""
        for block in msg.content:
            btype = getattr(block, "type", None) or (
                isinstance(block, dict) and block.get("type")
            )
            if btype == "text":
                out += (
                    getattr(block, "text", None)
                    if not isinstance(block, dict)
                    else (block or {}).get("text") or ""
                ) or ""
        return out

    sdk_text = _extract_sdk_text(final_message)
    if sdk_text.strip():
        final_text = sdk_text

    def _needs_synthesis_recovery(msg: Any) -> bool:
        """True when the turn does NOT end with a synthesising text
        block. Two failure modes covered:

          1. No text blocks at all — sdk_text empty.
          2. Interleaved planning text + tool_uses, but the LAST
             content block is a tool_use (no summary written after
             the final tool call). Observed Sonnet quirk
             2026-05-19: bot replied with "Проверю транскрипт…"
             then ended on `mcp_tool_use` without ever writing the
             answer.
        """
        if msg is None or not getattr(msg, "content", None):
            return False
        last_text_idx = -1
        last_tool_idx = -1
        for i, block in enumerate(msg.content):
            btype = getattr(block, "type", None) or (
                isinstance(block, dict) and block.get("type")
            )
            if btype == "text":
                txt = (
                    getattr(block, "text", None)
                    if not isinstance(block, dict)
                    else (block or {}).get("text") or ""
                ) or ""
                if txt.strip():
                    last_text_idx = i
            elif btype in {
                "tool_use", "mcp_tool_use", "server_tool_use",
                "mcp_tool_result",
            }:
                last_tool_idx = i
        return last_text_idx <= last_tool_idx

    # FR-CB2-3.17 — synthesis recovery. Observed Sonnet quirks
    # (both covered by `_needs_synthesis_recovery`):
    #   * model fires tool_use blocks then ends without summary;
    #   * model writes interleaved planning text + tool_uses but
    #     the LAST block is a tool_use (no synthesis after it).
    # When detected, ask the model explicitly to summarise —
    # feeding back its own tool calls and results as context.
    if (
        tool_uses
        and final_message is not None
        and _needs_synthesis_recovery(final_message)
    ):
        log.info("ceo_brain_responder_empty_text_recovery_attempt")
        try:
            recovery_messages = list(request["messages"]) + [
                {
                    "role": "assistant",
                    "content": _serialise_content_blocks(
                        getattr(final_message, "content", None) or []
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Сформулируй краткий ответ на основе результатов "
                        "tool-вызовов выше. Если данных недостаточно — "
                        "честно скажи об этом."
                    ),
                },
            ]
            recovery_request = {**request, "messages": recovery_messages}
            with stream_factory(**recovery_request) as rec_stream:
                for event in rec_stream:
                    etype = getattr(event, "type", None) or (
                        isinstance(event, dict) and event.get("type")
                    )
                    if etype != "content_block_delta":
                        continue
                    delta = (
                        getattr(event, "delta", None)
                        if not isinstance(event, dict)
                        else event.get("delta")
                    )
                    dtype = (
                        getattr(delta, "type", None)
                        if not isinstance(delta, dict)
                        else (delta or {}).get("type")
                    )
                    if dtype not in {"text_delta", "text"}:
                        continue
                    chunk = (
                        getattr(delta, "text", None)
                        if not isinstance(delta, dict)
                        else (delta or {}).get("text")
                    ) or ""
                    if chunk:
                        text_buffer.append(chunk)
                        _stream_text_to_slack(
                            slack=slack, channel=channel,
                            placeholder_ts=placeholder_ts,
                            text_buffer=text_buffer,
                            last_update=last_update,
                        )
                recovery_final = rec_stream.get_final_message()
            recovery_text = _extract_sdk_text(recovery_final)
            if recovery_text.strip():
                final_text = recovery_text
                final_message = recovery_final
                log.info(
                    "ceo_brain_responder_empty_text_recovered",
                    chars=len(recovery_text),
                )
            else:
                log.warning(
                    "ceo_brain_responder_empty_text_recovery_no_text",
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ceo_brain_responder_empty_text_recovery_failed",
                error=str(e), error_type=type(e).__name__,
            )

    # Last-resort fallback: still empty after recovery → explicit
    # message so the operator doesn't see a blank reply.
    if not final_text.strip():
        final_text = (
            "_(модель не сформулировала ответ — см. Sources ниже)_"
        )

    rendered = format_final_response(text=final_text, tool_uses=tool_uses)
    # FR-CB2-3.18 — robust final render. Retries rate-limited updates
    # and falls back to a new threaded message if all retries fail.
    _final_render_to_slack(
        slack=slack,
        channel=channel,
        placeholder_ts=placeholder_ts,
        thread_ts=placeholder_thread_ts,
        text=rendered,
        sleep=sleep,
    )

    # Extract usage / cost from the final_message. Coerce defensively
    # so a MagicMock or unexpected SDK shape doesn't poison the
    # downstream Decimal conversion.
    def _coerce_int(v: Any) -> int | None:
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        return None

    usage = getattr(final_message, "usage", None) if final_message else None
    input_t = _coerce_int(getattr(usage, "input_tokens", None) if usage else None)
    output_t = _coerce_int(getattr(usage, "output_tokens", None) if usage else None)
    cache_r = _coerce_int(
        getattr(usage, "cache_read_input_tokens", None) if usage else None
    )
    cache_w = _coerce_int(
        getattr(usage, "cache_creation_input_tokens", None) if usage else None
    )
    # Sonnet 4.6 list price: input $3/Mtok, output $15/Mtok, cache
    # write 1.25× input, cache read 0.1× input. Approximation —
    # exact billing comes from Anthropic.
    cost = 0.0
    if input_t:
        cost += (input_t / 1_000_000) * 3.0
    if output_t:
        cost += (output_t / 1_000_000) * 15.0
    if cache_r:
        cost += (cache_r / 1_000_000) * 0.30
    if cache_w:
        cost += (cache_w / 1_000_000) * 3.75

    if db_session is not None:
        row = persist_run(
            db_session,
            slack_channel_id=channel,
            slack_event_ts=slack_event_ts or placeholder_ts,
            slack_placeholder_ts=placeholder_ts,
            request_payload=request,
            response_text=final_text,
            tool_uses=tool_uses,
            status="done",
            cost_usd=round(cost, 6),
            input_tokens=input_t,
            output_tokens=output_t,
            cache_read_tokens=cache_r,
            cache_write_tokens=cache_w,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )
        return row
    return None


__all__ = [
    "build_anthropic_request",
    "build_system_prompt",
    "build_thread_history",
    "describe_tool_use_for_slack",
    "format_final_response",
    "persist_run",
    "post_placeholder",
    "run_responder",
    "should_respond",
]
