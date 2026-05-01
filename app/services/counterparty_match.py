"""FR-CR-05-125 — match counterparty mentions in a meeting
transcript to canonical rows in the `counterparties` directory.

The matcher passes the full directory (just `name + type`, no
attributes) as context to the LLM along with the transcript.
The LLM returns the SUBSET of directory ids that the transcript
actually mentions — fuzzy-tolerant (speech recognition produces
«Адног» for «ADNOC», legal-form variants etc.).

Why an LLM call rather than a pure-Python fuzzy match:
- Russian transcripts mix Cyrillic + Latin spelling for the
  same brand («Гольдман Сакс» / «Goldman Sachs»). Static
  similarity scores miss these.
- The LLM filters out mentions that aren't real counterparty
  references (e.g. someone's first name happens to be «Felix»
  but the meeting wasn't about Felix Capital).
- A single 1-call match for ~25 mentions is cheaper than per-
  candidate similarity scoring.

Returns canonical hub ids; caller persists them as
`CounterpartyMention` rows for audit + querying.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Counterparty

log = get_logger(__name__)


COUNTERPARTY_MATCH_SYSTEM = """\
You match COMPANIES / FUNDS / ORGANISATIONS mentioned in a
business-meeting transcript to a directory of known
counterparties (FR-CR-05-125).

Input (in the user prompt):
  - The full transcript (may include Whisper noise / typos).
  - A `directory` table: `id | name | type` of every known
    counterparty.

Output via the provided tool: a list of `directory.id` values
for ONLY the counterparties the transcript actually references.

THINK CAREFULLY:

1. Speech recognition is fuzzy. «Адног» / «АДНОК» / «Adn-OC» →
   match «ADNOC» (id N). «Гольдман Сакс» / «Голдман» → «Goldman
   Sachs». Treat short forms / partial spellings / accent
   variants / Cyrillic↔Latin transliterations as the same
   counterparty when context confirms it.

2. ONLY include counterparties the transcript actually
   discusses or names. A passing reference («like ADNOC's
   pilot») counts. A coincidental name match («Felix» the
   first name vs. «Felix Capital» the fund) DOES NOT count
   unless the surrounding text references the fund.

3. NEVER invent ids that aren't in the directory. Skip the
   mention rather than guessing.

4. Output the ids in the order they first appear in the
   transcript so the consumer can render them in chronological
   order.

5. If the transcript mentions a counterparty that's NOT in the
   directory (operator hasn't added it yet), simply omit it.
   Don't try to add new entries — that's the operator's job
   via the source sheet.

When you find no matches, return `{"matched_ids": []}`.

Respond via the `record_counterparty_matches` tool.
"""


COUNTERPARTY_MATCH_TOOL_NAME = "record_counterparty_matches"
COUNTERPARTY_MATCH_TOOL_DESCRIPTION = (
    "Record the directory ids of counterparties that the "
    "meeting transcript actually mentions."
)
COUNTERPARTY_MATCH_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matched_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Directory `id` values, in order of first "
                "appearance in the transcript."
            ),
        },
    },
    "required": ["matched_ids"],
}


def _render_directory(rows: list[Counterparty]) -> str:
    """Compact directory rendering for the user prompt. Three
    columns: id | type | name. Notes / status / etc. live on
    the satellite — the matcher doesn't need them."""
    lines = ["  id    | type                       | name"]
    for cp in rows:
        type_ = (cp.type or "")[:26]
        name = (cp.name or "")[:200]
        lines.append(f"  {cp.id:<5} | {type_:<26} | {name}")
    return "\n".join(lines)


def match_counterparties_in_transcript(
    session: Session,
    *,
    transcript: str,
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = None,
) -> list[Counterparty]:
    """Run the LLM matcher and return the matched Counterparty
    rows in transcript order. Returns [] when:
      - no transcript text;
      - directory is empty;
      - LLM call fails (logged);
      - LLM returns no matches.
    Doesn't persist anything — caller writes
    `CounterpartyMention` rows.
    """
    if not transcript:
        return []
    directory = (
        session.query(Counterparty)
        .order_by(Counterparty.type, Counterparty.name)
        .all()
    )
    if not directory:
        return []
    user_prompt = (
        "directory:\n"
        + _render_directory(directory)
        + "\n\nТранскрипт встречи:\n"
        + transcript
    )
    try:
        result = llm_backend.call_tool(
            system_prompt=COUNTERPARTY_MATCH_SYSTEM,
            user_prompt=user_prompt,
            tool_name=COUNTERPARTY_MATCH_TOOL_NAME,
            tool_description=COUNTERPARTY_MATCH_TOOL_DESCRIPTION,
            tool_parameters=COUNTERPARTY_MATCH_TOOL_PARAMETERS,
            model=model,
            reasoning_effort=reasoning_effort,
        ) or {}
    except Exception as e:  # noqa: BLE001
        log.warning(
            "counterparty_match_llm_failed",
            model=model, error=str(e),
        )
        return []
    matched_ids = result.get("matched_ids") or []
    if not isinstance(matched_ids, list):
        return []
    valid_ids = {cp.id for cp in directory}
    matched_ids = [
        i for i in matched_ids if isinstance(i, int) and i in valid_ids
    ]
    if not matched_ids:
        return []
    by_id = {cp.id: cp for cp in directory}
    # Preserve LLM-emitted order; dedupe.
    seen: set[int] = set()
    out: list[Counterparty] = []
    for i in matched_ids:
        if i in seen:
            continue
        seen.add(i)
        out.append(by_id[i])
    log.info(
        "counterparty_match_done",
        matched=len(out),
        directory_size=len(directory),
    )
    return out


__all__ = [
    "COUNTERPARTY_MATCH_SYSTEM",
    "COUNTERPARTY_MATCH_TOOL_NAME",
    "COUNTERPARTY_MATCH_TOOL_DESCRIPTION",
    "COUNTERPARTY_MATCH_TOOL_PARAMETERS",
    "match_counterparties_in_transcript",
]
