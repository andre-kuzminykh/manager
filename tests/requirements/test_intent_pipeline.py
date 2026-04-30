"""Requirement coverage: FR-CR-04-1 (Detection stage),
FR-CR-04-2 (Parallel extraction), FR-CR-04-5 (date stripping from
title/description), NFR-CR-04-1 (per-stage resilience).

Pipeline architecture tests:
  Stage 1 — a single LLM call decides: is this a task? (yes / no).
  Stage 2 — if yes, three concerns are extracted IN PARALLEL:
            2a. description / title / priority  (LLM)
            2b. owner                           (LLM, conversation-aware)
            2c. due date                        (Python resolver)
  Stage 3 — assemble and return IntentClassification.
  UX: mention → auto-confirm; passive → needs confirmation.
"""
from __future__ import annotations

import threading
import time
from datetime import date

from app.intent.detect_prompt import DETECT_SYSTEM_PROMPT, DETECT_TOOL_NAME
from app.intent.owner_prompt import OWNER_SYSTEM_PROMPT, OWNER_TOOL_NAME
from app.intent.pipeline import run_pipeline
from app.intent.title_prompt import TITLE_SYSTEM_PROMPT, TITLE_TOOL_NAME
from app.schemas.intent import IntentType


# -------- Stage prompt sanity --------------------------------------------- #


def test_detect_prompt_asks_single_yes_no_question():
    # The detection schema covers the binary verdict + the
    # FR-CR-05-05 multi-task-split signal (task_count + task_chunks).
    from app.intent.detect_prompt import DETECT_TOOL_PARAMETERS

    props = DETECT_TOOL_PARAMETERS["properties"]
    assert set(props.keys()) == {
        "is_task",
        "confidence",
        "reasoning",
        "task_count",
        "task_chunks",
    }
    assert DETECT_TOOL_PARAMETERS["required"] == ["is_task", "confidence"]
    assert "is_task" in DETECT_SYSTEM_PROMPT.lower() or "is the author" in DETECT_SYSTEM_PROMPT.lower()


def test_title_prompt_covers_title_description_priority_only():
    from app.intent.title_prompt import TITLE_TOOL_PARAMETERS

    props = TITLE_TOOL_PARAMETERS["properties"]
    assert set(props.keys()) == {"title", "description", "priority"}
    # The tool schema must NOT expose assignee or due_date fields.
    assert "due_date" not in props
    assert "owner" not in props
    assert "assignee" not in props


def test_detect_prompt_lists_status_reports_and_parroted_phrases_as_no_action():
    """FR-CR-05-09 — the detect prompt is taught to reject:

    - status-list reports («DBS — нет, Jefferies — отправила, …»)
    - parroted one-line acknowledgements without context
      («хорошо! напишу ему», «ок, сделаю»)
    - OCR / transcription noise («файндхэзом» as the entire message)

    These show up enough in real chat traffic that we want them
    pinned in the prompt, not just folded into the generic «chat»
    bucket.
    """
    blob = DETECT_SYSTEM_PROMPT
    assert "Status-list reports" in blob or "status-list" in blob.lower()
    assert "Parroted" in blob or "parroted" in blob.lower()
    # At least one example each side of the dash for status lists.
    assert "DBS" in blob or "Jefferies" in blob
    # Parroted-phrase example must be quoted so the model anchors
    # on the exact pattern.
    assert "напишу ему" in blob
    # OCR-noise rejection must be explicit — we hit «файндхэзом»
    # in real traffic and want it killed at the detect stage.
    assert "OCR" in blob or "transcription" in blob.lower()


def test_detect_prompt_rejects_passive_past_tense_status_reports():
    """FR-CR-05-12 — «письма в Abundance отправлены» (passive past
    tense) must be no_action, same as active past («отправил»).
    The prompt now lists passive forms explicitly so the LLM
    doesn't read them as imperatives."""
    blob = DETECT_SYSTEM_PROMPT
    # Passive forms that previously slipped through.
    for word in ("отправлены", "подписан", "оплачен", "утверждён", "sent", "approved"):
        assert word in blob, (
            f"detect prompt should mention passive-past form {word!r}"
        )
    # Concrete example anchoring the rule.
    assert "письма в Abundance отправлены" in blob


def test_title_prompt_teaches_imperative_rewrite_from_context():
    """FR-CR-05-09 — the title prompt is taught to use the
    `context` block to rewrite parroted one-liners into a proper
    imperative title. «хорошо, напишу ему» with a prior message
    «надо ответить Андрею» must NOT land as the literal phrase —
    the prompt explicitly forbids it and shows a rewrite example.
    """
    blob = TITLE_SYSTEM_PROMPT
    assert "PARROTED" in blob or "parroted" in blob.lower()
    # Forbid the verbatim copy and show the proper rewrite shape.
    assert "напишу ему" in blob
    assert "написать" in blob
    # The instruction MUST mention the context block, since the
    # rewrite depends on it.
    assert "context" in blob.lower()


def test_title_prompt_forbids_vague_placeholder_phrases_in_description():
    """FR-CR-05-22 — descriptions must use concrete names /
    numbers / projects from the context, never placeholder
    pronouns like «указанных» / «правильной» / «нужных» when the
    context tells the model who or what is meant."""
    blob = TITLE_SYSTEM_PROMPT
    assert "CONCRETE OVER VAGUE" in blob or "concrete over vague" in blob.lower()
    # Pin the specific anti-patterns we hit in production. Note
    # the prompt may wrap long phrases across lines, so we
    # collapse whitespace before checking.
    flat = " ".join(blob.split())
    for word in ("указанных людей", "правильной командой", "the right people"):
        assert word in flat, f"placeholder example {word!r} should be pinned"
    # And the «when context doesn't name them, write (уточнить)» rule.
    assert "уточнить" in blob


def test_title_prompt_forbids_first_person_plural_in_description():
    """FR-CR-05-22 — «нам надо» / «будем рады» / «we'd love to»
    are first-person-plural source artefacts. The description is
    a brief about a task assigned to ONE specific owner — third
    person only."""
    blob = TITLE_SYSTEM_PROMPT
    assert "THIRD PERSON" in blob or "third person" in blob.lower()
    for fragment in ("нам", "будем рады", "we'd love"):
        assert fragment in blob, f"first-person-plural example {fragment!r} should be pinned"


def test_title_prompt_forbids_third_party_status_promises():
    """FR-CR-05-13 — «Нет Алина сама отправит» (a third-party
    promise sentence about another teammate's commitment) must NOT
    land verbatim as the title. The title prompt teaches an
    explicit rewrite path."""
    blob = TITLE_SYSTEM_PROMPT
    assert "THIRD-PARTY" in blob or "third-party" in blob.lower()
    assert "Нет Алина сама отправит" in blob
    # The example must show a clean imperative as the rewrite.
    assert "отправить" in blob


def test_title_prompt_forbids_trailing_clauses_in_descriptions():
    """FR-CR-05-12 — the title prompt now caps the description at
    1-3 short sentences and explicitly forbids trailing-clause
    truncations like «так как осталось открытым с» (mid-sentence
    cut)."""
    blob = TITLE_SYSTEM_PROMPT
    # Length / completeness rule must be present.
    assert "LENGTH RULE" in blob or "length rule" in blob.lower()
    assert "complete sentence" in blob.lower() or "finish every sentence" in blob.lower()
    assert "trail off" in blob.lower() or "trailing" in blob.lower()


def test_owner_prompt_renders_role_and_notes_columns():
    """FR-CR-05-12 — the owner prompt receives `role` and `notes`
    per known_employee row to disambiguate same-first-name
    teammates («Алина — founder» vs «Алина — project manager»).
    The user-side prompt must surface those columns."""
    from app.intent.owner_prompt import build_owner_user_prompt

    prompt = build_owner_user_prompt(
        source_text="подать заявку на StartUp Qatar",
        context_messages=[],
        author_user_id="U1",
        known_employees=[
            {
                "slack_user_id": "111",
                "display_name": "Alina",
                "real_name": "Alina Founder",
                "role": "founder",
                "notes": "deals with international expansion",
            },
            {
                "slack_user_id": "222",
                "display_name": "Alina",
                "real_name": "Alina Manager",
                "role": "project manager / аналитик",
                "notes": "",
            },
        ],
    )
    # Both role strings must appear so the LLM can tell them apart.
    assert "founder" in prompt
    assert "project manager" in prompt.lower()
    # Header advertises the new columns.
    assert "role" in prompt
    assert "notes" in prompt


def test_owner_prompt_disambiguation_section_lists_role_first():
    """The system prompt must teach the LLM to USE role / notes
    when several rows share a first name. If we don't pin this
    in the prompt, the model picks alphabetically and we get the
    wrong Alina."""
    blob = OWNER_SYSTEM_PROMPT
    assert "DISAMBIGUATION" in blob or "disambiguat" in blob.lower()
    assert "role" in blob.lower()
    assert "notes" in blob.lower()


def test_owner_prompt_uses_role_notes_for_unnamed_assignments():
    """FR-CR-05-31 — when the source describes work without
    naming a person («нужно ответить инвестору Olayan по
    cap-table»), the LLM MUST pick the teammate whose role /
    notes match the responsibility area. Pinned in the system
    prompt so the model doesn't fall back to «no one named ⇒
    null»."""
    blob = OWNER_SYSTEM_PROMPT
    flat = " ".join(blob.split())
    # The prompt now has a SOURCE OF TRUTH block + concrete
    # examples that show role/notes-driven owner picks.
    assert "SOURCE OF TRUTH" in flat or "source of truth" in flat.lower()
    # At least one example wording the model can anchor on.
    assert "investor relations" in flat or "investor" in flat.lower()


def test_owner_user_prompt_keeps_long_notes_intact():
    """FR-CR-05-31 — operator-written notes can be ~150 chars
    («ответственная за инвестор-релейшнс, готовит cap-table и
    ходит на встречи с инвесторами»). The render previously
    truncated to 60 chars, which clipped the very signal the
    LLM needs. New cap is 200; long notes survive."""
    from app.intent.owner_prompt import build_owner_user_prompt

    long_notes = (
        "ответственная за инвестор-релейшнс, готовит cap-table "
        "и ходит на встречи с инвесторами; в команде с 2024"
    )
    assert len(long_notes) > 60
    prompt = build_owner_user_prompt(
        source_text="нужно ответить инвестору",
        context_messages=[],
        author_user_id=None,
        known_employees=[
            {
                "slack_user_id": "111",
                "display_name": "Маша",
                "real_name": "Маша IR",
                "role": "investor relations",
                "notes": long_notes,
            }
        ],
    )
    # Full notes survive the render (truncation cap is now 200).
    assert "cap-table" in prompt
    assert "встречи с инвесторами" in prompt


def test_owner_prompt_sees_conversation_context_placeholder():
    from app.intent.owner_prompt import build_owner_user_prompt

    prompt = build_owner_user_prompt(
        source_text="сделай это",
        context_messages=[{"ts": "0.5", "user": "U-a", "text": "hello"}],
        author_user_id="U-a",
    )
    assert "context" in prompt.lower()
    assert "source_author: U-a" in prompt


# -------- Recording backend ------------------------------------------------ #


class _RecordingBackend:
    def __init__(self, *, detect, title, owner, delay_title: float = 0):
        self._payloads = {
            DETECT_TOOL_NAME: detect,
            TITLE_TOOL_NAME: title,
            OWNER_TOOL_NAME: owner,
        }
        self._delay_title = delay_title
        self.call_log: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def extract_intent(self, *, user_prompt):  # pragma: no cover
        raise NotImplementedError

    def call_tool(self, **kw):
        tool = kw["tool_name"]
        start = time.perf_counter()
        if tool == TITLE_TOOL_NAME and self._delay_title:
            time.sleep(self._delay_title)
        end = time.perf_counter()
        with self._lock:
            self.call_log.append((tool, start))
            self.call_log.append((tool + ":end", end))
        return self._payloads.get(tool)


# -------- Stage 1 short-circuit ------------------------------------------- #


def test_detection_false_short_circuits_the_pipeline():
    backend = _RecordingBackend(
        detect={"is_task": False, "confidence": 0.05, "reasoning": "chat"},
        title={"title": "unused"},
        owner={"reasoning": "unused", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="how's it going?",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.intent == IntentType.no_action
    # Stage 2 never ran.
    tools_called = {
        entry[0] for entry in backend.call_log if not entry[0].endswith(":end")
    }
    assert tools_called == {DETECT_TOOL_NAME}


# -------- Stage 2 parallelism --------------------------------------------- #


def test_stage2_title_and_owner_run_in_parallel():
    """Title stage sleeps 80ms. Owner stage is instant. If we ran them
    sequentially, total Stage-2 time would be ~80ms; running in parallel
    caps it near 80ms regardless of the owner stage. We verify by
    checking that the two start timestamps are nearly simultaneous —
    title should start BEFORE owner finished, i.e. they overlap."""
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "собрать демо"},
        owner={"reasoning": "no one", "display_name": None},
        delay_title=0.08,
    )
    run_pipeline(
        backend=backend,
        source_text="надо собрать демо",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    starts = {
        entry[0]: entry[1]
        for entry in backend.call_log
        if not entry[0].endswith(":end")
    }
    ends = {
        entry[0][:-4]: entry[1]
        for entry in backend.call_log
        if entry[0].endswith(":end")
    }
    # Owner started before title finished → they overlap in time.
    assert starts[OWNER_TOOL_NAME] < ends[TITLE_TOOL_NAME], (
        "Stage 2 must run title and owner concurrently, not sequentially"
    )


# -------- Stage 3 assembly ------------------------------------------------- #


def test_pipeline_assembles_all_four_fields():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.88, "reasoning": "yes"},
        title={
            "title": "подготовить питчдек",
            "description": "для инвесторов",
            "priority": "high",
        },
        owner={
            "slack_user_id": "U-ivan",
            "display_name": "Иван",
            "reasoning": "addressed to <@U-ivan>",
        },
    )
    out = run_pipeline(
        backend=backend,
        source_text="Иван, подготовь питчдек к понедельнику",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.intent == IntentType.create_task
    assert out.confidence == 0.88
    assert out.task.title == "подготовить питчдек"
    assert out.task.description == "для инвесторов"
    assert out.task.priority == "high"
    assert out.task.owner_user_id == "U-ivan"
    assert out.task.owner_display_name == "Иван"
    # Stage 2c: Python date resolver picked up "понедельнику".
    assert out.task.due_date == date(2026, 4, 27)


def test_pipeline_strips_date_from_title_and_description():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.88},
        title={
            "title": "подготовить заметки к 1 мая",
            "description": "к 1 мая",
            "priority": "high",
        },
        owner={"reasoning": "no assignee", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо подготовить заметки к 1 мая",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.task.title == "подготовить заметки"
    # Description was just "к 1 мая" → becomes None after stripping.
    assert out.task.description is None
    # Date still ends up in due_date.
    assert out.task.due_date == date(2026, 5, 1)


def test_pipeline_leaves_owner_null_when_owner_stage_says_no_one():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай отчёт"},
        owner={"reasoning": "no assignee wording", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать отчёт",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.task is not None
    assert out.task.owner_user_id is None
    assert out.task.owner_display_name is None


def test_pipeline_uses_display_name_when_no_slack_id():
    backend = _RecordingBackend(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "сделай"},
        owner={"display_name": "Паша", "reasoning": "assigned to Паша"},
    )
    out = run_pipeline(
        backend=backend,
        source_text="Паша, сделай",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.task.owner_display_name == "Паша"
    assert out.task.owner_user_id is None


# -------- Resilience ------------------------------------------------------- #


def test_title_stage_failure_falls_back_to_source_text():
    class _BrokenTitle(_RecordingBackend):
        def call_tool(self, **kw):
            if kw["tool_name"] == TITLE_TOOL_NAME:
                raise RuntimeError("openai down")
            return super().call_tool(**kw)

    backend = _BrokenTitle(
        detect={"is_task": True, "confidence": 0.9},
        title={"title": "unused"},
        owner={"reasoning": "no one", "display_name": None},
    )
    out = run_pipeline(
        backend=backend,
        source_text="надо сделать X",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    assert out.intent == IntentType.create_task
    assert out.task.title  # falls back to source_text[:120] or similar


def test_detect_stage_failure_returns_no_action():
    class _BrokenDetect:
        def extract_intent(self, *, user_prompt):
            raise NotImplementedError

        def call_tool(self, **kw):
            if kw["tool_name"] == DETECT_TOOL_NAME:
                raise RuntimeError("openai down")
            return None

    out = run_pipeline(
        backend=_BrokenDetect(),
        source_text="надо сделать",
        context_messages=[],
        author_user_id="U-author",
        today=date(2026, 4, 24),
    )
    # The pipeline swallows the failure and emits no_action. The
    # classify_with_backend safety net (separate concern) handles the
    # rules-based override above this layer.
    assert out.intent == IntentType.no_action


def test_intent_prompt_teaches_multi_task_split():
    """FR-CR-05-46 — the SYSTEM_PROMPT carries explicit guidance to
    split conjunctions / enumerations / two-verb sentences into
    separate tasks. Pinned because the regression we hit was a
    voice-dictated «надо разработать бота а ещё дашборд» being
    captured as one task with both verbs concatenated."""
    from app.intent.prompts import SYSTEM_PROMPT

    blob = SYSTEM_PROMPT
    # Splitting signals taught.
    assert "а ещё" in blob or "а еще" in blob.lower()
    assert "tasks" in blob.lower()
    # The exact failure mode worked-example is pinned.
    assert "разработать бота" in blob and "сделать дашборд" in blob
    # Single-task instruction also documented (use tasks with one
    # item, don't fall back to legacy `task`).
    assert "single-item" in blob.lower() or "one item" in blob.lower()


def test_intent_tool_schema_has_tasks_array():
    """FR-CR-05-46 — `INTENT_TOOL_PARAMETERS` exposes a `tasks`
    array (in addition to legacy `task`) so the LLM can return
    multi-task extraction directly. Without this field the
    multi-task path was unreachable from the LLM side even
    though `IntentClassification.tasks` was wired in code."""
    from app.intent.llm_backends import INTENT_TOOL_PARAMETERS

    props = INTENT_TOOL_PARAMETERS["properties"]
    assert "tasks" in props
    assert props["tasks"]["type"] == "array"
    assert props["tasks"]["items"]["type"] == "object"
    # Each task in the array has at least a title + the four
    # standard optional fields.
    item_props = props["tasks"]["items"]["properties"]
    for f in ("title", "description", "owner_display_name", "priority", "due_date"):
        assert f in item_props
    assert props["tasks"]["items"]["required"] == ["title"]


def test_owner_prompt_routes_routine_work_to_assistant_named_in_notes():
    """FR-CR-05-52 — when notes on a principal's row say
    «только стратегические задачи; ассистент — Ирина», the
    owner prompt teaches the LLM to delegate routine work to
    the assistant rather than the principal. Pinned because
    operator wrote «у Артёма есть Ассистент Ирина» in the
    Team sheet and expects the bot to actually use that hint
    when an operational task is delegated 'to Артём'."""
    blob = OWNER_SYSTEM_PROMPT
    assert "ASSISTANT" in blob.upper() or "ассистент" in blob.lower()
    assert "только стратегические" in blob.lower() or "strategic" in blob.lower()
    # Worked example pinned: Артём + Ирина (CEO + assistant) so
    # this exact failure mode the operator hit can never silently
    # regress without a test failure.
    assert "Артём" in blob and "Ирина" in blob
    # Decision/strategic cases stay with the principal — pinned
    # so we don't end up routing EVERYTHING to the assistant.
    assert "strategic" in blob.lower() or "стратеги" in blob.lower()


def test_intent_prompt_demands_rich_3_to_6_sentence_descriptions():
    """FR-CR-05-62 — description rule rewritten to demand 3-6
    sentences with explicit context-message pulling. Pinned
    after the operator showed «🟡 отправить инвайт Олаяна / 📝
    Необходимо отправить инвайт Олаяна на встречу в 14:00» —
    the LLM was producing 80-char paraphrases that re-stated
    the title instead of summarising the surrounding chat."""
    from app.intent.prompts import SYSTEM_PROMPT

    blob = SYSTEM_PROMPT
    # Length target raised.
    assert "3-6 sentences" in blob or "3-6 предложений" in blob.lower()
    # Context-message pulling is explicit.
    assert "context_messages" in blob.lower()
    # The exact regression case is pinned as a failure example.
    assert "Олаяна" in blob and "Юля" in blob
    # «BAD desc» / «GOOD desc» worked example.
    assert "BAD desc" in blob
    assert "GOOD desc" in blob


def test_date_prompt_pins_no_list_item_dates():
    """FR-CR-05-71 — operator: a task source starting «5.
    Мистраль - Arthur Mehcsh - отправь письмо» got
    `due_date=2026-05-05` because the LLM read «5.» as the 5th
    day of next month. The DATE_SYSTEM_PROMPT now explicitly
    forbids treating numbered-list bullets as dates."""
    from app.intent.date_prompt import DATE_SYSTEM_PROMPT

    blob = DATE_SYSTEM_PROMPT
    assert "list-item" in blob.lower() or "enumeration" in blob.lower()
    # Worked counter-example pinned.
    assert "Мистраль" in blob or "Arthur Mehcsh" in blob
    # Anchor requirement.
    assert "temporal anchor" in blob.lower() or "к 5" in blob


def test_intent_prompt_pins_no_list_item_dates():
    """FR-CR-05-71 — same anti-rule mirrored on the main intent
    prompt's date-resolution section so the multi-task batch
    extractor doesn't fall into the same trap as the
    standalone date stage."""
    from app.intent.prompts import SYSTEM_PROMPT

    blob = SYSTEM_PROMPT
    assert "list-item" in blob.lower() or "enumeration" in blob.lower()
    assert "structural" in blob.lower()


def test_owner_prompt_pins_dative_audience_rule():
    """FR-CR-05-77 — operator: «подготовить отчёт артему завтра»
    landed on Артём instead of self. Russian dative case
    («отчёт Артёму») is AUDIENCE, not assignment. Prompt now
    spells this out with the exact regression case as worked
    counter-example."""
    from app.intent.owner_prompt import OWNER_SYSTEM_PROMPT

    blob = OWNER_SYSTEM_PROMPT
    assert "DATIVE" in blob
    assert "отчёт Артёму" in blob or "отчет Артёму" in blob
    assert "AUDIENCE" in blob
    # Vocative + verb is the canonical assignment signal.
    assert "сделай" in blob
