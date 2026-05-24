"""Render proposed action_drafts as a unified, direction-grouped digest via LLM.

READ-ONLY. Queries `action_drafts` (+ `intent_inferences` for source),
classifies each task by strategic DIRECTION (investors / budget / design /
beta / deliverables — the filter categories), optionally keeps only those,
then asks the LLM — per task — for an entity `group` (Tether / XTX / …) +
a concise `action`. Python renders the digest as DIRECTION → ENTITY group →
numbered tasks, appending the responsible person (resolved to a real name
from team_members, role-suffix stripped; never invented).

The LLM only classifies + rephrases per task (JSON-per-id), so no task can
be silently dropped: missing ids are re-requested once, and the render is
fully deterministic.

By default the script ONLY previews (renders the exact Slack-mrkdwn message
and prints it) — it does NOT post to Slack. Pass --send to actually post;
that is the flag a cron/timer would use for the morning digest.

Usage:
    # ПРЕВЬЮ (ничего не отправляется) — проверить, что выведется:
    docker exec manager-zoom-ff-1 python -m ops.strategic_tasks_digest \\
        --since 2025-05-22 --strategic-only

    # РЕАЛЬНАЯ отправка в Slack-бот (для таймера/cron):
    docker exec manager-zoom-ff-1 python -m ops.strategic_tasks_digest \\
        --since 2025-05-22 --strategic-only --send

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

FORMAT_SYSTEM_PROMPT = (
    "Ты — ассистент CEO, собираешь утренний дайджест «Задачи на сегодня» "
    "(в основном fundraising / инвесторы). На КАЖДУЮ задачу верни:\n"
    "1. group — каноническое короткое имя контрагента/фонда/человека/темы, "
    "о ком задача (1-3 слова: «Tether», «XTX», «20VC», «Robostrategy»). "
    "Для задач про ОДНОГО И ТОГО ЖЕ контрагента используй ОДИНАКОВЫЙ group "
    "(точно та же строка), чтобы они сгруппировались вместе. Если "
    "контрагента нет — короткая тема.\n"
    "2. action — лаконично и ПОНЯТНО, что именно сделать (императив). Если "
    "в данных есть статус/история (даты, что уже делали, ответ контакта) — "
    "кратко добавь, напр. «05/05 ответили — 12/05 напомнили — напомнить "
    "последний раз».\n\n"
    "ПРАВИЛА:\n"
    "- НЕ включай в action имя ответственного — его подставят отдельно.\n"
    "- НЕ выдумывай даты и факты. Только то, что есть в данных задачи.\n"
    "- Каждой задаче — РОВНО один объект. Ничего не выбрасывай и не "
    "объединяй разные задачи.\n\n"
    "Верни СТРОГО JSON: "
    '{"items":[{"id":<int>,"group":"<контрагент/тема>","action":"<что сделать>"}]}'
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
            }
        )
    return out


def _assign_directions(
    tasks: list[dict], *, llm, model, strategic_only: bool
) -> list[dict]:
    """Classify every task by strategic direction (chunked, so the LLM can't
    silently drop ids on large batches). Sets t["direction"]. When
    `strategic_only` — keep only DIRECTIONS_IMPORTANT; otherwise keep all
    (unclassified → «other»).
    """
    mapping: dict[int, str] = {}
    chunk = 50
    for i in range(0, len(tasks), chunk):
        batch = tasks[i : i + chunk]
        part = classify_directions(
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
            model=model,
        )
        mapping.update(part)
    for t in tasks:
        t["direction"] = mapping.get(t["id"], "other")
    if strategic_only:
        kept = [t for t in tasks if t["direction"] in DIRECTIONS_IMPORTANT]
        print(
            f"  strategic filter: {len(kept)}/{len(tasks)} прошли "
            f"(направления: {DIRECTIONS_IMPORTANT})"
        )
        return kept
    return tasks


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
        out[tid] = {
            "group": (it.get("group") or "").strip(),
            "action": (it.get("action") or "").strip(),
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
                "group": (t["title"][:40] or "Задача"),
                "action": "уточнить",
            }
    return result


def _normalize_owner(owner: str, name_by_username: dict[str, str]) -> str:
    """Clean the responsible name: strip role suffix, resolve @handle → real
    name via team_members. Returns '' if empty."""
    o = (owner or "").strip()
    if not o:
        return ""
    for sep in (" - ", " — ", " /", " ("):
        if sep in o:
            o = o.split(sep, 1)[0].strip()
    if o.startswith("@"):
        uname = o[1:].strip().lower()
        return name_by_username.get(uname, o[1:])
    return o


def _render(
    tasks: list[dict], formatted: dict[int, dict], *, flavor: str = "md"
) -> str:
    """Render the digest: DIRECTION (filter category) → ENTITY sub-group
    (Tether / XTX / …) → numbered tasks with the responsible in parens.

    flavor="md"    → markdown for terminal.
    flavor="slack" → Slack mrkdwn (`*bold*`; Slack ignores `##`/`**`).
    """
    slack = flavor == "slack"
    h1 = (lambda s: f"*{s}*") if slack else (lambda s: f"# {s}")
    h2 = (lambda s: f"*{s}*") if slack else (lambda s: f"## {s}")
    h3 = (lambda s: f"*{s}*") if slack else (lambda s: f"### {s}")
    bold = (lambda s: f"*{s}*") if slack else (lambda s: f"**{s}**")

    by_id = {t["id"]: t for t in tasks}
    dir_buckets: dict[str, list[int]] = {d: [] for d in DIRECTION_ORDER}
    for tid, t in by_id.items():
        dir_buckets.setdefault(t.get("direction") or "other", []).append(tid)

    def _grp(tid: int) -> str:
        return (formatted[tid].get("group") or "").strip() or "Разное"

    def _action(tid: int) -> str:
        return formatted[tid].get("action") or "уточнить"

    out: list[str] = [h1("Задачи на сегодня")]
    n = 0
    for d in DIRECTION_ORDER:
        ids = dir_buckets.get(d) or []
        if not ids:
            continue
        out.append("")
        out.append(h2(DIRECTION_LABELS.get(d, d)))

        # cluster within direction by entity group
        groups: dict[str, list[int]] = {}
        for tid in ids:
            groups.setdefault(_grp(tid), []).append(tid)
        multi = sorted(
            (g for g, v in groups.items() if len(v) >= 2 and g != "Разное"),
            key=lambda g: (-len(groups[g]), g.lower()),
        )
        single_ids = sorted(
            tid for g, v in groups.items() if g not in multi for tid in v
        )

        for g in multi:
            out.append("")
            out.append(h3(g))
            for tid in sorted(groups[g]):
                n += 1
                out.append(f"{n}. {_action(tid)} ({by_id[tid]['owner'] or '—'})")

        if single_ids:
            # If the direction has named clusters, file the rest under «Разное»;
            # otherwise list them directly (no redundant sub-header).
            if multi:
                out.append("")
                out.append(h3("Разное"))
            for tid in single_ids:
                n += 1
                out.append(
                    f"{n}. {bold(_grp(tid))} — {_action(tid)} "
                    f"({by_id[tid]['owner'] or '—'})"
                )
    return "\n".join(out).rstrip()


def _post_to_slack(text: str, *, channel: str, token: str) -> dict:
    """Post the digest to Slack: first chunk = parent, rest = thread replies."""
    from slack_sdk import WebClient

    from app.services.slack_mirror import SLACK_TEXT_CHUNK_CHARS, _split_for_slack

    chunks = _split_for_slack(text, limit=SLACK_TEXT_CHUNK_CHARS)
    client = WebClient(token=token)
    resp = client.chat_postMessage(
        channel=channel, text=chunks[0], unfurl_links=False, unfurl_media=False
    )
    parent_ts = (resp.data or {}).get("ts")
    for c in chunks[1:]:
        client.chat_postMessage(
            channel=channel, text=c, thread_ts=parent_ts,
            unfurl_links=False, unfurl_media=False,
        )
    return {"ok": True, "parent_ts": parent_ts, "chunks": len(chunks)}


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--since",
        default="2025-05-22",
        help="ISO date (YYYY-MM-DD); drafts created at/after этой даты (UTC).",
    )
    ap.add_argument(
        "--strategic-only",
        action="store_true",
        help="Keep only DIRECTIONS_IMPORTANT (investors/budget/design/beta/"
        "deliverables). По умолчанию — все proposed.",
    )
    ap.add_argument("--model", default=None, help="OpenAI model override.")
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

        print(f"  классифицируем направления ({len(tasks)} задач, {model})...")
        tasks = _assign_directions(
            tasks, llm=llm, model=model, strategic_only=args.strategic_only
        )
        if not tasks:
            print("  стратегических задач не найдено.")
            session.rollback()
            return 0

        # Resolve @handles / strip role suffixes → нормальные имена.
        from app.services.team_members import get_humans_for_matcher

        name_by_username = {
            h["tg_username"].lower(): h["real_name"]
            for h in get_humans_for_matcher(session)
            if h.get("tg_username") and h.get("real_name")
        }
        for t in tasks:
            t["owner"] = _normalize_owner(t["owner"], name_by_username)

        print(f"  форматируем {len(tasks)} задач через LLM...")
        formatted = _format_all(tasks, llm=llm, model=model)
        slack_text = _render(tasks, formatted, flavor="slack")

        session.rollback()  # explicit: read-only, БД нетронута

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
        print("=" * 70 + "\n")
        print(slack_text)
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
    res = _post_to_slack(slack_text, channel=channel, token=token)
    print(f"  результат: {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
