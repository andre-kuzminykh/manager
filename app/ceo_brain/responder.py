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
    """FR-CB2-3.14 + FR-CB2-3.19 — system prompt with persona,
    today's date, and operator-pinned search-strategy rules."""
    today = today or datetime.now(timezone.utc)
    today_iso = today.strftime("%Y-%m-%d")
    prompt = (
        f"Ты — CEO Brain Bot, личный ассистент Артема Соколова "
        f"(CEO humanoid.ai). Сегодня {today_iso}.\n\n"
        f"ОСНОВНОЕ ПРАВИЛО: на любой вопрос про данные оператора "
        f"(встречи, сообщения, письма, документы, заметки, контакты, "
        f"задачи, инвесторов и т.д.) ТЫ ОБЯЗАТЕЛЬНО ДЁРГАЕШЬ "
        f"соответствующий MCP-tool и отвечаешь на основе РЕАЛЬНЫХ "
        f"данных. Не отвечай по памяти, не предполагай, не описывай "
        f"свои возможности — сразу ищи. Только если данных правда нет "
        f"и tool вернул empty — честно скажи «не нашёл».\n\n"
        f"СТРАТЕГИЯ ПОИСКА (FR-CB2-3.19+3.21, operator-pinned 2026-05-19):\n"
        f"1. Search-query должен брать ТЕМУ из вопроса оператора "
        f"(fundraising, due diligence, raise, EQT, term sheet и т.п.), "
        f"а не только имя контрагента. Поиск только по имени даёт "
        f"все встречи с этим человеком — велик шанс попасть в "
        f"нерелевантную. Пример: вопрос «что обсудили с Йоханом по "
        f"фандрайзингу» → query «fundraising» / «Fundraising daily», "
        f"а не «Jochen» или «Strategic Investors».\n"
        f"2. ПОСЛЕ search-tools ВСЕГДА ОБЯЗАТЕЛЬНО зови "
        f"`get_zoom_transcript` (или `get_meeting`) для топ-1-3 "
        f"кандидатов из выдачи. Search возвращает только ЗАГОЛОВКИ "
        f"встреч — никакого содержимого там нет. Без get_transcript "
        f"ответить невозможно. Запрещено отвечать оператору, имея "
        f"в контексте только результаты search-tools.\n"
        f"3. Если `get_zoom_transcript` / `get_meeting` вернул "
        f"transcript короче 2000 символов — это пустой transcript "
        f"(содержит только список участников, без диалога). НЕ "
        f"сдавайся — попробуй следующий кандидат из выдачи поиска "
        f"или сделай новый search с другим query (другая тема, "
        f"другая дата). Только если все кандидаты пустые — отвечай "
        f"что данных нет.\n\n"
        f"СТИЛЬ ОТВЕТА (FR-CB2-3.28+3.29+3.30, operator-pinned, "
        f"строгий):\n"
        f"- ПЕРВАЯ СТРОКА ответа = СУТЬ ответа. НЕЛЬЗЯ начинать с "
        f"«Ищу параллельно.», «Хорошо, нашёл…», «Транскрипт "
        f"зашумлён…», «Дополняю данными из задач.», «Читаю "
        f"транскрипт…», «Вот что удалось восстановить…», «Из "
        f"транскрипта восстанавливается:». Без преамбул, без "
        f"вступлений, без мета-комментариев про процесс или "
        f"качество данных.\n"
        f"- БЕЗ описания процесса (что ты искал, какие tools "
        f"дёргал, как зашумлены данные).\n"
        f"- БЕЗ markdown bold `**текст**` и `*текст*`. Никаких "
        f"звёздочек для выделения. Если нужен акцент — заглавные "
        f"буквы или просто без выделения.\n"
        f"- БЕЗ разделителей `---` / `===` между секциями.\n"
        f"- БЕЗ markdown-таблиц с колонками «Приоритет/Срок» если "
        f"оператор явно не просил таблицу. Просто текст или "
        f"короткий bullet-список (с `-` или `•`).\n"
        f"- ОГРАНИЧЕНИЕ ОБЪЁМА: 5-8 строк максимум на типичный "
        f"вопрос. Длиннее — только если оператор просит «подробно».\n"
        f"- БЕЗ эмодзи. БЕЗ заголовков «1. … 2. … 3. …» когда это "
        f"просто 3 факта подряд.\n"
        f"- ЗАПРЕЩЕНЫ technical IDs в тексте: `Zoom ID: Es0xx…`, "
        f"`meeting_id=…`, `60 мин`, длинные hash-строки. Это metadata "
        f"мусор. Оператор хочет суть факта + ссылку на источник.\n"
        f"- ИСТОЧНИКИ КАК INLINE-ГИПЕРССЫЛКИ В ТЕКСТЕ. НЕ выводи "
        f"финальный блок `_Источники: tool_a, tool_b_`. Вместо этого "
        f"вшивай ссылку прямо во фразу в Slack-нативном формате "
        f"`<URL|короткий якорь>`. Пример: «На <https://docs.google.com/"
        f"document/d/.../edit|Fundraising daily> обсудили статус "
        f"раунда». Если URL источника недоступен — просто упомяни "
        f"источник коротко словами без отдельной строки в конце.\n"
        f"- На русском, если оператор пишет на русском; иначе на "
        f"языке вопроса.\n\n"
        f"Когда оператор пишет короткое приветствие («привет», "
        f"«hi», «hey») без конкретного запроса — отвечай коротко "
        f"приветствием без описания capabilities."
    )
    # FR-CB2-4.6 — Atlassian Rovo MCP tools require a `cloudId` (site URL or
    # UUID). Inject it from env so Claude always passes it; without it the
    # call fails with «Cloud ID configuration error».
    import os as _os

    _atl = _os.environ.get("ATLASSIAN_CLOUD_ID", "").strip()
    if _atl:
        prompt += (
            "\n\nATLASSIAN (Jira/Confluence/Rovo MCP): у инструментов "
            "`atlassian` (getTeamworkGraphContext, getTeamworkGraphObject) "
            f"параметр cloudId ОБЯЗАТЕЛЕН — всегда передавай cloudId='{_atl}'. "
            "Без него запрос к Atlassian падает с ошибкой Cloud ID."
        )
    # FR-CB2-4.7 — when Jira REST creds are set, the bot has local
    # `jira_search`/`jira_get_issue` tools. Rovo MCP's Teamwork-Graph
    # tools can't search by status; JQL can — so steer status/count
    # questions to jira_search.
    if _os.environ.get("ATLASSIAN_EMAIL") and _os.environ.get(
        "ATLASSIAN_API_TOKEN"
    ):
        prompt += (
            "\n\nJIRA-ЗАДАЧИ: для любых вопросов про статусы, количество "
            "или списки задач Jira («сколько задач в on hold», «что в "
            "работе») используй инструмент `jira_search` с JQL — НЕ "
            "Teamwork-Graph. «CEO Brain» (а также «CEO», «мозг CEO») в "
            "Jira — это проект CEO Office, ключ `BA`; используй "
            "`project = BA`. Статусы пишутся как в Jira, в т.ч. по-"
            "английски в русскоязычном инстансе: `\"On Hold\"`, "
            "`Done`/«Готово», «К выполнению». Пример: "
            "`project = BA AND status = \"On Hold\"`. Если jira_search "
            "вернул поле `error` или ошибку про несуществующее значение "
            "поля — НЕ выдумывай «0» и не говори «нет доступа»: покажи "
            "текст ошибки и повтори запрос с исправленным полем. Поле "
            "`total` в ответе — число найденных задач."
        )

    # FR-TV-071 — task-tool routing rules (only when the layer is enabled).
    try:
        from app.config import get_settings as _gs

        if getattr(_gs(), "task_vector_enabled", False):
            prompt += (
                "\n\nЗАДАЧИ (vector): для вопросов про задачи и для NL-"
                "обновлений используй инструменты `search_tasks` / `get_task` "
                "/ `resolve_person` / `update_task_status` / `update_task_due` "
                "/ `update_task_owner`.\n"
                "• Глагол→статус: отправил/сделал/закрыл/завершил/готово → "
                "done; начал/в работе/приступил → in_progress.\n"
                "• Срок: «перенеси на пятницу / до 10 июня» — разбери дату САМ "
                "в ISO (YYYY-MM-DD) и зови `update_task_due`.\n"
                "• Ответственный: «теперь Семён» — сперва `resolve_person`, "
                "потом `update_task_owner` с person_id+именем.\n"
                "• Сначала `search_tasks`, чтобы найти задачу. Если РОВНО ОДНА "
                "уверенно подходит — применяй изменение СРАЗУ и сообщи "
                "«<поле> X→Y (отменить?)». Если подходят 2+ или уверенность "
                "низкая — НЕ меняй, перечисли кандидатов и спроси, какую. "
                "Отмена = повторный вызов того же тула со старым значением "
                "(в ответе тула есть `from`)."
            )
    except Exception:  # noqa: BLE001 — prompt must never fail to build
        pass
    return prompt


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
    """FR-CB2-3.29 — return body only. Operator-pinned 2026-05-19:
    no auto-appended `_Sources:_` footer. The system prompt now
    instructs the model to embed source hyperlinks inline.
    `tool_uses` is still accepted for API stability but ignored
    in the rendered output."""
    body = (text or "").rstrip()
    return body or "_(пустой ответ от Claude)_"


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
# FR-CB2-3.23 — transient MCP handshake error. Anthropic returns
# `BadRequestError: Connection error while communicating with MCP
# server` when ONE of the configured MCP endpoints stalls on the
# initial parallel handshake. Individual endpoints are healthy
# (verified one-by-one) — this is a parallel-startup race, not a
# persistent fault. Retry like 429.
_RETRYABLE_MCP_RE = re.compile(
    r"connection error while communicating with mcp server",
    re.IGNORECASE,
)
# FR-CB2-3.23 v2 — Anthropic API timeout / network drop is also
# transient (the model spent too long resolving an MCP tool, or
# the HTTP connection dropped). Retry.
_RETRYABLE_TIMEOUT_RE = re.compile(
    r"request timed out|api.?timeout|connection.?(drop|reset|aborted)",
    re.IGNORECASE,
)


def _is_429(exc: BaseException) -> bool:
    return bool(_RETRYABLE_429_RE.search(str(exc) or ""))


def _is_transient_mcp_handshake(exc: BaseException) -> bool:
    """FR-CB2-3.23 — Anthropic 400 with the MCP-handshake message,
    OR API timeout, OR connection drop — all considered transient."""
    msg = str(exc) or ""
    return bool(
        _RETRYABLE_MCP_RE.search(msg)
        or _RETRYABLE_TIMEOUT_RE.search(msg)
        or type(exc).__name__ == "APITimeoutError"
    )


# FR-CB2-3.25 — MCP catalog for smart routing. Operator-pinned set.
_MCP_DESCRIPTIONS: dict[str, str] = {
    "n8n_main": (
        "Google Drive / Sheets — поиск файлов, чтение spreadsheet'ов "
        "по названию или содержимому."
    ),
    "n8n_calendar": (
        "Zoom встречи + транскрипты + задачи: search_zoom_meetings, "
        "search_meetings, get_zoom_transcript, get_meeting, "
        "search_zoom_tasks. Используй ЛЮБОЙ раз когда вопрос про "
        "встречи, звонки, обсуждения, transcripts, action items."
    ),
    "n8n_gmail": (
        "LinkedIn search — поиск людей в LinkedIn по имени, компании, "
        "ключевым словам. (Несмотря на имя, это НЕ Gmail.)"
    ),
    "n8n_drive": (
        "Telegram чаты и сообщения — get_chats, search_messages. "
        "Используй когда вопрос про переписку в Telegram, "
        "сообщения с командой, апдейты от коллег."
    ),
    "n8n_rocketreach": (
        "RocketReach — контакты людей: email, телефон, LinkedIn по "
        "имени/компании. Используй для cold-outreach задач."
    ),
    "n8n_hubspot": (
        "HubSpot CRM — компании и контакты в CRM, search_companies, "
        "search_contacts. Используй для look-up по инвесторам / "
        "клиентам / counterparty info."
    ),
    "slack_self": (
        "Slack-инструменты самого CEO Brain бота: список каналов "
        "куда подключен, поиск сообщений в Slack, чтение истории "
        "канала, поиск юзера по email, permalink. Используй когда "
        "вопрос про сам Slack — «в каких каналах ты добавлен», "
        "«найди в slack сообщение X», «кто такой пользователь Y»."
    ),
    "tasks_self": (
        "Задачи (семантический поиск + обновления): search_tasks, get_task, "
        "resolve_person, update_task_status, update_task_due, "
        "update_task_owner. Используй для вопросов про задачи («какие задачи "
        "по X / что на Y / что просрочено») и для NL-обновлений («отправил "
        "письмо Семёну», «перенеси на пятницу», «ответственный теперь Семён»)."
    ),
}


def select_mcps_for_question(
    *,
    question: str,
    all_servers: list[dict],
    anthropic_client: Any,
    thread_context: list[dict] | None = None,
) -> list[dict]:
    """FR-CB2-3.25 — pick the subset of MCP servers actually needed
    to answer this question. Reduces parallel handshake load and
    overall latency.

    Uses a fast `claude-haiku-4-5` call with a tiny prompt — adds
    ~1 sec but saves 30-300 sec on handshake retries downstream.
    On ANY failure (network, malformed JSON, unknown names) falls
    back to the full server list (no degradation).

    FR-CB2-3.37 — when ``thread_context`` is provided (last 2-3
    messages in the thread, oldest first), include it in the prompt
    so the classifier resolves follow-up questions like «а за
    вчера», «а на встречах».
    """
    if not all_servers or not question:
        return list(all_servers)
    catalog_lines = []
    name_to_server = {s.get("name"): s for s in all_servers if s.get("name")}
    for name in name_to_server:
        desc = _MCP_DESCRIPTIONS.get(name, "(no description)")
        catalog_lines.append(f"- {name}: {desc}")
    catalog = "\n".join(catalog_lines)
    # FR-CB2-3.37 — render thread context (last 2-3 messages) so
    # follow-ups make sense in isolation.
    context_block = ""
    if thread_context:
        ctx_lines = []
        for m in thread_context[-3:]:
            role = m.get("role") or "?"
            txt = (m.get("content") or "").strip()
            if not txt:
                continue
            label = "Оператор" if role == "user" else "Бот"
            ctx_lines.append(f"- {label}: {txt[:300]}")
        if ctx_lines:
            context_block = (
                "Недавний контекст в треде (для расшифровки "
                "follow-up вопросов вроде «а за вчера», «а на "
                "встречах»):\n" + "\n".join(ctx_lines) + "\n\n"
            )

    prompt = (
        f"{context_block}"
        f"Текущий вопрос оператора: {question}\n\n"
        "Доступные источники данных (MCP servers):\n"
        f"{catalog}\n\n"
        "Какие источники реально нужны, чтобы ответить на ЭТОТ "
        "вопрос (учитывая контекст треда выше, если есть)? Включай "
        "только те, что СКОРЕЕ ВСЕГО содержат "
        "релевантные данные. Когда сомневаешься — включай. "
        "Минимум 1 источник.\n\n"
        "ВАЖНО: если в вопросе упоминаются имена коллег "
        "(Артем, Алина, Ира, Дима, Йохан и т.п.) В КОНТЕКСТЕ "
        "СООБЩЕНИЙ или 'сказал/ответил/написал' — почти всегда "
        "нужен `n8n_drive` (Telegram-переписка), даже если есть и "
        "`n8n_calendar`. Multi-part вопрос («что X сказала на встрече "
        "и Y ответила в чате») = ОБА источника.\n\n"
        "ВАЖНО: следующие слова обозначают ВСТРЕЧУ/ZOOM (всегда "
        "подключай `n8n_calendar`): «встреча», «созвон», «звонок», "
        "«синк», «sync», «syncup», «синхрон», «zoom», «meeting», "
        "«колл», «call», «daily», «синк», «дейли», «standup», "
        "«стендап», «планёрка». Даже без слова «встреча», если "
        "контекст про обсуждение/разговор с конкретным человеком "
        "за прошедшее время («что Х сказала позавчера на синке») — "
        "это про zoom-встречу, добавь `n8n_calendar`.\n\n"
        "Output: только JSON формата {\"mcps\": [\"name1\", ...]}, "
        "без markdown, без комментариев."
    )
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        log.info(
            "ceo_brain_mcp_router_classifier_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return list(all_servers)
    raw_text = ""
    for b in (getattr(resp, "content", None) or []):
        if getattr(b, "type", None) == "text":
            raw_text += getattr(b, "text", None) or ""
    raw_text = raw_text.strip()
    # Strip ```json fences if model added them despite the prompt.
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()
    try:
        parsed = json.loads(raw_text)
    except Exception:  # noqa: BLE001
        log.info(
            "ceo_brain_mcp_router_bad_json", raw=raw_text[:200],
        )
        return list(all_servers)
    picked = parsed.get("mcps") if isinstance(parsed, dict) else None
    if not isinstance(picked, list) or not picked:
        return list(all_servers)
    out: list[dict] = []
    for name in picked:
        srv = name_to_server.get(name)
        if srv is not None:
            out.append(srv)
    if not out:
        return list(all_servers)
    log.info(
        "ceo_brain_mcp_router_picked",
        from_=len(all_servers), to=len(out),
        names=[s.get("name") for s in out],
    )
    return out


def _degrade_mcp_servers(servers: list[dict] | None, attempt: int) -> list[dict] | None:
    """FR-CB2-3.24 — progressive MCP degradation on handshake retry.

    `attempt` is 1-indexed. On attempts 1-2 we keep the full set;
    starting attempt 3, drop one MCP per attempt from the END of
    the list (operator-controlled order). Always keep at least 2.

    Examples (with 6 servers in env):
      attempt 1 → 6
      attempt 2 → 6
      attempt 3 → 5
      attempt 4 → 4
      attempt 5 → 3
    """
    if not servers:
        return servers
    if attempt <= 2:
        return list(servers)
    drop = attempt - 2
    keep = max(2, len(servers) - drop)
    return list(servers[:keep])


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
    max_retries: int = 5,
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

    # FR-CB2-4.7 — local Jira tools. The Atlassian Rovo MCP endpoint
    # with an API token only exposes the 2 Teamwork-Graph tools (no
    # JQL search), so status questions can't be answered via MCP. The
    # same token works against the Jira Cloud REST API, so we expose
    # `jira_search` / `jira_get_issue` as local tools when creds are set.
    from app.ceo_brain.jira_tools import build_jira_executors_from_env

    _jira_execs, _jira_schemas = build_jira_executors_from_env()
    if _jira_execs:
        tool_executors = {**tool_executors, **_jira_execs}
        local_tool_schemas = list(local_tool_schemas) + _jira_schemas

    # FR-TV (P3) — local Task tools (semantic search + NL status/due/owner
    # updates), gated by TASK_VECTOR_ENABLED. Never breaks the responder.
    _task_execs: dict[str, Any] = {}
    try:
        from app.config import get_settings as _gs

        if getattr(_gs(), "task_vector_enabled", False):
            from app.ceo_brain.task_tools import (
                TASK_TOOL_SCHEMAS as _TTS,
                build_task_executors as _bte,
            )
            from app.db import get_session_factory as _gsf

            _task_execs = _bte(session_factory=_gsf(), settings=_gs())
            tool_executors = {**tool_executors, **_task_execs}
            local_tool_schemas = list(local_tool_schemas) + list(_TTS)
    except Exception as e:  # noqa: BLE001 — never break the responder
        log.warning("ceo_brain_task_tools_wire_failed", error=str(e))
        _task_execs = {}

    request = build_anthropic_request(
        thread_history=thread_history, today=today,
        tools=local_tool_schemas,
    )
    started_at = datetime.now(timezone.utc)

    # FR-CB2-3.25 — smart MCP routing. Pre-classify the question
    # with claude-haiku (fast, cheap) to pick only the MCP servers
    # actually needed — drops parallel handshake load, reliability
    # ↑, latency ↓. On classifier failure, fall back to full list.
    last_user_q = ""
    if request.get("mcp_servers"):
        for m in reversed(thread_history or []):
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    last_user_q = content
                    break
        if last_user_q:
            # FR-CB2-3.37 — provide last 2-3 thread messages as
            # context so follow-ups («а за вчера», «а на встречах»)
            # resolve. Exclude the trailing current question so it
            # doesn't duplicate.
            ctx = [
                m for m in (thread_history or [])
                if isinstance(m, dict) and m.get("content")
            ]
            if ctx and ctx[-1].get("content", "").strip() == last_user_q.strip():
                ctx = ctx[:-1]
            picked = select_mcps_for_question(
                question=last_user_q,
                all_servers=request["mcp_servers"],
                anthropic_client=anthropic_client,
                thread_context=ctx[-3:] if ctx else None,
            )
            request = {**request, "mcp_servers": picked}

    # FR-CB2-3.31 — direct-HTTP gather (replaces FR-CB2-3.30
    # Anthropic-MCP gather). The Anthropic-MCP connector compounds
    # latency 5-15 sec per inner tool call (operator-observed
    # 2026-05-20: minutes on multi-step queries, timeouts). Going
    # direct HTTP to n8n MCP returns the same data in 1.97 sec per
    # call. Architecture:
    #   1. fetch tool catalogs (`mcp_client.list_tools`, cached
    #      per URL)
    #   2. haiku planner picks `[(mcp, tool, args), ...]`
    #   3. parallel HTTP calls via `gather_via_direct_http`
    #   4. final synthesis via Anthropic (no MCP, no beta)
    # FR-CB2-3.32 — virtual `slack_self` MCP for local Slack tools.
    # Operator-observed 2026-05-20: «в каких каналах ты добавлен»
    # questions couldn't reach `slack_list_channels` because the
    # direct-HTTP pipeline only saw n8n MCPs. Inject a synthetic
    # MCP entry so the classifier/planner can pick local tools too.
    SLACK_SELF_URL = "local://slack"
    if last_user_q:
        servers_with_self = list(request.get("mcp_servers") or [])
        # Add slack_self if not already there.
        if not any(s.get("name") == "slack_self" for s in servers_with_self):
            servers_with_self.append({
                "name": "slack_self",
                "url": SLACK_SELF_URL,
                "type": "url",
            })
        # FR-CB2-4.7 — virtual `jira_self` MCP so the classifier/planner
        # can route Jira status questions to the local REST tools.
        if _jira_execs and not any(
            s.get("name") == "jira_self" for s in servers_with_self
        ):
            servers_with_self.append({
                "name": "jira_self",
                "url": "local://jira",
                "type": "url",
            })
        # FR-TV — virtual `tasks_self` MCP so the planner can route task
        # questions/updates to the local task tools.
        if _task_execs and not any(
            s.get("name") == "tasks_self" for s in servers_with_self
        ):
            servers_with_self.append({
                "name": "tasks_self",
                "url": "local://tasks",
                "type": "url",
            })
        # Re-classify with slack_self in the mix.
        picked2 = select_mcps_for_question(
            question=last_user_q,
            all_servers=servers_with_self,
            anthropic_client=anthropic_client,
        )
        # Keep slack_self only if classifier picked it explicitly.
        request = {**request, "mcp_servers": picked2}

    if request.get("mcp_servers") and last_user_q:
        from app.ceo_brain.mcp_client import list_tools as _mcp_list_tools
        from app.ceo_brain.parallel_gather import (
            gather_via_direct_http,
            synthesize_final_answer,
        )
        from app.ceo_brain.planner import plan_tool_calls
        from app.ceo_brain.slack_tools import SLACK_TOOL_SCHEMAS

        log.info(
            "ceo_brain_direct_http_start",
            mcp_count=len(request["mcp_servers"]),
            mcps=[s.get("name") for s in request["mcp_servers"]],
        )
        try:
            slack.chat_update(
                channel=channel, ts=placeholder_ts,
                text="🔄 планирую и параллельно дёргаю MCP…",
            )
        except Exception:  # noqa: BLE001
            pass

        # 1. tool catalogs
        tools_by_mcp: dict[str, list[dict]] = {}
        for srv in request["mcp_servers"]:
            name = srv.get("name") or ""
            url = srv.get("url") or ""
            if not name:
                continue
            if name == "slack_self":
                # Local tools — catalog comes from SLACK_TOOL_SCHEMAS,
                # not an HTTP roundtrip.
                tools_by_mcp[name] = list(SLACK_TOOL_SCHEMAS)
                continue
            if name == "jira_self":
                from app.ceo_brain.jira_tools import JIRA_TOOL_SCHEMAS
                tools_by_mcp[name] = list(JIRA_TOOL_SCHEMAS)
                continue
            if name == "tasks_self":
                from app.ceo_brain.task_tools import TASK_TOOL_SCHEMAS
                tools_by_mcp[name] = list(TASK_TOOL_SCHEMAS)
                continue
            if not url:
                continue
            tools_by_mcp[name] = _mcp_list_tools(url)

        # 2. plan
        from datetime import datetime as _dt, timezone as _tz
        today_iso = (today or _dt.now(_tz.utc)).strftime("%Y-%m-%d")
        # FR-CB2-3.37 — same thread context to planner.
        plan_ctx = [
            m for m in (thread_history or [])
            if isinstance(m, dict) and m.get("content")
        ]
        if plan_ctx and plan_ctx[-1].get("content", "").strip() == last_user_q.strip():
            plan_ctx = plan_ctx[:-1]
        planned = plan_tool_calls(
            question=last_user_q,
            mcp_servers=request["mcp_servers"],
            tools_by_mcp=tools_by_mcp,
            anthropic_client=anthropic_client,
            today_iso=today_iso,
            thread_context=plan_ctx[-3:] if plan_ctx else None,
        )
        if not planned:
            # Planner failed — fall back to Anthropic-MCP gather
            # (FR-CB2-3.30) which still has its retry+wall-timeout.
            log.info(
                "ceo_brain_planner_empty_fallback_to_anthropic_mcp",
            )
            from app.ceo_brain.parallel_gather import (
                gather_from_mcps,
            )
            gathered_raw = gather_from_mcps(
                question=last_user_q,
                picked_mcps=request["mcp_servers"],
                anthropic_client=anthropic_client,
            )
        else:
            # 3. parallel direct HTTP — pass schemas for arg coercion
            #    plus local executors for `slack_self` virtual MCP.
            gathered_raw = gather_via_direct_http(
                planned_calls=planned,
                mcp_servers=request["mcp_servers"],
                tools_by_mcp=tools_by_mcp,
                local_tool_executors=tool_executors or None,
            )

        # 4. synthesize
        sys_blocks = request.get("system") or []
        sys_text = ""
        if isinstance(sys_blocks, list):
            for sb in sys_blocks:
                if isinstance(sb, dict) and sb.get("text"):
                    sys_text = sb["text"]
                    break
        final_text = synthesize_final_answer(
            question=last_user_q,
            gathered=gathered_raw,
            anthropic_client=anthropic_client,
            system_prompt_text=sys_text,
        )
        tool_uses: list[dict[str, Any]] = [
            {"name": label, "input": {}}
            for label, txt in gathered_raw.items() if txt
        ]
        rendered = format_final_response(
            text=final_text or
                "_(модель не сформулировала ответ — см. источники)_",
            tool_uses=tool_uses,
        )
        _final_render_to_slack(
            slack=slack, channel=channel,
            placeholder_ts=placeholder_ts,
            thread_ts=placeholder_thread_ts,
            text=rendered,
            sleep=sleep,
        )
        if db_session is not None:
            return persist_run(
                db_session,
                slack_channel_id=channel,
                slack_event_ts=slack_event_ts or placeholder_ts,
                slack_placeholder_ts=placeholder_ts,
                request_payload={
                    **request,
                    "_direct_http": True,
                    "planned_calls": planned,
                    "gathered_labels": list(gathered_raw.keys()),
                },
                response_text=final_text or "",
                tool_uses=tool_uses,
                status="done" if (final_text or "").strip() else "failed",
                cost_usd=0,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
        return None

    # FR-CB2-3.30 — per-MCP parallel gather (now unreachable for
    # MCP requests; kept for tests/back-compat only).
    if False and request.get("mcp_servers") and last_user_q:
        from app.ceo_brain.parallel_gather import (
            gather_from_mcps,
            synthesize_final_answer,
        )

        log.info(
            "ceo_brain_parallel_gather_start",
            mcp_count=len(request["mcp_servers"]),
            mcps=[s.get("name") for s in request["mcp_servers"]],
        )
        # Briefly update placeholder so operator sees activity.
        try:
            slack.chat_update(
                channel=channel, ts=placeholder_ts,
                text="🔄 параллельно дёргаю "
                + ", ".join(
                    (s.get("name") or "?")
                    for s in request["mcp_servers"]
                )
                + "…",
            )
        except Exception:  # noqa: BLE001
            pass

        gathered = gather_from_mcps(
            question=last_user_q,
            picked_mcps=request["mcp_servers"],
            anthropic_client=anthropic_client,
        )
        # Pull system prompt out of `request["system"]` for synth.
        sys_blocks = request.get("system") or []
        sys_text = ""
        if isinstance(sys_blocks, list):
            for sb in sys_blocks:
                if isinstance(sb, dict) and sb.get("text"):
                    sys_text = sb["text"]
                    break
        final_text = synthesize_final_answer(
            question=last_user_q,
            gathered=gathered,
            anthropic_client=anthropic_client,
            system_prompt_text=sys_text,
        )
        # Record tool_uses metadata from gather participants.
        tool_uses: list[dict[str, Any]] = [
            {"name": name, "input": {}}
            for name, txt in gathered.items() if txt
        ]
        rendered = format_final_response(
            text=final_text or
                "_(модель не сформулировала ответ — см. источники)_",
            tool_uses=tool_uses,
        )
        _final_render_to_slack(
            slack=slack, channel=channel,
            placeholder_ts=placeholder_ts,
            thread_ts=placeholder_thread_ts,
            text=rendered,
            sleep=sleep,
        )
        if db_session is not None:
            return persist_run(
                db_session,
                slack_channel_id=channel,
                slack_event_ts=slack_event_ts or placeholder_ts,
                slack_placeholder_ts=placeholder_ts,
                request_payload={
                    **request,
                    "_parallel_gather": True,
                    "gathered_mcps": list(gathered.keys()),
                },
                response_text=final_text or "",
                tool_uses=tool_uses,
                status="done" if (final_text or "").strip() else "failed",
                cost_usd=0,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
        return None

    # FR-CB2-3.22 — main turn goes through `messages.create` (non-
    # stream) because `messages.stream` + MCP + parallel tool_use
    # truncates the response before tool_results arrive. Recovery
    # (below) still uses `messages.stream`.
    if request.get("mcp_servers"):
        main_caller = anthropic_client.beta.messages.create
        stream_factory = anthropic_client.beta.messages.stream
        request = {**request, "betas": ["mcp-client-2025-04-04"]}
    else:
        main_caller = anthropic_client.messages.create
        stream_factory = anthropic_client.messages.stream

    text_buffer: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    last_update = [0.0]
    final_message: Any = None

    # FR-CB2-3.16 — outer tool-use loop. Each iteration runs one
    # Claude turn; if the turn ends with a `tool_use` block for a
    # *local* tool, execute it, append assistant+user turns, and
    # re-issue. MCP tool_uses are resolved by Anthropic server-side
    # so they don't trigger another iteration.
    final_response_failed = False
    # FR-CB2-3.24 — capture the original mcp_servers so progressive
    # degradation always degrades from the FULL set, not from an
    # already-degraded list.
    original_mcp_servers = list(request.get("mcp_servers") or [])
    for loop_idx in range(max(1, max_tool_loops)):
        main_ok = False
        for attempt in range(max(1, max_retries)):
            try:
                final_message = main_caller(**request)
                # Populate `tool_uses` from the returned content so
                # the Sources block + diagnostics still work.
                for block in (getattr(final_message, "content", None) or []):
                    btype = getattr(block, "type", None) or (
                        isinstance(block, dict) and block.get("type")
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
                # FR-CB2-3.22 v2 — surface tool-usage progress now
                # that we no longer stream. Operator gets a brief
                # «🔍 проверил: X, Y» between placeholder and final
                # answer instead of a static «🤔 думаю...» for the
                # entire 30-60 sec the model spends on MCP calls.
                if tool_uses:
                    uniq = []
                    for tu in tool_uses:
                        n = tu.get("name") or ""
                        if n and n not in uniq:
                            uniq.append(n)
                    progress = (
                        "🔍 проверил: " + ", ".join(uniq[:6])
                        + "\n\n_формулирую ответ…_"
                    )
                    try:
                        slack.chat_update(
                            channel=channel, ts=placeholder_ts,
                            text=progress,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                main_ok = True
                break
            except BaseException as exc:  # noqa: BLE001
                # FR-CB2-3.23 — transient MCP handshake error retries
                # alongside 429.
                if (_is_429(exc) or _is_transient_mcp_handshake(exc)) and attempt + 1 < max_retries:
                    # Exponential backoff: 2/4/8/16/32 sec for MCP
                    # handshake (n8n cloud workers sometimes cold-
                    # start slowly). 429 still honours Retry-After.
                    if _is_429(exc):
                        wait = _retry_after_seconds(exc, default=2.0)
                        reason = "rate-limit Anthropic"
                    else:
                        wait = min(2 ** (attempt + 1), 32)
                        reason = "MCP-сервер не отвечает на handshake"
                        # FR-CB2-3.24 — progressive degradation.
                        # `attempt` is 0-indexed inside this loop;
                        # _degrade uses 1-indexed for readability.
                        # Always degrade from the ORIGINAL set —
                        # otherwise we'd shrink from already-degraded.
                        next_attempt = attempt + 2
                        if original_mcp_servers:
                            new_servers = _degrade_mcp_servers(
                                original_mcp_servers, next_attempt,
                            )
                            if len(new_servers) != len(request.get("mcp_servers") or []):
                                log.info(
                                    "ceo_brain_responder_mcp_degraded",
                                    next_attempt=next_attempt,
                                    keeping=[
                                        s.get("name") for s in new_servers
                                    ],
                                )
                            request = {**request, "mcp_servers": new_servers}
                    log.info(
                        "ceo_brain_responder_transient_retry",
                        attempt=attempt + 1, wait_seconds=wait,
                        reason=reason, max_retries=max_retries,
                    )
                    try:
                        slack.chat_update(
                            channel=channel, ts=placeholder_ts,
                            text=(
                                f"⏳ {reason}, попытка "
                                f"{attempt + 2}/{max_retries}…"
                            ),
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
        if not main_ok:
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

    def _harvest_tool_result_text(msg: Any) -> str:
        """Pull every piece of text the tools produced into one flat
        string. Used to build the recovery prompt as a plain text
        dump — sidesteps any conversation-format issues with replaying
        `mcp_tool_use` / `mcp_tool_result` blocks back to the API."""
        if msg is None or not getattr(msg, "content", None):
            return ""
        parts: list[str] = []
        for block in msg.content:
            btype = getattr(block, "type", None) or (
                isinstance(block, dict) and block.get("type")
            )
            if btype == "text":
                txt = getattr(block, "text", None) or (
                    isinstance(block, dict) and block.get("text") or ""
                )
                if txt:
                    parts.append(str(txt))
            elif btype in {"mcp_tool_result", "tool_result"}:
                # content is a list of text-like sub-blocks for MCP;
                # plain string for non-MCP tool_result.
                cont = getattr(block, "content", None)
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
            # FR-CB2-3.17 v3 — flatten the tool conversation into a
            # single plain user message instead of replaying
            # mcp_tool_use / mcp_tool_result blocks (which Anthropic
            # silently rejects in some recovery scenarios, returning
            # empty content). This bypasses any conversation-format
            # quirk: the model gets a clean, structured text prompt
            # with the harvested tool data + the original question.
            harvested = _harvest_tool_result_text(final_message)
            # Diagnostic — counts both the raw block count of the
            # main turn and what survived into the harvested DATA
            # block. Empty harvest with non-empty block count means
            # we missed something (e.g. unrecognised block type).
            main_block_count = len(
                getattr(final_message, "content", None) or []
            )
            log.info(
                "ceo_brain_responder_recovery_harvest",
                main_blocks=main_block_count,
                harvested_chars=len(harvested),
                harvested_preview=harvested[:300],
            )
            original_question = ""
            for m in reversed(request.get("messages") or []):
                if m.get("role") == "user":
                    original_question = str(m.get("content") or "")
                    break
            recovery_user_msg = (
                "Ниже — собранные ранее данные из tool-вызовов "
                "(транскрипты встреч, задачи, сообщения, и т.п.):\n\n"
                "<DATA>\n"
                f"{harvested or '(нет данных)'}\n"
                "</DATA>\n\n"
                f"ИСХОДНЫЙ ВОПРОС ОПЕРАТОРА: {original_question}\n\n"
                "Напиши ПРЯМОЙ ответ на исходный вопрос на основе "
                "данных выше. Не вызывай tools. Если данных "
                "недостаточно — честно скажи об этом и укажи что "
                "конкретно отсутствует."
            )
            recovery_request = {
                k: v for k, v in request.items()
                if k not in {"mcp_servers", "tools", "betas"}
            }
            recovery_request["messages"] = [
                {"role": "user", "content": recovery_user_msg},
            ]
            # No MCP / beta blocks in the conversation → plain
            # `messages.stream` is the right path.
            recovery_stream_factory = anthropic_client.messages.stream
            with recovery_stream_factory(**recovery_request) as rec_stream:
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
                # Recovery attempted but model still produced no
                # text. Keep the intermediate planning text (if any)
                # so the operator at least sees what the bot was
                # doing, append an explicit note. Better UX than
                # discarding the planning entirely.
                planning = sdk_text.strip()
                if planning:
                    final_text = (
                        planning + "\n\n_(финальный синтез не "
                        "получился — см. результаты в Sources ниже)_"
                    )
                else:
                    final_text = ""
                log.warning(
                    "ceo_brain_responder_empty_text_recovery_no_text",
                )
        except Exception as e:  # noqa: BLE001
            # Recovery itself crashed. Same logic — preserve
            # planning if present so the operator sees something.
            planning = sdk_text.strip()
            if planning:
                final_text = (
                    planning + "\n\n_(финальный синтез не получился "
                    f"— recovery crashed: {type(e).__name__})_"
                )
            else:
                final_text = ""
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
