"""FR-CB2-3.31 — Tool-call planner via haiku.

Given an operator question + the catalog of available MCP tools
(fetched once via `mcp_client.list_tools` per endpoint), ask
`claude-haiku-4-5` to return a JSON list of `(mcp_name, tool_name,
arguments)` triplets to execute via direct HTTP.

This replaces Anthropic's MCP connector for the "decide what to
call" step. Each picked tool is then invoked through
`mcp_client.call_tool` in parallel.

The planner is intentionally lightweight — one haiku call, JSON
output, no tool use. ~1 sec latency. If the model returns garbage
JSON, fall back to a single MCP with the operator's question as
the only call.
"""
from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


# FR-CB2-3.33 — operator-pinned 2026-05-20: explicit per-tool
# descriptions that override n8n's default `description` field
# (often too short / vague). Keys are (mcp_name, tool_name).
# When a tool is not in this map, the planner falls back to n8n's
# own description.
_OPERATOR_TOOL_DESCRIPTIONS: dict[tuple[str, str], str] = {
    # n8n_calendar — Zoom встречи + Fireflies транскрипты
    ("n8n_calendar", "search_zoom_meetings"): (
        "ВНУТРЕННИЕ Zoom-встречи Humanoid (синки команды, дейли, "
        "Fundraising daily, Strategic Investors, planning sessions "
        "и т.п. — всё что записывалось в Humanoid Zoom). Ищет по "
        "query (тема или имя участника), фильтр по date_from / "
        "date_after. Возвращает список с Zoom ID, Title, "
        "участниками. БЕРИ ОТСЮДА Zoom ID для последующего "
        "get_zoom_transcript."
    ),
    ("n8n_calendar", "search_meetings"): (
        "ВНЕШНИЕ встречи (звонки с инвесторами, партнёрами, "
        "клиентами — кто-то не из @thehumanoid.ai). Транскрипты в "
        "Fireflies. Ищет по query (тема, компания, имя контрпарти) "
        "за период. Возвращает meeting_id для последующего "
        "get_meeting."
    ),
    ("n8n_calendar", "get_zoom_transcript"): (
        "ПОЛНЫЙ транскрипт внутренней Zoom-встречи по `zoom_id`. "
        "Зови ОБЯЗАТЕЛЬНО после search_zoom_meetings — без "
        "транскрипта нельзя ответить на вопросы «что сказал», "
        "«что обсудили». Можешь оставить zoom_id пустым — engine "
        "сам подставит первый из search-результата. "
        "ВАЖНО: `search_fragment` ВСЕГДА оставляй ПУСТОЙ СТРОКОЙ — "
        "тебе нужен ПОЛНЫЙ транскрипт без фильтра. Имя человека "
        "или ключевое слово ИСКАТЬ В ОТВЕТЕ ТЫ САМ из полного "
        "текста — n8n при заполненном search_fragment отдаёт только "
        "буквальные совпадения и пропускает варианты (Ира/Ирина/"
        "Irina/Shipilova), что приводит к пустому ответу."
    ),
    ("n8n_calendar", "get_meeting"): (
        "ПОЛНЫЙ транскрипт + summary + action items внешней "
        "встречи по `meeting_id`. Зови после search_meetings. "
        "Используй для разборов звонков с инвесторами / "
        "контрагентами."
    ),
    ("n8n_calendar", "search_zoom_tasks"): (
        "Задачи (action items) извлечённые из транскриптов "
        "внутренних встреч. Фильтр по owner, priority, query, "
        "zoom_id. Используй для вопросов «что я должен сделать», "
        "«какие задачи у X», «что осталось по митингу Y»."
    ),
    # n8n_drive — Telegram переписка (несмотря на имя)
    ("n8n_drive", "get_chats"): (
        "Список Telegram-чатов оператора с метаданными "
        "(chat_name, message_count). Используй чтобы УЗНАТЬ "
        "точное chat_name для последующего search_messages."
    ),
    ("n8n_drive", "search_messages"): (
        "Поиск сообщений в Telegram. Можно фильтр по chat_name "
        "(используй get_chats чтобы узнать имя), sender_name "
        "(кто писал), query (текст). Если интересуют ВСЕ "
        "сообщения от человека за период — оставь query пустым, "
        "поставь sender_name. Если интересны сообщения по теме в "
        "конкретном чате — chat_name + query."
    ),
    # n8n_main — Google Drive / Sheets
    ("n8n_main", "gdrive_search"): (
        "Поиск файлов в Google Drive по названию или содержимому. "
        "Возвращает webViewLink + id. Используй чтобы найти Doc, "
        "Sheet, файлы по теме."
    ),
    ("n8n_main", "gdrive_list_sheets"): (
        "Список листов внутри Google Sheets файла."
    ),
    ("n8n_main", "gdrive_read_sheet"): (
        "Чтение содержимого конкретного листа Google Sheets."
    ),
    ("n8n_main", "gdrive_read"): (
        "Чтение содержимого Google Doc по id."
    ),
    # n8n_gmail — LinkedIn search (NOT Gmail!)
    ("n8n_gmail", "humanoid_mcp_linkedin"): (
        "Поиск людей в LinkedIn по имени / компании / ключевым "
        "словам. Возвращает должность, компанию, LinkedIn URL, "
        "preview сообщения. Несмотря на имя tool, это LinkedIn, "
        "НЕ Gmail."
    ),
    # n8n_rocketreach — контакты
    ("n8n_rocketreach", "rocketreach_person_search"): (
        "Поиск людей в RocketReach по имени / компании. "
        "Возвращает email, телефон, LinkedIn, текущая роль. Для "
        "cold-outreach."
    ),
    ("n8n_rocketreach", "rocketreach_get_profile"): (
        "Полный профиль RocketReach по id."
    ),
    ("n8n_rocketreach", "rocketreach_lookup_by_linkedin"): (
        "Найти RocketReach-профиль по LinkedIn URL."
    ),
    ("n8n_rocketreach", "rocketreach_lookup_by_email"): (
        "Найти RocketReach-профиль по email."
    ),
    ("n8n_rocketreach", "rocketreach_lookup_by_id"): (
        "RocketReach-профиль по внутреннему id."
    ),
    # n8n_hubspot — CRM
    ("n8n_hubspot", "hubspot_search_companies"): (
        "Поиск компаний в HubSpot CRM. Возвращает домен, "
        "описание, ARR и т.п. Используй для вопросов про "
        "инвесторов / клиентов / партнёров в CRM."
    ),
    ("n8n_hubspot", "hubspot_search_contacts"): (
        "Поиск контактов в HubSpot CRM. Возвращает email, роль, "
        "компанию, owner."
    ),
    # slack_self — local Slack tools
    ("slack_self", "slack_list_channels"): (
        "Список каналов в Slack куда добавлен бот. Возвращает id, "
        "name, is_private, is_im. Используй для «в каких каналах "
        "ты», «список каналов»."
    ),
    ("slack_self", "slack_search"): (
        "Полнотекстовый поиск сообщений в Slack по query. "
        "Требует user-токен (xoxp). Возвращает permalink, ts, "
        "channel, превью."
    ),
    ("slack_self", "slack_get_channel_history"): (
        "История канала/DM — N последних сообщений. Channel = "
        "ID `Cxxxx` или `Dxxxx`."
    ),
    ("slack_self", "slack_get_thread_replies"): (
        "Все сообщения в треде по channel + thread_ts."
    ),
    ("slack_self", "slack_post_message"): (
        "Отправить сообщение в канал / DM / тред от имени бота. "
        "Используй когда оператор просит «напиши в чат X»."
    ),
    ("slack_self", "slack_users_info"): (
        "Профиль Slack-юзера по ID `Uxxxx` — name, email, tz."
    ),
    ("slack_self", "slack_users_lookup_by_email"): (
        "Найти Slack-юзера по email."
    ),
    ("slack_self", "slack_get_permalink"): (
        "Получить permalink на сообщение по channel + ts."
    ),
}


def _describe_tool(mcp_name: str, tool: dict) -> str:
    """Operator-provided override > tool's native description."""
    tname = tool.get("name") or "?"
    override = _OPERATOR_TOOL_DESCRIPTIONS.get((mcp_name, tname))
    if override:
        return override
    return (tool.get("description") or "").strip()


def _tool_catalog_lines(
    mcp_servers: list[dict],
    tools_by_mcp: dict[str, list[dict]],
) -> str:
    """Render a compact catalog: for each MCP, list its tool names
    + first line of description + REQUIRED args marked with `*`."""
    lines: list[str] = []
    for srv in mcp_servers:
        name = srv.get("name") or "?"
        lines.append(f"\n[{name}]")
        tools = tools_by_mcp.get(name) or []
        if not tools:
            lines.append("  (no tools listed)")
            continue
        for t in tools:
            tname = t.get("name") or "?"
            # FR-CB2-3.33 — operator-curated descriptions override
            # whatever n8n returns; falls back to n8n's text.
            desc_raw = _describe_tool(name, t)
            desc = desc_raw.strip().replace("\n", " ")[:400]
            schema = t.get("inputSchema") or {}
            if isinstance(schema, dict):
                props = schema.get("properties") or {}
                required = set(schema.get("required") or [])
            else:
                props, required = {}, set()
            arg_parts = []
            for pname in props.keys():
                arg_parts.append(
                    f"{pname}*" if pname in required else pname
                )
            arg_str = ", ".join(arg_parts)
            req_note = ""
            if required:
                req_note = (
                    f" [required: {', '.join(sorted(required))}]"
                )
            lines.append(
                f"  - {tname}({arg_str}){req_note} — {desc}"
            )
    return "\n".join(lines)


def plan_tool_calls(
    *,
    question: str,
    mcp_servers: list[dict],
    tools_by_mcp: dict[str, list[dict]],
    anthropic_client: Any,
    today_iso: str | None = None,
    thread_context: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Ask haiku to plan the tool calls. Returns a list of
    ``{mcp: name, tool: name, args: dict, reason: str}`` triplets.
    Empty list on failure (caller decides fallback).

    FR-CB2-3.37 — ``thread_context`` is the last 2-3 messages from
    the Slack thread (oldest first, role+content). When present,
    rendered into the prompt so follow-up questions like «а за
    вчера» resolve correctly.
    """
    if not mcp_servers or not question:
        return []
    catalog = _tool_catalog_lines(mcp_servers, tools_by_mcp)
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
        f"Сегодня: {today_iso or '<today>'}\n\n"
        f"{context_block}"
        f"Текущий вопрос оператора: {question}\n\n"
        f"Доступные MCP-серверы и их tools:\n{catalog}\n\n"
        "Задача: выбери список tool-вызовов которые надо сделать "
        "чтобы ответить на ЭТОТ вопрос (учитывая контекст треда "
        "выше, если есть). Возвращай ТОЛЬКО JSON, без "
        "markdown-блоков. Формат:\n"
        "{\n  \"calls\": [\n"
        "    {\"mcp\":\"<name>\",\"tool\":\"<tool_name>\",\"args\":{...}},\n"
        "    ...\n  ]\n}\n\n"
        "Правила:\n"
        "- ОБЯЗАТЕЛЬНО задействуй КАЖДЫЙ MCP из списка выше — "
        "минимум 1 tool-call в каждый. Если classifier подключил "
        "и n8n_calendar и n8n_drive — план ДОЛЖЕН содержать "
        "calls в оба, не только в один. Иначе тратим работу "
        "classifier'а впустую.\n"
        "- Минимум tool-вызовов чтобы ответить — но не меньше "
        "числа подключённых MCP. Обычно 2-5.\n"
        "- ВСЕ args ОБЯЗАТЕЛЬНО как STRINGS — даже числовые. "
        "`limit: \"5\"` а НЕ `limit: 5`. Иначе schema error.\n"
        "- ВСЕ required-args в каталоге помечены `*` или [required: ...]. "
        "ВКЛЮЧАЙ их ВСЕГДА, даже если значение пустое: `chat_name: "
        "\"\"`, `sender_name: \"\"` и т.п. Иначе n8n ругается «Required → "
        "at <field>».\n"
        "- QUERY для search-тулзов: НЕ комбинируй языки в один query "
        "(токенизатор n8n трактует «фандрайзинг fundraising» как AND, "
        "редко что находит). Один язык за раз. Названия встреч в "
        "Zoom часто английские («Fundraising daily», «Strategic "
        "Investors») — пиши query на английском для них. Имена "
        "людей хорошо индексируются на русском («Ира», «Алина», "
        "«Йохан»). Если не уверен — сделай ДВА отдельных search-call'а "
        "с разными query (один на тему-EN, один на имя-RU); система "
        "выполнит параллельно и подберёт лучшее.\n"
        "- Если в вопросе есть имя — search по имени часто точнее "
        "search по теме. Например для «что Ира сказала на встрече» "
        "лучше `query: \"Ира\"` чем `query: \"фандрайзинг\"`.\n"
        "- Для search_messages в n8n_drive когда фильтр по "
        "`sender_name` — query можно оставить ПУСТЫМ (`\"\"`), "
        "тогда вернётся ВСЁ что писал этот человек за период. Иначе "
        "n8n требует AND match по query И sender → редко находит.\n"
        "- ОБЯЗАТЕЛЬНО двухступенчатый план для встреч/звонков: "
        "сначала `search_zoom_meetings` или `search_meetings` чтобы "
        "найти meeting_id/zoom_id → СРАЗУ ЖЕ добавляй "
        "`get_zoom_transcript` или `get_meeting` БЕЗ id-args — наш "
        "engine сам подставит zoom_id из результата search. Если id "
        "известен из контекста — передай его явно.\n"
        "- Если вопрос про Telegram-сообщения / переписку (имена "
        "коллег вроде Алина, Ира, Артем, Дима, упоминания «вчера», "
        "«сегодня» в контексте сообщений) → ОБЯЗАТЕЛЬНО "
        "`search_messages` в n8n_drive.\n"
        "- Если вопрос содержит И встречу И сообщения → включи "
        "ОБА: и search_zoom_meetings (+ transcript), и "
        "search_messages.\n"
        "- Если нужны компании/контакты → search в hubspot/rocketreach.\n"
        "- НЕ дублируй один и тот же call с одинаковыми args.\n"
        "- НЕ ВЫДУМЫВАЙ tool-имена или args-ключи: бери только из "
        "каталога выше."
    )
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_planner_call_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return []
    raw = ""
    for b in (getattr(resp, "content", None) or []):
        if getattr(b, "type", None) == "text":
            raw += getattr(b, "text", "") or ""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        log.info("ceo_brain_planner_bad_json", raw=raw[:300])
        return []
    if not isinstance(parsed, dict):
        return []
    calls = parsed.get("calls") or []
    if not isinstance(calls, list):
        return []
    # Validate each call.
    name_to_server = {s.get("name"): s for s in mcp_servers}
    tool_names_per_mcp = {
        m: {t.get("name") for t in (tools_by_mcp.get(m) or [])}
        for m in name_to_server
    }
    out: list[dict[str, Any]] = []
    for item in calls:
        if not isinstance(item, dict):
            continue
        mcp_name = item.get("mcp")
        tool_name = item.get("tool")
        args = item.get("args") or {}
        if not mcp_name or not tool_name:
            continue
        if mcp_name not in name_to_server:
            continue
        if tool_name not in (tool_names_per_mcp.get(mcp_name) or set()):
            log.info(
                "ceo_brain_planner_unknown_tool",
                mcp=mcp_name, tool=tool_name,
            )
            continue
        if not isinstance(args, dict):
            args = {}
        out.append({
            "mcp": mcp_name,
            "tool": tool_name,
            "args": args,
        })
    # FR-CB2-3.38 — defensive coverage. Operator-observed 2026-05-20:
    # classifier picked both n8n_drive AND n8n_calendar, but planner
    # only emitted a single n8n_drive call → today's Zoom meeting
    # was skipped. Backfill: for each MCP the classifier picked but
    # planner ignored, inject a sensible default search call.
    used_mcps = {c["mcp"] for c in out}
    expected_mcps = {s.get("name") for s in mcp_servers if s.get("name")}
    missing_mcps = expected_mcps - used_mcps
    for mcp_name in missing_mcps:
        # Pick the first "search_*" tool in this MCP as the default
        # fallback call. Args are empty / question-derived.
        tools = tools_by_mcp.get(mcp_name) or []
        search_tool = next(
            (t for t in tools if (t.get("name") or "").startswith("search")),
            None,
        )
        if not search_tool:
            continue
        tname = search_tool.get("name")
        # Build minimal args: just include `query=question[:60]`
        # if there's a `query` property. Required args get filled
        # by mcp_client._coerce_args_to_schema.
        schema = search_tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        args: dict[str, Any] = {}
        if "query" in props:
            args["query"] = question[:60]
        out.append({"mcp": mcp_name, "tool": tname, "args": args})
        log.info(
            "ceo_brain_planner_backfill",
            mcp=mcp_name, tool=tname,
        )
    # Post-process: force `search_fragment=""` on get_zoom_transcript
    # / get_meeting so we always receive the FULL transcript. The
    # synthesis LLM later extracts specific quotes / names from the
    # whole text — n8n's substring filter misses variants like
    # «Ира» vs «Ирина» / «Irina» / «Shipilova» and was making the
    # bot answer «не нашёл реплик» on perfectly normal questions.
    for c in out:
        tname = (c.get("tool") or "").lower()
        if tname in {"get_zoom_transcript", "get_meeting"}:
            args = c.setdefault("args", {})
            if args.get("search_fragment"):
                log.info(
                    "ceo_brain_planner_cleared_search_fragment",
                    tool=tname,
                    original=args["search_fragment"],
                )
            args["search_fragment"] = ""
    log.info(
        "ceo_brain_planner_plan",
        question_chars=len(question), calls=len(out),
        plan=[
            {"mcp": c["mcp"], "tool": c["tool"]}
            for c in out
        ],
    )
    return out


__all__ = ["plan_tool_calls"]
