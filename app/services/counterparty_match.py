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
    max_directory_rows: int = 1000,
) -> list["Counterparty"]:
    """FR-CR-05-126 / FR-CR-05-128 — Python-side fuzzy
    prefilter so the LLM only sees plausible candidates instead
    of all 600+ rows.

    FR-CR-05-128 — cap raised 300 → 500 → 1000 because the
    operator's directory is ~573 rows (after dedupe). With
    Russian transcripts producing many incidental substring
    boosts («капитал»→«kapital» substring-matches every «X
    Capital» row at 0.95), borderline phonetic matches like
    «Тезер»→«tether» (ratio 0.73) keep getting pushed past
    smaller caps. At 1000 the cap is effectively absent for the
    current directory size — we keep the fuzzy filter only to
    drop the truly-unrelated rows (those scoring < 0.6) and
    pass everything else.

    Strategy:
      - tokenise both the transcript and each directory name;
      - transliterate Cyrillic → Latin so «шафлера» (Whisper)
        matches «Schaeffler» (directory);
      - score each directory row by SequenceMatcher ratio of
        any name token vs any transcript token;
      - keep rows ≥ 0.6 OR substring-equal;
      - cap the result at `max_directory_rows`.

    Falls through to the full directory if shortlist would be
    empty — better to send everything than miss a match.
    """
    if not directory or not transcript:
        return directory[:max_directory_rows]
    import difflib
    import re
    import unicodedata

    # FR-CR-05-126 — Cyrillic → Latin transliteration. Standard
    # ISO 9 / common Russian translit so «шафлера» → «shaflera»,
    # «инвидио» → «invidio», «эдиа» → «edia», etc. Lossy but
    # close enough that fuzzy ratio against canonical Latin
    # names like «Schaeffler» / «Nvidia» / «ADIA» lands above
    # the 0.65 threshold.
    _CYR_TO_LAT = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
        "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
        "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }

    def _translit(s: str) -> str:
        return "".join(_CYR_TO_LAT.get(ch, ch) for ch in s)

    def _fold(s: str) -> str:
        # NFKD-decompose accents, drop combining marks, lower,
        # then translit any leftover Cyrillic.
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        return _translit(s.lower())

    def _tokens(s: str) -> set[str]:
        # Latin-only tokens after the fold (translit puts
        # Cyrillic → Latin already). 3+ char alnum runs.
        return set(re.findall(r"[a-z0-9]{3,}", _fold(s)))

    transcript_tokens = _tokens(transcript)
    if not transcript_tokens:
        return directory[:max_directory_rows]

    scored: list[tuple[float, Counterparty]] = []
    for cp in directory:
        name_tokens = _tokens(cp.name or "")
        if not name_tokens:
            continue
        best = 0.0
        for n in name_tokens:
            for t in transcript_tokens:
                # Substring match: strong signal when the
                # directory token appears as a substring of a
                # transcript token (or vice versa). 4+ char
                # threshold avoids false hits like «inc» ⊂
                # «invest».
                if n == t:
                    best = 1.0
                    break
                if len(n) >= 4 and n in t:
                    # FR-CR-05-126 follow-up — boost long
                    # substring matches («schaeffler» ⊂ «schaefflera»
                    # in a translit transcript token would be
                    # caught here; the operator's regression
                    # «шафлера» translits to «shaflera» which
                    # contains «schaffl» partially).
                    best = max(best, 0.95)
                    continue
                if len(t) >= 4 and t in n:
                    best = max(best, 0.95)
                    continue
                # Length-aware ratio. «schaeffler» 10 vs
                # «shaflera» 8 (translit of «шафлера»):
                # SequenceMatcher ratio ≈ 0.67 → above 0.6 so
                # the match lands.
                if abs(len(n) - len(t)) <= max(4, len(n) // 2):
                    r = difflib.SequenceMatcher(None, n, t).ratio()
                    if r > best:
                        best = r
            if best >= 1.0:
                break
        # FR-CR-05-126 follow-up — threshold relaxed 0.65 → 0.6
        # because Cyrillic-translit edit distances tend to land
        # in the 0.6-0.7 range («shaflera» vs «schaeffler» ≈
        # 0.67).
        if best >= 0.6:
            scored.append((best, cp))

    if not scored:
        return directory[:max_directory_rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [cp for _, cp in scored[:max_directory_rows]]


def match_counterparties_in_transcript(
    session: Session,
    *,
    transcript: str,
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = None,
    trace_source: str | None = None,
    trace_recording_id: str | None = None,
) -> list[Counterparty]:
    """Run the LLM matcher and return the matched Counterparty
    rows in transcript order. `trace_source` (`"fireflies"` /
    `"zoom"`) + `trace_recording_id` thread per-recording trace
    events to `/app/traces/<source>-<id>.jsonl` (FR-CR-05-128).
    Returns [] when no transcript / empty directory / LLM
    failure / no matches. Doesn't persist anything.
    """
    from app.services.trace_log import trace_event

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
        if trace_source:
            trace_event(
                source=trace_source, recording_id=trace_recording_id,
                event="counterparty_match_skipped_empty_directory",
                transcript_chars=len(transcript),
            )
        return []
    # FR-CR-05-126 — Python-side fuzzy prefilter narrows the
    # directory to plausible candidates before the LLM call.
    shortlist = _shortlist_directory_for_transcript(directory, transcript)
    user_prompt = (
        "directory:\n"
        + _render_directory(shortlist)
        + "\n\nТранскрипт встречи:\n"
        + transcript
    )
    # FR-CR-05-128 — diagnose «X в таблице есть, в матче не»:
    # the trace MUST show whether the entry made it into the
    # shortlist + the LLM saw it. Log the FULL shortlist (id +
    # name only) and the FULL prompt to the trace file (NOT the
    # docker logs — too noisy), and the FULL LLM raw response.
    _start_log_payload = dict(
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
    log.info("counterparty_match_call_started", **_start_log_payload)
    if trace_source:
        # Trace gets the FULL detail (file is per-recording, not
        # rotated) — no truncation on shortlist, full prompt
        # body, full transcript. Operator can `cat traces/...
        # | jq '.fields.shortlist_full'` to verify Tether-class
        # entries are actually being sent to the LLM.
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="counterparty_match_call_started",
            **_start_log_payload,
            shortlist_full=[
                {"id": cp.id, "name": cp.name, "type": cp.type}
                for cp in shortlist
            ],
            user_prompt_full=user_prompt,
            system_prompt=COUNTERPARTY_MATCH_SYSTEM,
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
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="counterparty_match_llm_failed",
                        model=model, error=str(e))
        return []
    raw_ids = result.get("matched_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = []
    valid_ids = {cp.id for cp in directory}
    invalid_ids = [i for i in raw_ids if i not in valid_ids]
    matched_ids = [
        i for i in raw_ids if isinstance(i, int) and i in valid_ids
    ]
    _ret_log = dict(
        raw_ids_count=len(raw_ids),
        valid_ids_count=len(matched_ids),
        invalid_ids=invalid_ids[:10],
        raw_ids_sample=raw_ids[:10],
    )
    log.info("counterparty_match_llm_returned", **_ret_log)
    if trace_source:
        # Trace gets the full LLM response so the operator can
        # see exactly what came back (was Tether returned and
        # filtered out? did the LLM return a different id?).
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="counterparty_match_llm_returned",
            **_ret_log,
            raw_response_full=result,
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
    _done_payload = dict(
        matched=len(out),
        directory_size=len(directory),
        matched_names=[cp.name for cp in out],
    )
    log.info("counterparty_match_done", **_done_payload)
    if trace_source:
        trace_event(source=trace_source, recording_id=trace_recording_id,
                    event="counterparty_match_done", **_done_payload)
    return out


__all__ = [
    "COUNTERPARTY_MATCH_SYSTEM",
    "COUNTERPARTY_MATCH_TOOL_NAME",
    "COUNTERPARTY_MATCH_TOOL_DESCRIPTION",
    "COUNTERPARTY_MATCH_TOOL_PARAMETERS",
    "match_counterparties_in_transcript",
]
