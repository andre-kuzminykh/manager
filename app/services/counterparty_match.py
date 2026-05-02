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


# ============================================================
# FR-CR-05-129 — two-pass canonical name resolution
# ============================================================

COUNTERPARTY_EXTRACT_SYSTEM = """\
You are a transcript-reader. Your ONE job: list every
COUNTERPARTY mention in this business-meeting transcript,
verbatim as it appears (including Whisper-mangled phonetic
forms).

A counterparty is a COMPANY / FUND / INVESTOR / CLIENT /
ORGANISATION name. NOT person names of internal speakers,
NOT generic terms like «инвесторы», «фонд», «раунд».

═══════════════════════════════════════════════════════════════
EXTRACT EVERY DISTINCT FORM, PHONETIC OR NOT.

Whisper transcripts contain MANY phonetic / Cyrillic-Latin
variants of the same fund. Your job is to LIST THEM ALL —
matching them to canonical names is a separate downstream step.

Examples (one transcript, multiple forms of the same entity):

  «Тезер»              ← Whisper-phonetic for Tether
  «тезер»              ← lowercase / re-mention
  «teaser»             ← Whisper sometimes hears it Latin
  «Tether»             ← if speaker said it Latin

  «Bauer/Dart»         ← Whisper-rendered with slash
  «BauerDart»          ← compound
  «Баутерт»            ← phonetic Cyrillic
  «Bower/Баутерт»      ← mixed

  «Шафлер» / «Schaeffler» / «Шаффлер»

  «Голдман» / «Голдман Сакс» / «Goldman Sachs»

  «Адног» / «АДНОК» / «ADNOC»

LIST EVERY UNIQUE STRING. Don't try to merge them — that's
the next pass. Don't normalize case. Don't translit. Just
copy the surface form from the transcript.
═══════════════════════════════════════════════════════════════

OUTPUT RULES:

1. Return all distinct surface forms (case-sensitive). If the
   transcript has «Тезер» twice and «тезер» once, return
   {«Тезер», «тезер»} (two entries).
2. Skip generic words: «инвестор», «фонд», «компания»,
   «раунд», «контракт», «клиент» without a brand name.
3. Skip first names of internal Humanoid team members —
   they're speakers, not counterparties.
4. Empty list is valid (internal-only meeting): `{"mentions": []}`.

Respond as a JSON object: `{"mentions": ["<verbatim>", ...]}`.
"""


COUNTERPARTY_EXTRACT_TOOL_NAME = "record_counterparty_mentions"
COUNTERPARTY_EXTRACT_TOOL_DESCRIPTION = (
    "Record every distinct counterparty mention surface form "
    "from the meeting transcript."
)
COUNTERPARTY_EXTRACT_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mentions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Distinct surface forms (case-sensitive) of "
                "counterparty mentions in the transcript. "
                "Include phonetic variants verbatim."
            ),
        },
    },
    "required": ["mentions"],
}


COUNTERPARTY_RESOLVE_SYSTEM = """\
You map each counterparty MENTION (as it appears in a meeting
transcript, possibly Whisper-mangled) to a directory entry's
canonical id — or to `null` when the directory has no entry
for it.

Input (in the user prompt):
  - `mentions`: a numbered list of strings the previous LLM
    extracted from the transcript verbatim («Тезер»,
    «Bauer/Dart», «Шафлер»…).
  - `directory`: `id | type | name` for every known
    counterparty.

═══════════════════════════════════════════════════════════════
PHONETIC + CYRILLIC↔LATIN MATCHING IS THE WHOLE JOB.

For each mention, find the directory entry that's the same
underlying entity, even if spelling differs:
  - «Тезер»       → «Tether» (phonetic Cyrillic of Tether)
  - «teaser»      → «Tether» (Whisper one-off swap)
  - «Bauer/Dart»  → «Bauerdart» (slash-rendered compound)
  - «Баутерт»     → «Bauerdart» (Cyrillic phonetic)
  - «Шафлер»      → «Schaeffler»
  - «Голдман»     → «Goldman Sachs»
  - «Адног»       → «ADNOC»

Multiple mentions can resolve to the SAME directory id (that's
the point — Whisper's phonetic variants are the same entity).

Map to `null` ONLY when no directory entry plausibly matches.
NEVER invent ids.
═══════════════════════════════════════════════════════════════

OUTPUT RULES:

1. Return one entry per input mention (preserve order).
2. Each entry is `{"mention": <input string>, "directory_id":
   <int|null>}`.
3. Same directory id may appear multiple times if multiple
   mentions resolve to it.
4. NEVER guess an id that's not in the directory.

Respond as a JSON object: `{"matches": [{"mention": <input>, "directory_id": <int|null>}, ...]}`.
"""


COUNTERPARTY_RESOLVE_TOOL_NAME = "record_resolved_mentions"
COUNTERPARTY_RESOLVE_TOOL_DESCRIPTION = (
    "Record the directory id each counterparty mention "
    "resolves to (or null if no match)."
)
COUNTERPARTY_RESOLVE_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mention": {"type": "string"},
                    "directory_id": {"type": ["integer", "null"]},
                },
                "required": ["mention", "directory_id"],
            },
        },
    },
    "required": ["matches"],
}


# FR-CR-05-126 / -129 — module-level Cyrillic→Latin map, used
# both by the fuzzy prefilter inside the matcher AND by the
# fuzzy fallback that extends the canonical-rewrite map over
# task content (`fuzzy_extend_canonical_map`).
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
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


def extract_counterparty_mentions(
    transcript: str,
    *,
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = None,
    trace_source: str | None = None,
    trace_recording_id: str | None = None,
) -> list[str]:
    """FR-CR-05-129 Pass 1 — list every counterparty mention
    surface form in the transcript verbatim («Тезер»,
    «Bauer/Dart», «Шафлер»). Returns a deduped list preserving
    first-appearance order.

    FR-CR-05-129 follow-up — uses `complete_text` with JSON
    response_format + `reasoning_effort` instead of `call_tool`
    because gpt-5.5 in /v1/chat/completions rejects
    «reasoning_effort + function tools» (400 Bad Request) and
    the retry-without-reasoning-effort path leaves the LLM too
    shallow to phonetically match «Тезер» / «Bauer/Dart» style
    Whisper-mangled forms.
    """
    import json as _json

    from app.services.trace_log import trace_event

    if not transcript:
        return []
    user_prompt = (
        "Return JSON: `{\"mentions\": [\"<verbatim mention>\", ...]}`."
        " Empty list ok.\n\nТранскрипт встречи:\n"
        + transcript
    )
    _start = dict(model=model, reasoning_effort=reasoning_effort,
                  transcript_chars=len(transcript))
    log.info("counterparty_extract_call_started", **_start)
    if trace_source:
        trace_event(source=trace_source, recording_id=trace_recording_id,
                    event="counterparty_extract_call_started",
                    **_start, system_prompt=COUNTERPARTY_EXTRACT_SYSTEM,
                    user_prompt_preview=user_prompt[:1000])
    try:
        text = llm_backend.complete_text(
            system_prompt=COUNTERPARTY_EXTRACT_SYSTEM,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.warning("counterparty_extract_llm_failed",
                    model=model, error=str(e))
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="counterparty_extract_llm_failed",
                        model=model, error=str(e))
        return []
    try:
        result = _json.loads(text) if text else {}
    except _json.JSONDecodeError:
        log.warning("counterparty_extract_json_parse_failed",
                    text_preview=text[:200])
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="counterparty_extract_json_parse_failed",
                        text_preview=text[:500])
        return []
    if not isinstance(result, dict):
        return []
    raw = result.get("mentions") or []
    if not isinstance(raw, list):
        raw = []
    seen: set[str] = set()
    out: list[str] = []
    for m in raw:
        if not isinstance(m, str):
            continue
        m = m.strip()
        if not m or m.lower() in seen:
            continue
        seen.add(m.lower())
        out.append(m)
    log.info(
        "counterparty_extract_done",
        mentions_count=len(out),
        mentions_sample=out[:25],
    )
    if trace_source:
        trace_event(source=trace_source, recording_id=trace_recording_id,
                    event="counterparty_extract_done",
                    mentions_count=len(out),
                    mentions=out, raw_response_full=result)
    return out


def resolve_mentions_to_directory(
    mentions: list[str],
    directory: list[Counterparty],
    *,
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = None,
    trace_source: str | None = None,
    trace_recording_id: str | None = None,
) -> dict[str, int | None]:
    """FR-CR-05-129 Pass 2 — for each Pass-1 mention, ask the
    LLM which directory id it resolves to (or null when no
    match). Returns a mention → directory_id mapping, deduped.
    Uses the FULL directory in the prompt (no fuzzy shortlist
    — Pass 1 already filtered the universe down to actual
    mentions).
    """
    from app.services.trace_log import trace_event

    if not mentions:
        return {}
    if not directory:
        return {m: None for m in mentions}
    mentions_block = "\n".join(
        f"  {i+1}. {m}" for i, m in enumerate(mentions)
    )
    import json as _json

    user_prompt = (
        "Return JSON: `{\"matches\": [{\"mention\": ..., "
        "\"directory_id\": <int|null>}, ...]}`.\n\n"
        "mentions:\n" + mentions_block
        + "\n\ndirectory:\n" + _render_directory(directory)
    )
    _start = dict(
        model=model, reasoning_effort=reasoning_effort,
        mentions_count=len(mentions),
        directory_size=len(directory),
        prompt_chars=len(user_prompt),
    )
    log.info("counterparty_resolve_call_started", **_start)
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="counterparty_resolve_call_started",
            **_start, mentions=mentions,
            system_prompt=COUNTERPARTY_RESOLVE_SYSTEM,
            user_prompt_full=user_prompt,
        )
    try:
        text = llm_backend.complete_text(
            system_prompt=COUNTERPARTY_RESOLVE_SYSTEM,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.warning("counterparty_resolve_llm_failed",
                    model=model, error=str(e))
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="counterparty_resolve_llm_failed",
                        model=model, error=str(e))
        return {m: None for m in mentions}
    try:
        result = _json.loads(text) if text else {}
    except _json.JSONDecodeError:
        log.warning("counterparty_resolve_json_parse_failed",
                    text_preview=text[:200])
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="counterparty_resolve_json_parse_failed",
                        text_preview=text[:500])
        return {m: None for m in mentions}
    if not isinstance(result, dict):
        return {m: None for m in mentions}
    matches = result.get("matches") or []
    if not isinstance(matches, list):
        matches = []
    valid_ids = {cp.id for cp in directory}
    by_id = {cp.id: cp for cp in directory}
    out: dict[str, int | None] = {}
    for entry in matches:
        if not isinstance(entry, dict):
            continue
        mention = entry.get("mention")
        cid = entry.get("directory_id")
        if not isinstance(mention, str):
            continue
        if cid is not None and cid not in valid_ids:
            cid = None
        out[mention] = cid
    # Fill in any mentions the LLM forgot to map.
    for m in mentions:
        if m not in out:
            out[m] = None
    resolved_mapping = [
        {
            "mention": m,
            "directory_id": cid,
            "canonical_name": (by_id[cid].name if cid else None),
        }
        for m, cid in out.items()
    ]
    log.info(
        "counterparty_resolve_done",
        mentions_count=len(mentions),
        resolved_count=sum(1 for v in out.values() if v is not None),
        unresolved_count=sum(1 for v in out.values() if v is None),
        sample=resolved_mapping[:10],
    )
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="counterparty_resolve_done",
            resolved_count=sum(1 for v in out.values() if v is not None),
            unresolved_count=sum(1 for v in out.values() if v is None),
            mapping=resolved_mapping,
            raw_response_full=result,
        )
    return out


CANONICALIZE_TASKS_SYSTEM = """\
You rewrite task titles + descriptions so every counterparty
mention uses the CANONICAL name from the directory.

Input (in the user prompt):
  - `directory`: `id | type | name` for every known counterparty.
  - `tasks`: numbered list `[id]: title // description`.

Output via JSON: list of rewritten tasks. Each entry must have
`id`, `title_rewritten`, `description_rewritten`. PRESERVE the
order. PRESERVE everything that's not a counterparty mention
(actions, owners, dates, amounts) verbatim. Replace ONLY
phonetic / mangled / Cyrillic variants of counterparty names
with the canonical Latin name from the directory.

═══════════════════════════════════════════════════════════════
EXAMPLES (operator-pinned phonetic mishears):

  task: «Bowerdorf - пригласить на демо»
    directory has: «Bauerdart»
  → rewritten: «Bauerdart - пригласить на демо»

  task: «Felix CapitalG - проверить чек»
    directory has: «Felix Capital»
  → rewritten: «Felix Capital - проверить чек»

  task: «Jamal/Jabal - график демо»
    directory has: «Jabal»
  → rewritten: «Jabal - график демо»

  task: «отправить апдейт Тезер»
    directory has: «Tether»
  → rewritten: «отправить апдейт Tether»

  task: «Согласовать формулировку Шафлер»
    directory has: «Schaeffler»
  → rewritten: «Согласовать формулировку Schaeffler»

  task: «Подготовить follow-up по Insight»
    directory has: «Insight Partners»
  → rewritten: «Подготовить follow-up по Insight Partners»
═══════════════════════════════════════════════════════════════

OUTPUT RULES:

1. Don't add or remove tasks. Same count, same order.
2. Don't invent new counterparties. If a mention doesn't match
   any directory row, leave it as-is.
3. Don't change action verbs, dates, amounts, owners, or any
   non-counterparty word.
4. Use the canonical directory NAME exactly as written
   (case-sensitive).
5. When the input task text already uses the canonical name,
   leave the title/description untouched.

Respond as a JSON object: `{"rewritten": [{"id": <int>, "title_rewritten": ..., "description_rewritten": ...}, ...]}`.
"""


CONSOLIDATE_TASKS_SYSTEM = """\
You receive a list of meeting-extracted tasks and consolidate
them into a clean, non-overlapping set. Output the cleaned
list. PRESERVE every distinct piece of operator-actionable
information; only collapse near-duplicates that describe THE
SAME single deliverable.

Operator-pinned (FR-CR-05-131): «мне всегда надо максимум
информации» — granularity beats brevity. Don't over-merge.
But sequential phases of one action OR composite topics
(two unrelated counterparties slammed together) DO need
fixing.

═══════════════════════════════════════════════════════════════
WHEN TO MERGE TWO TASKS INTO ONE
═══════════════════════════════════════════════════════════════

Two tasks should be merged if they describe sequential phases
of the SAME deliverable on the SAME counterparty / topic where
the second is a precondition / continuation that adds no new
operator-actionable detail. Combine descriptions, keep the
broader scope.

Examples to MERGE:
  • «Tether — формулировка апдейта» + «Tether — отправить
    email во вторник» + «Tether — короткое сообщение в
    WhatsApp» → ONE task «Tether — отправить апдейт по email
    во вторник + продублировать в WhatsApp».
  • «Bauerdart — найти главного человека» + «Bauerdart —
    организовать встречу при согласии» → ONE task «Bauerdart
    — пригласить главного человека на demo в офис».
  • «Felix Capital — добавить в список кандидатов» + «Felix
    Capital — подготовить письмо» + «Felix Capital — на
    звонке проверить чек 30 млн» → ONE task «Felix Capital —
    подготовить письмо и вывести на звонок про чек 30 млн и
    варант».

Do NOT merge if:
  • Different counterparties («Felix Capital» vs «Supernova»).
  • Different verbs that produce DIFFERENT artefacts
    («подготовить письмо» AND «подготовить справку»).
  • Different owners.

═══════════════════════════════════════════════════════════════
WHEN TO SPLIT ONE TASK INTO TWO
═══════════════════════════════════════════════════════════════

If a task's topic is a COMPOSITE of two unrelated entities
(e.g. «Ziya/Odeya», «Lunate/Antonov + Lunate/Tokarev»), split
into one task per entity. Each gets its own description.

Don't split if the entity is genuinely one thing
(«TWG global» is a single fund — don't split «global»;
«Bauer/Dart» is one company name with a slash; «Schaeffler/
Bosch» is the contract context, not two separate task topics).

═══════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════

Respond as a JSON object:
  `{"consolidated": [{"id": <int|null>,
                       "merged_from": [<int>, ...],
                       "title": <str>,
                       "description": <str>,
                       "owner": <slack_user_id|null>,
                       "priority": "low"|"medium"|"high"|"urgent"}, ...]}`

- `id`: original task id when output is a single existing task
  (no merge / no split). `null` for new merged or split tasks.
- `merged_from`: list of input ids that this output covers.
  For split: `[<original_id>]` (one input → multiple outputs).
  For merge: `[<id1>, <id2>, ...]`.
  For unchanged: `[<id>]`.
- `title`: imperative verb-phrase, ≤80 chars, RU.
- `description`: «<тема-или-фонд> - <verb action with details>»
  format (FR-CR-05-128).
- `owner`: slack_user_id from the original task (preserve when
  merging same-owner tasks; null when conflict / unknown).
"""


def consolidate_tasks_via_llm(
    tasks: list[dict],
    *,
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = None,
    trace_source: str | None = None,
    trace_recording_id: str | None = None,
) -> list[dict]:
    """FR-CR-05-131 — LLM consolidation pass: merges sequential
    phases of one action and splits composite topics. Returns
    the cleaned list. Each entry has `merged_from` so the
    pipeline can mirror the merge into the DB (soft-delete
    the merged-into-others, update the kept one).

    `tasks` shape: `[{id, title, description, owner,
    owner_display_name, priority}, ...]`.

    Empty / parse-failure → returns the input unchanged.
    """
    import json as _json

    from app.services.trace_log import trace_event

    if not tasks:
        return list(tasks)
    tasks_block = "\n".join(
        f"  [{t['id']}] {(t.get('owner_display_name') or t.get('owner') or '—')}: "
        f"{(t.get('title') or '').strip()} // "
        f"{(t.get('description') or '').strip()[:300]}"
        for t in tasks if t.get("id") is not None
    )
    user_prompt = "tasks:\n" + tasks_block
    _start = dict(
        model=model, reasoning_effort=reasoning_effort,
        tasks_count=len(tasks),
        prompt_chars=len(user_prompt),
    )
    log.info("consolidate_tasks_call_started", **_start)
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="consolidate_tasks_call_started",
            **_start, system_prompt=CONSOLIDATE_TASKS_SYSTEM,
            user_prompt_full=user_prompt,
        )
    try:
        text = llm_backend.complete_text(
            system_prompt=CONSOLIDATE_TASKS_SYSTEM,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.warning("consolidate_tasks_llm_failed",
                    model=model, error=str(e))
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="consolidate_tasks_llm_failed",
                        model=model, error=str(e))
        return list(tasks)
    try:
        result = _json.loads(text) if text else {}
    except _json.JSONDecodeError:
        log.warning("consolidate_tasks_json_parse_failed",
                    text_preview=text[:200])
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="consolidate_tasks_json_parse_failed",
                        text_preview=text[:500])
        return list(tasks)
    if not isinstance(result, dict):
        return list(tasks)
    items = result.get("consolidated") or []
    if not isinstance(items, list):
        return list(tasks)
    valid_ids = {t["id"] for t in tasks}
    out: list[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        merged_raw = entry.get("merged_from")
        if isinstance(merged_raw, list):
            merged_from = [
                m for m in merged_raw
                if isinstance(m, int) and m in valid_ids
            ]
        else:
            merged_from = []
        out.append({
            "id": entry.get("id") if isinstance(entry.get("id"), int) else None,
            "merged_from": merged_from,
            "title": title[:10_000],
            "description": (entry.get("description") or "").strip(),
            "owner": entry.get("owner"),
            "priority": entry.get("priority") or "medium",
        })
    log.info(
        "consolidate_tasks_done",
        input_count=len(tasks),
        output_count=len(out),
        merged_count=sum(1 for o in out if len(o["merged_from"]) > 1),
        split_count=sum(
            1 for o in out
            if len(o["merged_from"]) == 1 and o["id"] is None
        ),
    )
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="consolidate_tasks_done",
            input_count=len(tasks),
            output_count=len(out),
            samples=out[:25],
            raw_response_full=result,
        )
    return out


def canonicalize_task_content_via_llm(
    tasks: list[dict],
    directory: list["Counterparty"],
    *,
    llm_backend: Any,
    model: str,
    reasoning_effort: str | None = None,
    trace_source: str | None = None,
    trace_recording_id: str | None = None,
) -> dict[int, dict[str, str]]:
    """FR-CR-05-130 — universal LLM rewrite of task content
    to use canonical directory names.

    `tasks` is a list of dicts: `[{id, title, description}, ...]`.
    Returns mapping `{id: {title, description}}` of rewrites
    (only entries that actually changed).

    Replaces the FR-CR-05-129 regex+SequenceMatcher fuzzy
    fallback (operator: «мне надо без regexp это делать, а
    как универсальное решение, там еще будет куча других слов,
    я тут костылями не обойдусь»). LLM with reasoning handles
    every phonetic / Cyrillic / composite variant universally.
    """
    import json as _json

    from app.services.trace_log import trace_event

    if not tasks or not directory:
        return {}
    tasks_block = "\n".join(
        f"  [{t['id']}]: {(t.get('title') or '').strip()} // "
        f"{(t.get('description') or '').strip()[:300]}"
        for t in tasks if t.get("id") is not None
    )
    user_prompt = (
        "directory:\n"
        + _render_directory(directory)
        + "\n\ntasks:\n"
        + tasks_block
    )
    _start = dict(
        model=model, reasoning_effort=reasoning_effort,
        tasks_count=len(tasks),
        directory_size=len(directory),
        prompt_chars=len(user_prompt),
    )
    log.info("canonicalize_tasks_call_started", **_start)
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="canonicalize_tasks_call_started",
            **_start, system_prompt=CANONICALIZE_TASKS_SYSTEM,
            user_prompt_full=user_prompt,
        )
    try:
        text = llm_backend.complete_text(
            system_prompt=CANONICALIZE_TASKS_SYSTEM,
            user_prompt=user_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            response_format={"type": "json_object"},
        ) or ""
    except Exception as e:  # noqa: BLE001
        log.warning("canonicalize_tasks_llm_failed",
                    model=model, error=str(e))
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="canonicalize_tasks_llm_failed",
                        model=model, error=str(e))
        return {}
    try:
        result = _json.loads(text) if text else {}
    except _json.JSONDecodeError:
        log.warning("canonicalize_tasks_json_parse_failed",
                    text_preview=text[:200])
        if trace_source:
            trace_event(source=trace_source, recording_id=trace_recording_id,
                        event="canonicalize_tasks_json_parse_failed",
                        text_preview=text[:500])
        return {}
    if not isinstance(result, dict):
        return {}
    items = result.get("rewritten") or []
    if not isinstance(items, list):
        items = []
    out: dict[int, dict[str, str]] = {}
    by_id = {t["id"]: t for t in tasks if t.get("id") is not None}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("id")
        if not isinstance(tid, int) or tid not in by_id:
            continue
        new_title = (entry.get("title_rewritten") or "").strip()
        new_desc = (entry.get("description_rewritten") or "").strip()
        old = by_id[tid]
        changes: dict[str, str] = {}
        if new_title and new_title != (old.get("title") or "").strip():
            changes["title"] = new_title
        if new_desc and new_desc != (old.get("description") or "").strip():
            changes["description"] = new_desc
        if changes:
            out[tid] = changes
    log.info(
        "canonicalize_tasks_done",
        rewritten=len(out),
        sample=[
            {"id": tid, "title": ch.get("title"), "desc_preview": (ch.get("description") or "")[:80]}
            for tid, ch in list(out.items())[:5]
        ],
    )
    if trace_source:
        trace_event(
            source=trace_source, recording_id=trace_recording_id,
            event="canonicalize_tasks_done",
            rewritten=len(out),
            samples=[
                {"id": tid, **ch}
                for tid, ch in list(out.items())[:20]
            ],
            raw_response_full=result,
        )
    return out


# --- legacy regex fuzzy (kept for reference / soft fallback) ---


def fuzzy_extend_canonical_map(
    text: str | None,
    directory: list["Counterparty"],
    existing_map: dict[str, str],
    *,
    ratio_threshold: float = 0.7,
) -> dict[str, str]:
    """FR-CR-05-129 follow-up — Python-only fuzzy pass that
    finds words in `text` that phonetically match a directory
    canonical name (after Cyrillic→Latin translit) and extends
    the existing mention→canonical mapping. Catches cases the
    LLM-driven Pass 1+Pass 2 missed because the EXTRACT TASKS
    LLM (separate call) saw a different surface form than the
    transcript-extract pass.

    Operator regression: «Jamal» appeared in task description
    even though Pass 1+2 correctly mapped «Jabal» from
    transcript. Different LLM call wrote «Jamal» — fuzzy
    fallback catches it: ratio(«jamal», «jabal»)=0.8.

    Returns a NEW map (existing entries preserved). Word-
    boundary tokenisation; only catches Cap-First or ALL-CAPS
    tokens 4-25 chars long (filters generic verbs / nouns).
    """
    import difflib
    import re
    import unicodedata

    if not text or not directory:
        return dict(existing_map)
    out: dict[str, str] = dict(existing_map)
    existing_lc = {k.lower() for k in existing_map}

    def _fold(s: str) -> str:
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = "".join(_CYR_TO_LAT.get(ch, ch) for ch in s.lower())
        return s.strip()

    # Build a folded-name → canonical-row index over the directory.
    by_fold: dict[str, "Counterparty"] = {}
    for cp in directory:
        f = _fold(cp.name or "")
        if f and len(f) >= 4:
            by_fold.setdefault(f, cp)

    # Tokenise text. Capture 1-3 CapFirst words in a row so
    # multi-word company names («Felix Capital», «Insight
    # Partners», «Goldman Sachs», «TWG global») land as ONE
    # token AND can be fuzzy-matched against multi-word
    # canonical names. FR-CR-05-129 follow-up — operator
    # regression: «Felix CapitalG» didn't canonicalize because
    # single-word capture split it into «Felix» (5 chars) and
    # «CapitalG» (8) — neither alone fuzzy-matches «felix
    # capital» (13).
    seen_tokens: set[str] = set()
    token_re = re.compile(
        r"[A-ZА-ЯЁ][\wА-Яа-яёЁ/\-\.]{2,24}"
        r"(?:\s+[A-ZА-ЯЁ][\wА-Яа-яёЁ/\-\.]+){0,2}"
    )
    for m in token_re.finditer(text):
        token = m.group(0)
        if token.lower() in existing_lc:
            continue
        if token.lower() in seen_tokens:
            continue
        seen_tokens.add(token.lower())
        # FR-CR-05-129 follow-up — composite tokens like
        # «Jamal/Jabal» or «Boutert/Bauerdart» (the LLM
        # combined two phonetic variants with `/`). Split on
        # `/` `-` separators and fuzzy-match EACH piece.
        # Replace the whole composite with the best match.
        candidates = [token]
        if "/" in token or "-" in token:
            candidates.extend(
                p for p in re.split(r"[/\-]+", token) if len(p) >= 4
            )
        best_cp: "Counterparty" | None = None
        best_ratio = 0.0
        for piece in candidates:
            piece_fold = _fold(piece)
            if not piece_fold or len(piece_fold) < 4:
                continue
            for fold_name, cp in by_fold.items():
                if abs(len(fold_name) - len(piece_fold)) > 3:
                    continue
                r = difflib.SequenceMatcher(
                    None, fold_name, piece_fold
                ).ratio()
                if r > best_ratio:
                    best_ratio = r
                    best_cp = cp
        if best_cp is not None and best_ratio >= ratio_threshold:
            # Map the FULL surface form (incl. composite) to
            # canonical so canonicalize_text rewrites in one shot.
            if token not in out:
                out[token] = best_cp.name
    return out


def canonicalize_text(
    text: str | None,
    mention_to_canonical: dict[str, str],
) -> str | None:
    """FR-CR-05-129 — replace each Pass-1 mention with its
    canonical directory name in `text`, longest-mention-first
    so a longer surface form («Bauer/Dart») isn't partially
    eaten by a shorter one («Bauer»). Case-insensitive replace
    that preserves the canonical name's casing as written in
    the directory.

    FR-CR-05-129 follow-up — when the mention is a PREFIX of
    the canonical name («Insight» mention, «Insight Partners»
    canonical), avoid the cascade «Insight Partners» →
    «Insight Partners Partners» by adding a negative-lookahead
    against the canonical's tail. The mention is replaced only
    when NOT already adjacent to the canonical's remaining
    tokens.
    """
    if not text or not mention_to_canonical:
        return text
    import re

    # Sort by length DESC so longer surface forms replace first.
    items = sorted(
        mention_to_canonical.items(), key=lambda x: -len(x[0])
    )
    out = text
    for mention, canonical in items:
        if not mention or not canonical:
            continue
        if mention == canonical:
            continue
        # FR-CR-05-129 follow-up — skip the replace when the
        # CANONICAL is already in the text right where we'd
        # substitute (avoids «Insight Partners Partners»).
        # Build the lookahead from the canonical's tail.
        if canonical.lower().startswith(mention.lower() + " "):
            tail = canonical[len(mention):]
            tail_pattern = re.escape(tail)
            try:
                pattern = re.compile(
                    r"(?<!\w)" + re.escape(mention)
                    + r"(?!\w)(?!" + tail_pattern + r")",
                    flags=re.IGNORECASE,
                )
                out = pattern.sub(canonical, out)
            except re.error:
                continue
            continue
        try:
            pattern = re.compile(
                r"(?<!\w)" + re.escape(mention) + r"(?!\w)",
                flags=re.IGNORECASE,
            )
            out = pattern.sub(canonical, out)
        except re.error:
            continue
    return out


__all__ = [
    "COUNTERPARTY_MATCH_SYSTEM",
    "COUNTERPARTY_MATCH_TOOL_NAME",
    "COUNTERPARTY_MATCH_TOOL_DESCRIPTION",
    "COUNTERPARTY_MATCH_TOOL_PARAMETERS",
    "COUNTERPARTY_EXTRACT_SYSTEM",
    "COUNTERPARTY_RESOLVE_SYSTEM",
    "CANONICALIZE_TASKS_SYSTEM",
    "CONSOLIDATE_TASKS_SYSTEM",
    "match_counterparties_in_transcript",
    "extract_counterparty_mentions",
    "resolve_mentions_to_directory",
    "canonicalize_task_content_via_llm",
    "consolidate_tasks_via_llm",
    "canonicalize_text",
    "fuzzy_extend_canonical_map",
]
