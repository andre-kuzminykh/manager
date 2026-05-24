"""Render proposed action_drafts as a unified, direction-grouped digest via LLM.

Queries `action_drafts` (+ `intent_inferences` for source). Direction уже
проставлен на ИНГЕСТЕ (gpt-4o-mini, payload["direction"]) — дайджест читает
его и фильтрует. Бэклог без direction доклассифицируется gpt-4o-mini и
сохраняется (один раз). Затем LLM (gpt-5.5) даёт на каждую задачу entity
`group` (Tether / XTX / …), `function`, `action`, `critical`. Рендер:
DIRECTION → ENTITY-кластер (≥2) → функциональные под-группы; ответственный
резолвится в нормальное имя из team_members.

Slack: главное сообщение = критичные задачи, остальное — в тред.

The LLM only classifies + rephrases per task (JSON-per-id), so no task can
be silently dropped: missing ids are re-requested once, and the render is
fully deterministic.

By default the script ONLY previews (renders the exact Slack-mrkdwn message
and prints it) — it does NOT post to Slack. Pass --send to actually post;
that is the flag a cron/timer would use for the morning digest.

Фильтр по стратегическим направлениям ВКЛЮЧЁН по умолчанию (--all чтобы
выключить и показать «other» тоже).

Usage:
    # ПРЕВЬЮ (ничего не отправляется) — проверить, что выведется:
    docker exec manager-zoom-ff-1 python -m ops.strategic_tasks_digest \\
        --since 2025-05-22

    # РЕАЛЬНАЯ отправка в Slack-бот (для таймера/cron):
    docker exec manager-zoom-ff-1 python -m ops.strategic_tasks_digest \\
        --since 2025-05-22 --send

Slack target: --slack-channel / AUTO_SEND_TO_SLACK_CHANNEL (DM или канал),
token из settings-поля --slack-token-key / AUTO_SEND_TO_SLACK_TOKEN_KEY
(default ceo_brain_slack_bot_token — DM CEO Brain бота).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from openai import OpenAI

from app.config import get_settings
from app.db import session_scope
from app.intent.llm_backends import OpenAIBackend
from app.logging_setup import get_logger, setup_logging
from app.models.intent import ActionDraft, ActionDraftState, IntentInference
from app.services.task_direction import (
    DIRECTIONS_IMPORTANT,
    classify_directions,
)

log = get_logger(__name__)


# Группировка идёт по стратегическим НАПРАВЛЕНИЯМ — тем самым, по которым
# работает фильтр (DIRECTIONS_IMPORTANT из task_direction). «other» нужен
# только когда запускаемся БЕЗ --strategic-only.
DIRECTION_ORDER: tuple[str, ...] = (
    "investors",
    "budget",
    "deliverables",
    "beta",
    "design",
    "other",
)

DIRECTION_LABELS: dict[str, str] = {
    "investors": "Инвесторы / Fundraising",
    "budget": "Бюджет",
    "deliverables": "Ключевые deliverables",
    "beta": "Бета / Релизы",
    "design": "Дизайн",
    "other": "Прочее",
}

# Функциональные под-категории для задач БЕЗ именованного кластера контрагента.
# Заменяют прежнее «Разное»: распределяем по типу действия.
FUNCTIONS: tuple[str, ...] = (
    "Follow-up и напоминания",
    "Интро и знакомства",
    "Материалы и документы",
    "Звонки и встречи",
    "Аутрич и письма",
    "Pipeline и операционка",
    "Ресёрч и контакты",
    "Прочее",
)
_FUNCTION_SET = frozenset(FUNCTIONS)
_FUNCTION_HINTS: dict[str, str] = {
    "Follow-up и напоминания": "напомнить, follow-up, на контроль, мониторить статус",
    "Интро и знакомства": "сделать/запросить интро, познакомить, соединить",
    "Материалы и документы": "отправить дек/NDA/презентацию, открыть data room, поделиться файлом",
    "Звонки и встречи": "назначить/провести звонок, созвон, встречу, демо",
    "Аутрич и письма": "первичный аутрич, написать/отправить письмо контакту",
    "Pipeline и операционка": "добавить в фолловеры/pipeline, таблицы, колонки, статусы, memo",
    "Ресёрч и контакты": "проверить тёплые контакты, найти выходы, ресёрч фонда",
    "Прочее": "не подходит ни под одну категорию выше",
}

FORMAT_SYSTEM_PROMPT = (
    "Ты — ассистент CEO, собираешь утренний дайджест «Задачи на сегодня» "
    "(в основном fundraising / инвесторы). На КАЖДУЮ задачу верни:\n"
    "1. group — каноническое короткое имя контрагента/фонда/человека, "
    "о ком задача (1-3 слова: «Tether», «XTX», «20VC», «Robostrategy»). "
    "Для задач про ОДНОГО И ТОГО ЖЕ контрагента используй ОДИНАКОВЫЙ group "
    "(точно та же строка). Если конкретного контрагента нет — пустая строка \"\".\n"
    "2. function — РОВНО одна из функциональных категорий (тип действия):\n"
    + "\n".join(f"   - {f}: {_FUNCTION_HINTS[f]}" for f in FUNCTIONS)
    + "\n3. action — ПОЛНАЯ, самодостаточная формулировка задачи: ЧТО "
    "сделать + КОМУ / С КЕМ / ЧТО ИМЕННО (получатель, контрагент, предмет: "
    "какой дек/NDA/презентация/письмо). Читатель должен понять задачу БЕЗ "
    "доп. контекста. Извлекай получателя и предмет ИЗ ДАННЫХ задачи.\n"
    "   Плохо: «Отправить NDA и дек». Хорошо: «Отправить NDA и инвест-дек "
    "Series A контакту Михаилу из фонда X».\n"
    "   Если в данных есть статус/история (даты, что уже делали, ответ "
    "контакта) — кратко добавь: «05/05 ответили — 12/05 напомнили — "
    "напомнить последний раз».\n"
    "4. critical — true ТОЛЬКО для реально горящего: дедлайн сегодня/просрочен, "
    "«напомнить последний раз», ждём ответ и нужно толкнуть сегодня, активная "
    "крупная сделка с действием прямо сейчас. Иначе false.\n\n"
    "ПРАВИЛА:\n"
    "- НЕ включай в action имя ответственного (исполнителя) — его подставят "
    "отдельно. Но ПОЛУЧАТЕЛЯ/контакт, КОМУ адресована задача, — включай.\n"
    "- НЕ выдумывай даты, имена и факты. Только то, что есть в данных задачи; "
    "если получатель в данных не указан — не придумывай, но сохрани максимум "
    "конкретики (предмет, фонд, цель), чтобы задача была понятной.\n"
    "- Лучше полная понятная формулировка, чем короткая, но невнятная.\n"
    "- Каждой задаче — РОВНО один объект. Ничего не выбрасывай и не "
    "объединяй разные задачи.\n\n"
    "Верни СТРОГО JSON: "
    '{"items":[{"id":<int>,"group":"<контрагент или \\"\\">",'
    '"function":"<категория>","action":"<что сделать>","critical":<bool>}]}'
)


def _build_tasks(drafts: list[ActionDraft]) -> list[dict]:
    """Flatten drafts into the dict shape used downstream."""
    out: list[dict] = []
    for d in drafts:
        payload = d.payload or {}
        pending = payload.get("_pending") or {}
        title = (payload.get("title") or "").strip()
        desc = (payload.get("description") or "").strip()
        src_text = (pending.get("source_text") or "").strip()
        owner = (payload.get("owner_display_name") or "").strip()
        out.append(
            {
                "id": d.id,
                "title": title,
                "description": desc,
                "source_kind": pending.get("source_kind") or "?",
                "source_text": src_text,
                "owner": owner,
                "due_date": (payload.get("due_date") or "").strip(),
                "priority": (payload.get("priority") or "medium").strip().lower(),
                # direction уже проставлен на ингесте (gpt-4o-mini). Пусто →
                # бэклог до этой фичи; дайджест доклассифицирует и сохранит.
                "direction": (payload.get("direction") or "").strip().lower(),
            }
        )
    return out


def _assign_directions(
    tasks: list[dict], *, llm, classify_model, strategic_only: bool
) -> tuple[list[dict], dict[int, str]]:
    """Use the STORED direction (set on ingest). Only tasks без direction
    (бэклог до фичи) доклассифицируем gpt-4o-mini, маленькими чанками +
    дозапрос. Возвращает (отфильтрованные_задачи, newly) — newly нужно
    сохранить в payload драфтов.
    """
    todo = [t for t in tasks if not t.get("direction")]

    def _classify(batch: list[dict]) -> dict[int, str]:
        return classify_directions(
            tasks=[
                {
                    "id": t["id"],
                    "title": t["title"],
                    "description": t["description"] or t["source_text"][:300],
                }
                for t in batch
            ],
            meeting_context=None,
            llm_backend=llm,
            model=classify_model,
        )

    mapping: dict[int, str] = {}
    chunk = 25  # gpt-4o-mini дропает на больших батчах — держим мелко
    for i in range(0, len(todo), chunk):
        mapping.update(_classify(todo[i : i + chunk]))
    miss = [t for t in todo if t["id"] not in mapping]
    if miss:
        mapping.update(_classify(miss))

    newly: dict[int, str] = {}
    for t in todo:
        d = mapping.get(t["id"])
        if d:
            t["direction"] = d
            newly[t["id"]] = d
        else:
            t["direction"] = "other"  # не сохраняем — перепробуем в след. раз

    already = len(tasks) - len(todo)
    if todo:
        print(f"  classify: {already} уже классифицированы (из ingest), "
              f"доклассифицировано gpt-4o-mini {len(newly)}/{len(todo)}")
    else:
        print(f"  classify: все {len(tasks)} уже классифицированы — LLM не звали")

    if strategic_only:
        kept = [t for t in tasks if t["direction"] in DIRECTIONS_IMPORTANT]
        print(f"  фильтр: {len(kept)}/{len(tasks)} в {DIRECTIONS_IMPORTANT}")
        return kept, newly
    return tasks, newly


def _format_chunk(batch: list[dict], *, llm, model) -> dict[int, dict]:
    """Ask the LLM to section + rephrase one chunk. Returns {id: {...}}."""
    lines = []
    for t in batch:
        ctx = " | ".join(
            p for p in (t["description"], t["source_text"]) if p
        ) or t["title"]
        lines.append(
            f'id={t["id"]} | title={t["title"][:160]} | контекст={ctx[:600]}'
        )
    user_prompt = (
        f"ЗАДАЧИ ({len(batch)}):\n" + "\n".join(lines) + "\n\nВерни JSON items."
    )
    raw = llm.complete_text(
        system_prompt=FORMAT_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=model,
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )
    out: dict[int, dict] = {}
    if not raw or not raw.strip():
        return out
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except Exception as e:  # noqa: BLE001
        log.warning("digest_format_json_parse_error", error=str(e), raw=raw[:200])
        return out
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            tid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        func = (it.get("function") or "Прочее").strip()
        if func not in _FUNCTION_SET:
            func = "Прочее"
        out[tid] = {
            "group": (it.get("group") or "").strip(),
            "function": func,
            "action": (it.get("action") or "").strip(),
            "critical": bool(it.get("critical")),
        }
    return out


def _format_all(tasks: list[dict], *, llm, model) -> dict[int, dict]:
    """Section + rephrase every task, chunked, with one re-request for misses."""
    by_id = {t["id"]: t for t in tasks}
    result: dict[int, dict] = {}
    chunk = 40
    for i in range(0, len(tasks), chunk):
        result.update(_format_chunk(tasks[i : i + chunk], llm=llm, model=model))
    missing = [by_id[i] for i in by_id if i not in result]
    if missing:
        print(f"  дозапрос {len(missing)} пропущенных...")
        result.update(_format_chunk(missing, llm=llm, model=model))
    # Final fallback for anything still missing — use the raw title.
    for tid, t in by_id.items():
        if tid not in result:
            result[tid] = {
                "group": "",
                "function": "Прочее",
                "action": (t["title"][:60] or "уточнить"),
                "critical": False,
            }
    return result


def _normalize_owner(
    owner: str,
    *,
    by_username: dict[str, str],
    by_name: dict[str, str],
) -> str:
    """Resolve the responsible to a REAL name from team_members.

    - «@handle» → real_name по telegram_username; если в таблице нет —
      возвращаем сам handle (без @), НИЧЕГО не выдумываем.
    - имя со суффиксом роли («Валентина - PM /аналитик») → сначала режем
      суффикс, потом матчим по таблице (полное имя или первое слово) →
      канон real_name; если не нашли — оставляем как есть.
    Возвращает '' если owner пуст.
    """
    o = (owner or "").strip()
    if not o:
        return ""
    if o.startswith("@"):
        uname = o[1:].strip().lower()
        return by_username.get(uname, o[1:])
    base = o
    for sep in (" - ", " — ", " /", " ("):
        if sep in base:
            base = base.split(sep, 1)[0].strip()
    key = base.lower()
    if key in by_name:
        return by_name[key]
    first = key.split()[0] if key.split() else key
    if first in by_name:
        return by_name[first]
    return base


def _render(
    tasks: list[dict],
    formatted: dict[int, dict],
    *,
    title: str = "Задачи на сегодня",
    flavor: str = "md",
    start_n: int = 0,
) -> str:
    """Render a digest section: DIRECTION (filter category) → ENTITY cluster
    (Tether / XTX / … — контрагенты с ≥2 задачами) → then the rest split by
    FUNCTION (Follow-up / Интро / Материалы / …). Each task numbered, with the
    responsible in parens.

    flavor="md" → markdown; flavor="slack" → Slack mrkdwn (`*bold*`).
    `start_n` lets the thread continue numbering after the parent.
    """
    slack = flavor == "slack"
    h1 = (lambda s: f"*{s}*") if slack else (lambda s: f"# {s}")
    h2 = (lambda s: f"*{s}*") if slack else (lambda s: f"## {s}")
    h3 = (lambda s: f"*{s}*") if slack else (lambda s: f"### {s}")

    by_id = {t["id"]: t for t in tasks}
    if not by_id:
        return h1(title)
    dir_buckets: dict[str, list[int]] = {d: [] for d in DIRECTION_ORDER}
    for tid, t in by_id.items():
        dir_buckets.setdefault(t.get("direction") or "other", []).append(tid)

    def _grp(tid: int) -> str:
        return (formatted[tid].get("group") or "").strip()

    def _func(tid: int) -> str:
        f = formatted[tid].get("function") or "Прочее"
        return f if f in _FUNCTION_SET else "Прочее"

    def _action(tid: int) -> str:
        return formatted[tid].get("action") or "уточнить"

    out: list[str] = [h1(title)]
    n = start_n
    for d in DIRECTION_ORDER:
        ids = dir_buckets.get(d) or []
        if not ids:
            continue
        out.append("")
        out.append(h2(DIRECTION_LABELS.get(d, d)))

        # 1) named entity clusters (counterparty with >=2 tasks)
        ent: dict[str, list[int]] = {}
        for tid in ids:
            g = _grp(tid)
            if g:
                ent.setdefault(g, []).append(tid)
        multi = sorted(
            (g for g, v in ent.items() if len(v) >= 2),
            key=lambda g: (-len(ent[g]), g.lower()),
        )
        clustered = {tid for g in multi for tid in ent[g]}
        for g in multi:
            out.append("")
            out.append(h3(g))
            for tid in sorted(ent[g]):
                n += 1
                out.append(f"{n}. {_action(tid)} ({by_id[tid]['owner'] or '—'})")

        # 2) the rest → functional sub-groups (Follow-up / Интро / …)
        rest = [tid for tid in ids if tid not in clustered]
        func_buckets: dict[str, list[int]] = {}
        for tid in rest:
            func_buckets.setdefault(_func(tid), []).append(tid)
        for func in FUNCTIONS:
            fids = func_buckets.get(func) or []
            if not fids:
                continue
            out.append("")
            out.append(h3(func))
            for tid in sorted(fids):
                n += 1
                g = _grp(tid)
                prefix = f"{g} — " if g else ""
                out.append(f"{n}. {prefix}{_action(tid)} ({by_id[tid]['owner'] or '—'})")
    return "\n".join(out).rstrip()


def _post_to_slack(parent_text: str, rest_text: str, *, channel: str, token: str) -> dict:
    """Parent message = critical digest; everything else goes into the thread."""
    from slack_sdk import WebClient

    from app.services.slack_mirror import SLACK_TEXT_CHUNK_CHARS, _split_for_slack

    p_chunks = _split_for_slack(parent_text, limit=SLACK_TEXT_CHUNK_CHARS)
    thread_chunks = list(p_chunks[1:]) + (
        _split_for_slack(rest_text, limit=SLACK_TEXT_CHUNK_CHARS) if rest_text.strip() else []
    )
    client = WebClient(token=token)
    resp = client.chat_postMessage(
        channel=channel, text=p_chunks[0], unfurl_links=False, unfurl_media=False
    )
    parent_ts = (resp.data or {}).get("ts")
    for c in thread_chunks:
        client.chat_postMessage(
            channel=channel, text=c, thread_ts=parent_ts,
            unfurl_links=False, unfurl_media=False,
        )
    return {"ok": True, "parent_ts": parent_ts, "thread_replies": len(thread_chunks)}


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--since",
        default="2025-05-22",
        help="ISO date (YYYY-MM-DD); drafts created at/after этой даты (UTC).",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="НЕ фильтровать по стратегическим направлениям (включить и "
        "«other»). По умолчанию фильтр ВКЛЮЧЁН — берём только "
        "investors/budget/design/beta/deliverables.",
    )
    ap.add_argument("--model", default=None,
                    help="Модель форматирования (subject/action). Default — "
                    "fireflies_tasks_model (gpt-5.5).")
    ap.add_argument("--classify-model", default=None,
                    help="Модель доклассификации направлений для бэклога. "
                    "Default — openai_model (gpt-4o-mini в проде).")
    ap.add_argument("--limit", type=int, default=0, help="Cap for testing.")
    ap.add_argument(
        "--send",
        action="store_true",
        help="ОТПРАВИТЬ в Slack. По умолчанию OFF — только превью (что "
        "выведется), в Slack ничего не пишем. Таймер/cron запускает с --send.",
    )
    ap.add_argument(
        "--slack-channel",
        default=None,
        help="Канал/DM. Default — env AUTO_SEND_TO_SLACK_CHANNEL.",
    )
    ap.add_argument(
        "--slack-token-key",
        default=None,
        help="Имя settings-поля с токеном. Default — env "
        "AUTO_SEND_TO_SLACK_TOKEN_KEY → ceo_brain_slack_bot_token.",
    )
    args = ap.parse_args()

    try:
        since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"ERROR: bad --since {args.since!r} (want YYYY-MM-DD)", file=sys.stderr)
        return 2

    s = get_settings()
    if not s.openai_api_key:
        print("ERROR: OPENAI_API_KEY empty", file=sys.stderr)
        return 2
    model = args.model or s.fireflies_tasks_model
    classify_model = args.classify_model or s.openai_model
    llm = OpenAIBackend(client=OpenAI(api_key=s.openai_api_key), model=model)

    with session_scope() as session:
        q = (
            session.query(ActionDraft)
            .join(IntentInference, ActionDraft.inference_id == IntentInference.id)
            .filter(ActionDraft.state == ActionDraftState.proposed)
            .filter(ActionDraft.created_at >= since_dt)
            .order_by(ActionDraft.id.asc())
        )
        drafts = q.all()
        if args.limit:
            drafts = drafts[: args.limit]

        print(f"\n{'='*70}")
        print(f"DIGEST: proposed drafts с {args.since} (model={model})")
        print(f"{'='*70}")
        print(f"  найдено proposed-драфтов: {len(drafts)}")

        tasks = _build_tasks(drafts)
        if not tasks:
            print("  нет задач — выходим.")
            session.rollback()
            return 0

        tasks, newly = _assign_directions(
            tasks, llm=llm, classify_model=classify_model,
            strategic_only=not args.all,
        )
        # Persist backfilled directions onto the drafts (one-time для бэклога).
        if newly:
            draft_by_id = {d.id: d for d in drafts}
            for tid, d in newly.items():
                dr = draft_by_id.get(tid)
                if dr is None:
                    continue
                p = dict(dr.payload or {})
                p["direction"] = d
                dr.payload = p
            session.commit()
            print(f"  сохранил direction в {len(newly)} драфтов (бэклог)")
        if not tasks:
            print("  стратегических задач не найдено.")
            return 0

        # Резолвим ответственного в РЕАЛЬНОЕ имя из team_members
        # (мэтч по telegram_username и по имени). Без выдумок.
        from app.services.team_members import get_humans_for_matcher

        humans = get_humans_for_matcher(session)
        by_username: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for h in humans:
            rn = (h.get("real_name") or "").strip()
            if not rn:
                continue
            u = (h.get("tg_username") or "").strip().lower()
            if u:
                by_username[u] = rn
            by_name.setdefault(rn.lower(), rn)
            first = rn.lower().split()[0] if rn.split() else ""
            if first:
                by_name.setdefault(first, rn)
        for t in tasks:
            t["owner"] = _normalize_owner(
                t["owner"], by_username=by_username, by_name=by_name
            )

        print(f"  форматируем {len(tasks)} задач через LLM...")
        formatted = _format_all(tasks, llm=llm, model=model)

        # critical = LLM-флаг OR priority high/urgent → главное сообщение
        crit_tasks, rest_tasks = [], []
        for t in tasks:
            is_crit = bool(formatted[t["id"]].get("critical")) or (
                t.get("priority") in ("high", "urgent")
            )
            (crit_tasks if is_crit else rest_tasks).append(t)
        print(f"  критичных (в главное сообщение): {len(crit_tasks)}, "
              f"в тред: {len(rest_tasks)}")

        parent_text = _render(
            crit_tasks, formatted,
            title="Задачи на сегодня · критичное", flavor="slack",
        )
        if not crit_tasks:
            parent_text += "\n_Горящего на сегодня нет — полный список в треде._"
        rest_text = _render(
            rest_tasks, formatted,
            title="Остальные задачи", flavor="slack", start_n=len(crit_tasks),
        ) if rest_tasks else ""

        session.rollback()  # backfill direction уже закоммичен выше; здесь — discard прочего

    # Resolve Slack target (used for both preview and send).
    from app.services.slack_publish import _get_channel, _get_token_key

    channel = args.slack_channel or _get_channel()
    token_key = args.slack_token_key or _get_token_key()
    token = getattr(s, token_key, "") or ""

    if not args.send:
        # PREVIEW — ровно то, что уйдёт в Slack (mrkdwn), без отправки.
        print("\n" + "=" * 70)
        print(f"PREVIEW (НЕ отправлено). target channel={channel or '(не задан)'}, "
              f"token_key={token_key}, token={'set' if token else 'EMPTY'}")
        print("=" * 70)
        print("\n----- ГЛАВНОЕ СООБЩЕНИЕ -----\n")
        print(parent_text)
        print("\n----- ТРЕД (остальное) -----\n")
        print(rest_text or "(пусто)")
        print("\n" + "=" * 70)
        print("Чтобы реально отправить — добавь флаг --send (это и поставишь в таймер).")
        return 0

    # SEND
    if not channel:
        print("ERROR: канал не задан (--slack-channel или AUTO_SEND_TO_SLACK_CHANNEL)",
              file=sys.stderr)
        return 4
    if not token:
        print(f"ERROR: settings.{token_key} пуст", file=sys.stderr)
        return 4
    print(f"\nОтправляю в Slack channel={channel} (token_key={token_key})...")
    res = _post_to_slack(parent_text, rest_text, channel=channel, token=token)
    print(f"  результат: {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
