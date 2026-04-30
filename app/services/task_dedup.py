"""LLM-driven duplicate detection for newly proposed tasks.

When a Telegram message is classified into one or more `TaskDraft`s,
we don't want to spam the user's DM with widgets for tasks that
already exist in the local DB — the same instruction often repeats
across days («не забудьте про отчёт», «отправь договор»). This
helper checks every fresh candidate against the last 20 tasks and
returns a verdict: is it a duplicate of an existing one?

The check uses the same LLM backend the classifier already uses —
one extra tool-call per candidate, which is cheap relative to the
full classify pipeline.

The dedup window is intentionally small (20 tasks). Larger windows
inflate the prompt and cost without much accuracy gain, and we'd
rather miss a duplicate of a 3-month-old task than over-suppress
legitimately new work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import ActionDraft, ActionDraftState, Task, TaskStatus

log = get_logger(__name__)


_OPEN = (TaskStatus.backlog, TaskStatus.todo, TaskStatus.in_progress)


@dataclass
class _ExistingItem:
    """Unified shape for «things the candidate might duplicate» —
    open Tasks (existing in the DB) AND open ActionDrafts (created
    earlier in the same prepare_drafts batch)."""

    item_id: int
    kind: str  # "task" or "draft"
    title: str
    description: str | None
    owner_label: str
    due_date: str


@dataclass
class DedupResult:
    """Outcome of a single dedup check."""

    is_duplicate: bool
    duplicate_of_task_id: int | None = None
    reason: str | None = None


_DEDUP_TOOL_NAME = "record_dedup_decision"
_DEDUP_TOOL_DESCRIPTION = (
    "Record whether a new task duplicates an existing one."
)
_DEDUP_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_duplicate": {
            "type": "boolean",
            "description": (
                "True when the new task describes the same work as "
                "one of the existing tasks listed."
            ),
        },
        "duplicate_of_task_id": {
            "type": ["integer", "null"],
            "description": "Task id of the duplicate. Null when is_duplicate is false.",
        },
        "reason": {
            "type": "string",
            "description": "One short sentence explaining the verdict.",
        },
    },
    "required": ["is_duplicate"],
}


_SYSTEM_PROMPT = """\
You decide whether a new task DUPLICATES an existing one.

Two tasks duplicate ONLY when they describe the SAME piece of
work — same deliverable AND same target AND same goal. Just
sharing a generic verb («подготовить отчёт») is NOT enough —
the SUBJECT, RECIPIENT, and DEADLINE all matter.

DEFAULT TO `is_duplicate=false`. Better to have one extra task
the operator manually merges than to silently drop a real one.
Operator regression: «подготовить отчёт Ирине послезавтра»
was killed as duplicate of «подготовить отчёт Артёму завтра» —
WRONG. Different recipient + different deadline = different
work.

NOT duplicates (operator's «отчёт Ирине ≠ отчёт Артёму» rule):
- DIFFERENT RECIPIENT / AUDIENCE — «отчёт Ирине» vs «отчёт
  Артёму» are TWO different tasks; even if the verb and noun
  overlap, the recipient is the discriminator.
- DIFFERENT DEADLINE — «отчёт к завтра» vs «отчёт к пятнице»
  may be two milestones of the same work, but treat them
  as separate. The operator can merge if needed.
- DIFFERENT DELIVERABLE — deck vs report on the same
  project, contract vs NDA on the same client.
- DIFFERENT SPECIFIC SUBJECT — «отчёт по продажам» vs
  «отчёт по клиентам».

Duplicates (rare):
- SAME deliverable, SAME recipient, SAME deadline — just
  different wording. «подготовить отчёт по продажам Ирине
  завтра» ≈ «сделать sales-отчёт для Ирины к завтра».
- One has more context, the other less, but the core is
  identical (same project, same person, same date).

Return ``is_duplicate=true`` ONLY when ALL of subject /
recipient / deadline overlap AND the candidate adds no new
information. Otherwise return false. Set
``duplicate_of_task_id`` to the existing task's id only when
true.

Worked examples:
  candidate: «отчёт Ирине послезавтра»
  existing:  «отчёт Артёму завтра»
  → false (different recipient AND different deadline)

  candidate: «отчёт по продажам Q2»
  existing:  «отчёт по клиентам Q2»
  → false (different specific subject)

  candidate: «подготовить sales-deck к пятнице»
  existing:  «сделать презу по продажам к пятнице»
  → true (same deliverable, same deadline, paraphrase)

TRANSLITERATION & NAME-VARIANTS (FR-CR-05-92). Operator
regression: «Предложить слоты для созвона с James Morgon» and
«Предложить слоты Джеймсу Моргану» landed as TWO tasks. Same
person, just one mentions the name in English transliteration
and the other in Russian. Treat as DUPLICATE.

Rules:
- An English / Latin spelling of a person and a Russian /
  Cyrillic spelling of phonetically the same person ARE THE
  SAME PERSON: «James Morgon» = «Джеймс Морган»; «Olayan»
  = «Олаян»; «Ryan Gariepy» = «Райан Гариепи». Apply the same
  matching rule to companies / funds / projects.
- Diminutives / short-forms are the same person: «Артем» =
  «Артём» = «Artem»; «Ира» = «Ирина» = «Irina»; «Petya» =
  «Петя» = «Пётр».
- Title paraphrases that swap one name-variant for another
  but keep verb + recipient + deadline = duplicate.

Worked counter-example:
  candidate: «Предложить слоты для созвона с James Morgon»
  existing:  «Предложить слоты Джеймсу Моргану»
  → true (same person James Morgan / Джеймс Морган, same
    verb «предложить слоты», same recipient assistant Ирина,
    same deadline 2026-04-30)

SYNONYM-VERBS + SAME SPECIFIC SUBJECT (FR-CR-05-95). Operator
regressions:
  «Подтвердить время с ADNOC» vs «Согласовать время с ADNOC»
    → duplicate (подтвердить ≈ согласовать; same client; same
      deadline)
  «Добавить в звонок с Йоханом» vs «Познакомиться с Йоханом»
    → duplicate (operator goal: meet Йохан on the same call;
      different verbs but the END-STATE is one introduction)
  «Узнать о переносе звонка по Сингапуру» (owner=Ирина) vs
  «Узнать о переносе звонка по Сингапуру» (owner=Женя)
    → duplicate (same call, same question; the internal-team
      owner attribution doesn't matter — the WORK is one
      external ask, not two).

Rules:
- VERB SYNONYMS that share the same direct object are the
  same task. Curated families (FR-CR-05-95 / -96):
    confirm-family: подтвердить ≈ согласовать ≈ утвердить ≈
                    закрепить ≈ зафиксировать ≈ финализировать
                    ≈ окончательно решить ≈ confirm ≈ approve
                    ≈ sign off ≈ lock in ≈ finalize ≈ pin down
    ask-family:     узнать ≈ уточнить ≈ выяснить ≈ спросить ≈
                    проверить ≈ ask ≈ check ≈ find out ≈
                    verify ≈ clarify
    intro-family:   познакомиться ≈ представить ≈ соединить ≈
                    свести ≈ интро ≈ introduce ≈ connect ≈
                    set up an intro
    send-family:    отправить ≈ выслать ≈ переслать ≈ скинуть
                    ≈ send ≈ forward ≈ share
    meeting-family (FR-CR-05-96): организовать встречу ≈
                    пообщаться ≈ встретиться ≈ собраться ≈
                    созвониться ≈ запланировать звонок ≈
                    организовать 1-1 ≈ catch up ≈ have a call
                    ≈ schedule a meeting ≈ set up a 1:1.
                    Different framings of «set up a sync»
                    collapse to one task when participants
                    overlap.
  Additional rule: «обсудить X» / «discuss X» on the same
  topic as a meeting-family task = the SAME meeting (you
  have to have it before you can discuss in it).
- SAME SPECIFIC SUBJECT is the discriminator. «отчёт Ирине»
  vs «отчёт Артёму» = two different reports with two
  different audiences — DIFFERENT (FR-CR-05-78 still holds).
  But «звонок по Сингапуру», «встреча с ADNOC», «звонок с
  Йоханом» — these are EXTERNAL events with one fixed
  audience; whoever inside the team handles them, the work
  is one. When the SUBJECT names a specific external entity
  / event, the internal owner is NOT a discriminator —
  treat as duplicate when verb-synonyms align.

Worked counter-example:
  candidate: «Подтвердить время с ADNOC» (owner=Genia)
  existing:  «Согласовать время с ADNOC» (owner=Genia)
  → true (синонимные глаголы; ADNOC = same external event)

Worked counter-examples (FR-CR-05-96):
  candidate: «Закрепить детали партнёрства с Bosch»
  existing:  «Подтвердить детали партнёрства с Bosch»
    → true (закрепить ∈ confirm-family; same noun phrase
      «детали партнёрства с Bosch»; same external partner)

  candidate: «Закрепить детали партнёрства с Bosch» (Игорь)
  existing:  «Закрепить детали партнёрства с Bosch» (Ирина)
    → true (identical title; internal owner attribution
      doesn't discriminate when the SUBJECT names a specific
      external partner — same SAME-SPECIFIC-SUBJECT rule)

  candidate: «Пообщаться с Джарадом и Томасом»
  existing:  «Организовать 1-1 с Джарадом и Томасом»
    → true (both meeting-family; same participants Джарад +
      Томас)

  candidate: «Встретиться и обсудить партнёрство»
  existing:  «Организовать встречу» (same context: Джарад,
                 Томас, partnership discussion)
    → true (meeting-family; «обсудить партнёрство» is what
      will happen IN the meeting, not a separate task)

  candidate: «Подготовить 1-1 с Джарадом и Томасом»
  existing:  «Пообщаться с Джарадом и Томасом»
    → true (meeting-family; same participants; «подготовить»
      here means «set it up», not «prep materials FOR the
      already-scheduled meeting»)

When in doubt about meeting-family overlap: ask «is this
about THE SAME meeting / call / sync as the existing one?»
If yes → duplicate. The operator can split into a separate
prep task by hand if needed; better to err on collapsing
than to spam 5 widgets for one meeting.
"""


def _fetch_recent(session: Session, limit: int = 20) -> list[_ExistingItem]:
    """Return the most recently created «open work items» — both
    saved Tasks and pending ActionDrafts — newest first.

    Including drafts catches the case where two adjacent source
    messages produce siblings of the same task within one
    prepare_drafts batch («добавить Юру в участников» × 2). Without
    this, the dedup gate only sees the DB Task table and the
    first draft of the batch isn't there yet — both sibling
    drafts go through, and the operator gets two widgets for the
    same work.
    """
    out: list[_ExistingItem] = []
    tasks = (
        session.query(Task)
        .filter(
            Task.deleted_at.is_(None),
            Task.status.in_(_OPEN),
        )
        .order_by(Task.id.desc())
        .limit(limit)
        .all()
    )
    for t in tasks:
        out.append(
            _ExistingItem(
                item_id=t.id,
                kind="task",
                title=t.title or "",
                description=t.description,
                owner_label=t.owner_display_name or t.owner_user_id or "?",
                due_date=t.due_date.isoformat() if t.due_date else "—",
            )
        )
    drafts = (
        session.query(ActionDraft)
        .filter(ActionDraft.state == ActionDraftState.proposed)
        .order_by(ActionDraft.id.desc())
        .limit(limit)
        .all()
    )
    for d in drafts:
        payload = d.payload or {}
        out.append(
            _ExistingItem(
                item_id=d.id,
                kind="draft",
                title=str(payload.get("title") or ""),
                description=payload.get("description"),
                owner_label=str(
                    payload.get("owner_display_name")
                    or payload.get("owner_user_id")
                    or "?"
                ),
                due_date=str(payload.get("due_date") or "—"),
            )
        )
    return out


def _fmt_existing(items: list[_ExistingItem]) -> str:
    """Compact one-line-per-item listing fed to the LLM. Mark
    drafts with `D#` and tasks with `T#` so the model can address
    them separately when reporting which one is the duplicate."""
    if not items:
        return "(none)"
    lines: list[str] = []
    for it in items:
        prefix = "T#" if it.kind == "task" else "D#"
        desc = (it.description or "").replace("\n", " ").strip()
        if len(desc) > 200:
            desc = desc[:197] + "..."
        bits = [
            f"{prefix}{it.item_id}",
            it.title,
            f"owner={it.owner_label}",
            f"due={it.due_date}",
        ]
        if desc:
            bits.append(f"desc={desc}")
        lines.append("- " + " | ".join(bits))
    return "\n".join(lines)


def _fmt_candidate(payload: dict[str, Any]) -> str:
    title = payload.get("title") or ""
    desc = payload.get("description") or ""
    owner = (
        payload.get("owner_display_name")
        or payload.get("owner_user_id")
        or "?"
    )
    due = payload.get("due_date") or "—"
    parts = [f"title: {title}"]
    if desc:
        parts.append(f"description: {desc[:500]}")
    parts.append(f"owner: {owner}")
    parts.append(f"due: {due}")
    return "\n".join(parts)


def check_duplicate(
    session: Session,
    *,
    candidate: dict[str, Any],
    llm_backend: Any | None,
    lookback: int = 10,
) -> DedupResult:
    """Return a :class:`DedupResult` for ``candidate``.

    ``candidate`` is a dict with at least `title`; `description`,
    `owner_*`, and `due_date` are optional. When no LLM backend is
    available or the lookback window is empty, returns
    «not a duplicate» — better to create than to silently drop.
    """
    if not candidate.get("title"):
        return DedupResult(is_duplicate=False)

    existing = _fetch_recent(session, limit=lookback)
    if not existing:
        return DedupResult(is_duplicate=False)

    if llm_backend is None or not hasattr(llm_backend, "call_tool"):
        return DedupResult(is_duplicate=False)

    user_prompt = (
        "EXISTING TASKS (most recent open):\n"
        f"{_fmt_existing(existing)}\n\n"
        "NEW CANDIDATE:\n"
        f"{_fmt_candidate(candidate)}"
    )
    try:
        result = llm_backend.call_tool(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            tool_name=_DEDUP_TOOL_NAME,
            tool_description=_DEDUP_TOOL_DESCRIPTION,
            tool_parameters=_DEDUP_TOOL_PARAMETERS,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("task_dedup_llm_failed", error=str(e))
        return DedupResult(is_duplicate=False)

    if not isinstance(result, dict):
        return DedupResult(is_duplicate=False)

    is_dup = bool(result.get("is_duplicate"))
    of_id = result.get("duplicate_of_task_id")
    if of_id is not None:
        try:
            of_id = int(of_id)
        except (TypeError, ValueError):
            of_id = None
        # Don't trust an id that isn't actually in our lookback set —
        # the model occasionally invents. Match against both Tasks
        # and Drafts in the existing set.
        if of_id not in {it.item_id for it in existing}:
            of_id = None
    return DedupResult(
        is_duplicate=is_dup,
        duplicate_of_task_id=of_id,
        reason=str(result.get("reason") or "")[:200] or None,
    )
