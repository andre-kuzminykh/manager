"""FR-CR-05-130 — extract real-name participants of a meeting
from the transcript by matching against the operator's
canonical Team Members directory.

Operator-pinned: «теперь надо "участники" конкретно для встреч
по зуму вычленять из списка Team — надо чтобы они подтягивались
оттуда как и с назначением задач, это отдельным промтом тоже».

The Zoom recording's `participants` metadata is unreliable —
attendee labels come from the Zoom client account name, not
from who actually spoke. We instead ask the LLM to read the
transcript and emit the SUBSET of `team_members.real_name`
values that the LLM heard speak (or got addressed by name).
"""
from __future__ import annotations

import json as _json
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


PARTICIPANTS_EXTRACT_SYSTEM = """\
You read a meeting transcript and identify which TEAM MEMBERS
spoke or were addressed in the meeting.

Input (in the user prompt):
  - The full meeting transcript (Whisper-transcribed Russian +
    English speech, EXPECT typos and phonetic errors).
  - A `team_members` table: `real_name | role | notes` for
    every known operator-side teammate.

Output via JSON: a list of `real_name` values from the
team_members table that the transcript reasonably indicates
participated in the meeting (spoke, were named, had something
addressed to them).

═══════════════════════════════════════════════════════════════
PHONETIC TOLERANCE — Whisper mishears Russian first names
constantly:
  «Алина» / «Алинна» / «Алинь»  → match teammate «Алина»
  «Дима» / «Димка» / «Димас»     → match teammate «Дима» /
                                   «Дмитрий» whoever's row says
  «Артём» / «Артем» / «Артэм»    → match the Artem teammate
  «Ира» / «Ирина» / «Ирочка»     → match Irina teammate
═══════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════
AMBIGUOUS FIRST-NAME RULE (FR-CR-05-140) — operator-pinned:
«не забудь ставить в ответственных только тех кто был на
встрече» combined with «у тебя в участниках Дроздов, а нужен
Седов» (we picked the wrong Дима).

When the transcript uses a BARE first name («Дима», «Лена»,
«Саша») WITHOUT a surname AND `team_members` has TWO OR MORE
teammates with that first name (e.g. «Дима Дроздов» AND
«Дмитрий Седов»):

  - DO NOT guess one. Include ALL of them in the participants
    list. The downstream task-routing layer reads the per-task
    topic + each teammate's `notes` column to pick the right
    person per task, with much more context than this single
    pass has.
  - The ONLY exception: when the transcript explicitly uses a
    surname («Дима Дроздов сказал …», «Седов посмотрит …») or
    other unambiguous identifier — then include only the named
    one.

Worked example:
  team_members: «Дима Дроздов» (Head of Network) + «Дмитрий
                Седов» (Финансовый Советник Артема).
  transcript:   «… Дима, отправь Tether email-апдейт …» (no
                surname mentioned anywhere in the call).
  output:       BOTH names in `participants`. Downstream
                task-routing layer uses notes to assign each
                Дима-task to the right one.
═══════════════════════════════════════════════════════════════

OUTPUT RULES:

1. Return the team_member's `real_name` EXACTLY as in the
   table.
2. Each name once (dedup).
3. Order: by first appearance in the transcript.
4. If only the host's auto-stamp / the bot is mentioned with no
   real participation evidence, return `{"participants": []}`.
5. NEVER add names that aren't in the team_members table
   (those are external counterparties, not participants).
6. Apply the AMBIGUOUS FIRST-NAME RULE above when the
   transcript leaves a first name unresolvable.

Respond as a JSON object: `{"participants": ["<real_name>", ...]}`.
"""


def extract_zoom_participants_via_llm(
    transcript: str,
    team_members: list[dict[str, str | None]],
    *,
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = None,
    trace_source: str | None = None,
    trace_recording_id: str | None = None,
) -> list[str]:
    """FR-CR-05-130 — return the operator-side team member
    real-names that participated in the meeting (spoke or
    were addressed). Returns [] on parse / LLM failure.

    `team_members` is a list of dicts with at least
    `real_name`. Optional keys: `role`, `notes` — fed to the
    LLM for context (so it can reason «Дима с инвестроутингом
    скорее всего ML investor relations человек»).
    """
    try:
        from app.services.trace_log import trace_event
    except Exception:  # noqa: BLE001
        def trace_event(**_kw: Any) -> None:  # type: ignore[misc]
            pass

    if not transcript or not team_members:
        return []
    table_lines = [
        "  real_name                    | role               | notes"
    ]
    for tm in team_members:
        rn = (tm.get("real_name") or "").strip()
        if not rn:
            continue
        role = (tm.get("role") or "")[:18]
        notes = (tm.get("notes") or "")[:80]
        table_lines.append(f"  {rn:<29} | {role:<18} | {notes}")
    table_block = "\n".join(table_lines)
    user_prompt = (
        "team_members:\n" + table_block
        + "\n\nТранскрипт встречи:\n" + transcript
    )
    _start = dict(
        model=model,
        reasoning_effort=reasoning_effort,
        team_members_count=len(team_members),
        transcript_chars=len(transcript),
        prompt_chars=len(user_prompt),
    )
    log.info("zoom_participants_call_started", **_start)
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="zoom_participants_call_started",
            **_start,
            system_prompt=PARTICIPANTS_EXTRACT_SYSTEM,
            user_prompt_preview=user_prompt[:1000],
        )
    try:
        text = llm_backend.complete_text(
            system_prompt=PARTICIPANTS_EXTRACT_SYSTEM,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.warning("zoom_participants_llm_failed",
                    model=model, error=str(e))
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="zoom_participants_llm_failed",
                        model=model, error=str(e))
        return []
    try:
        result = _json.loads(text) if text else {}
    except _json.JSONDecodeError:
        log.warning(
            "zoom_participants_json_parse_failed",
            text_preview=text[:200],
        )
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="zoom_participants_json_parse_failed",
                        text_preview=text[:500])
        return []
    if not isinstance(result, dict):
        return []
    raw = result.get("participants") or []
    if not isinstance(raw, list):
        raw = []
    valid_names = {(tm.get("real_name") or "").strip() for tm in team_members}
    valid_names.discard("")
    out: list[str] = []
    seen: set[str] = set()
    for name in raw:
        if not isinstance(name, str):
            continue
        n = name.strip()
        if not n or n in seen or n not in valid_names:
            continue
        seen.add(n)
        out.append(n)
    log.info(
        "zoom_participants_done",
        participants_count=len(out),
        participants=out,
    )
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="zoom_participants_done",
            participants=out,
            raw_response_full=result,
        )
    return out


__all__ = [
    "PARTICIPANTS_EXTRACT_SYSTEM",
    "extract_zoom_participants_via_llm",
]
