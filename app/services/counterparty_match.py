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
  - The full transcript (Whisper-transcribed Russian + English
    speech, EXPECT typos and phonetic errors).
  - A `directory` table: `id | name | type` of every known
    counterparty.

Output via the provided tool: a list of `directory.id` values
for the counterparties the transcript actually references.

═══════════════════════════════════════════════════════════════
WHISPER MISHEARS THINGS. THIS IS THE MAIN JOB. (FR-CR-05-126)
═══════════════════════════════════════════════════════════════

The transcript came through speech-to-text on a noisy meeting.
Whisper routinely:

- swaps similar-sounding words: «teaser» ↔ «Tether», «алимета» ↔
  «Altimeter», «прим. кэп» ↔ «Primavera», «эдиа» ↔ «ADIA»;
- transliterates between Cyrillic and Latin: «АДНОК» / «Адног» /
  «Adn-OC» → «ADNOC», «Голдман Сакс» / «Гольдман» → «Goldman
  Sachs», «Гэйтс Фронтиер» → «Gates Frontier», «Лунейт» / «Луна
  Эйт» → «Lunate»;
- collapses or splits multi-word names: «Се́квойя» → «Sequoia
  Capital», «Капричорн» → «Capricorn Investment Group»,
  «Бэттери Венчёрс» → «Battery Ventures»;
- drops legal forms: «Atinum» → «Atinum Investment», «Mubadala»
  → «Mubadala Investment Company».

YOUR JOB IS TO RECOGNISE THESE ANYWAY. When a transcript word
phonetically resembles a directory entry within reasonable edit
distance AND the surrounding sentence is about a fund / company
(deal, intro, follow-up, term sheet, KYC, NDA, ticket size,
round, fundraise…), prefer the match.

Worked examples (operator-pinned regressions):

  transcript: «надо отправить teaser в Tether-овый раунд»
              (Whisper heard «teaser» where speaker said «tether»)
  directory: 314 | Status outreach | Tether
  → matched_ids = [314]

  transcript: «дозвонились до Адног, у них pilot в нефтегазе»
  directory: 27  | Status outreach | ADNOC
  → matched_ids = [27]

  transcript: «Голдман Сакс прислали ответ»
  directory: 102 | Outreach          | Goldman Sachs
  → matched_ids = [102]

NOT matches (anti-examples):

  transcript: «Felix будет нашим SDR» (about a person named
              Felix, not «Felix Capital» the fund)
  → don't include Felix Capital
  transcript: «отправили teaser deck» (industry term for a short
              pitch deck — not the company Tether)
  → don't include Tether unless other context confirms

═══════════════════════════════════════════════════════════════

OUTPUT RULES:

1. NEVER invent ids that aren't in the directory. Skip rather
   than guess.
2. Output ids in the order the counterparty first appears in
   the transcript.
3. Dedupe — one id per counterparty even if mentioned multiple
   times.
4. If the transcript mentions a counterparty that's NOT in the
   directory, simply omit it. Don't try to add new entries.
5. When you find no matches at all, return
   `{"matched_ids": []}`. Empty is a valid answer when the
   meeting was internal-only (no external counterparties named).

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


def _shortlist_directory_for_transcript(
    directory: list["Counterparty"],
    transcript: str,
    *,
    max_directory_rows: int = 200,
) -> list["Counterparty"]:
    """FR-CR-05-126 — Python-side fuzzy prefilter so the LLM
    only sees plausible candidates instead of all 600+ rows.
    Boosts both recall (Whisper-mangled names land in the
    shortlist) and precision (fewer distractors).

    Strategy:
      - tokenise both the transcript and each directory name
        into lowercase ASCII-folded word stems;
      - score each directory row by SequenceMatcher ratio of
        its longest name token to ANY transcript token (so a
        single fuzzy hit is enough — operator's regression was
        a 1-word company name «Tether» misheard as «teaser»);
      - keep rows above a low threshold OR rows whose name is
        a substring of any 4+ char transcript token;
      - cap the result at `max_directory_rows` so the prompt
        stays bounded even on transcripts that mention many
        candidates.

    Falls through to the full directory if shortlist would be
    empty — better to send everything than to miss a match the
    LLM would otherwise catch.
    """
    if not directory or not transcript:
        return directory[:max_directory_rows]
    import difflib
    import re
    import unicodedata

    def _fold(s: str) -> str:
        s = unicodedata.normalize("NFKD", s or "")
        return "".join(c for c in s if not unicodedata.combining(c)).lower()

    def _tokens(s: str) -> set[str]:
        # 3+ char alnum runs; drops noise words.
        return set(re.findall(r"[a-zа-яё]{3,}", _fold(s)))

    transcript_tokens = _tokens(transcript)
    if not transcript_tokens:
        return directory[:max_directory_rows]

    scored: list[tuple[float, Counterparty]] = []
    for cp in directory:
        name_tokens = _tokens(cp.name or "")
        if not name_tokens:
            continue
        # Best ratio of any name-token to any transcript-token.
        best = 0.0
        for n in name_tokens:
            for t in transcript_tokens:
                # Cheap shortcut: identical or substring → max
                # signal (catches «Tether» ⊂ «tether» / direct).
                if n == t or n in t or t in n:
                    best = 1.0
                    break
                # Real fuzzy ratio for short tokens (saves cost
                # on long ones — the substring check above
                # already covered them).
                if abs(len(n) - len(t)) <= 3:
                    r = difflib.SequenceMatcher(None, n, t).ratio()
                    if r > best:
                        best = r
            if best >= 1.0:
                break
        if best >= 0.78:
            scored.append((best, cp))

    if not scored:
        return directory[:max_directory_rows]
    # Highest-score first; cap at limit.
    scored.sort(key=lambda x: x[0], reverse=True)
    return [cp for _, cp in scored[:max_directory_rows]]


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
        log.info(
            "counterparty_match_skipped_empty_directory",
            transcript_chars=len(transcript),
        )
        return []
    # FR-CR-05-126 — Python-side fuzzy prefilter narrows the
    # directory to plausible candidates before the LLM call.
    # Boosts both recall (Whisper-mangled names land in the
    # shortlist) and precision (fewer distractors).
    shortlist = _shortlist_directory_for_transcript(directory, transcript)
    user_prompt = (
        "directory:\n"
        + _render_directory(shortlist)
        + "\n\nТранскрипт встречи:\n"
        + transcript
    )
    log.info(
        "counterparty_match_call_started",
        model=model,
        reasoning_effort=reasoning_effort,
        directory_size=len(directory),
        shortlist_size=len(shortlist),
        shortlist_sample=[
            {"id": cp.id, "name": cp.name, "type": cp.type}
            for cp in shortlist[:8]
        ],
        transcript_chars=len(transcript),
        transcript_preview=transcript[:240],
        prompt_chars=len(user_prompt),
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
    raw_ids = result.get("matched_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = []
    valid_ids = {cp.id for cp in directory}
    invalid_ids = [i for i in raw_ids if i not in valid_ids]
    matched_ids = [
        i for i in raw_ids if isinstance(i, int) and i in valid_ids
    ]
    # FR-CR-05-126 — post-call trace: what the LLM raw-emitted,
    # what the filter dropped, what survived.
    log.info(
        "counterparty_match_llm_returned",
        raw_ids_count=len(raw_ids),
        valid_ids_count=len(matched_ids),
        invalid_ids=invalid_ids[:10],
        raw_ids_sample=raw_ids[:10],
    )
    if not matched_ids:
        return []
    by_id = {cp.id: cp for cp in directory}
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
        matched_names=[cp.name for cp in out],
    )
    return out


__all__ = [
    "COUNTERPARTY_MATCH_SYSTEM",
    "COUNTERPARTY_MATCH_TOOL_NAME",
    "COUNTERPARTY_MATCH_TOOL_DESCRIPTION",
    "COUNTERPARTY_MATCH_TOOL_PARAMETERS",
    "match_counterparties_in_transcript",
]
