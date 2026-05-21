"""Speech-to-text for Slack audio attachments.

Slack voice messages and audio file uploads arrive on a regular
`message` event with a `files` array. This module downloads each
audio file (using the bot token) and transcribes it via OpenAI's
Whisper endpoint. The rest of the pipeline treats the transcript as
just another message text.
"""
from __future__ import annotations

from typing import Any, Iterable

import httpx

from app.logging_setup import get_logger

log = get_logger(__name__)

# Slack's voice-note mimetypes we support. Whisper accepts a fairly
# wide set of formats; we filter by audio/* on the Slack side so we
# don't try to transcribe images or PDFs.
_AUDIO_MIMETYPE_PREFIX = "audio/"
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Whisper's per-request cap.

# FR-CR-05-164 — operator's own brand names that MUST be biased to
# Whisper regardless of whether they're in the Sheets-managed
# counterparties table. The counterparties sync is wipe-and-replace
# from Google Sheets, so a missing/misspelled row would silently
# regress transcripts (operator-pinned: «Humanoid» kept landing as
# «Gamanoid» / «Гуманоид»). Add entries here only with operator
# confirmation — every name burns Whisper prompt-token budget.
_ALWAYS_INCLUDE_BRANDS: tuple[str, ...] = (
    "Humanoid",
)


def is_audio_file(file_info: dict[str, Any]) -> bool:
    mimetype = (file_info.get("mimetype") or "").lower()
    return mimetype.startswith(_AUDIO_MIMETYPE_PREFIX)


def extract_audio_files(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only the audio entries from event['files'], preserving order."""
    files = event.get("files") or []
    return [f for f in files if isinstance(f, dict) and is_audio_file(f)]


def download_slack_file(*, url: str, bot_token: str) -> bytes | None:
    """Fetch a Slack private file via the bot's token. Returns None on
    failure so callers can degrade gracefully (skip this file but keep
    processing the rest of the event)."""
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=20.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("slack_file_download_failed", url=url, error=str(e))
        return None
    if len(resp.content) > _MAX_AUDIO_BYTES:
        log.warning(
            "slack_audio_too_large",
            url=url,
            bytes=len(resp.content),
            cap=_MAX_AUDIO_BYTES,
        )
        return None
    return resp.content


def transcribe_bytes(
    *,
    audio_bytes: bytes,
    mimetype: str,
    filename: str,
    openai_api_key: str,
    model: str = "whisper-1",
    prompt: str | None = None,
    language: str | None = None,
) -> str | None:
    """Send audio to OpenAI Whisper; returns the transcript or None.

    `prompt` is Whisper's optional context-biasing string (≤224
    tokens, hard-truncated server-side). Used by the meeting
    pipelines (FR-CR-05-127) to push proper-noun spellings the
    model otherwise mangles — e.g. «Tether» → «teaser»,
    «Schaeffler» → «шафлера», team member real names. Empty /
    None skips the parameter entirely so the Slack voice-message
    path stays unchanged.

    `language` — optional ISO-639-1 code (`"en"`, `"ru"`, …). When
    set, Whisper biases decoding toward that language instead of
    auto-detecting per segment. Used by FR-CB2-3.39 bilingual
    restoration to force an English-language pass over a recording
    that auto-detect rendered as garbled Russian.
    """
    if not openai_api_key or not audio_bytes:
        return None
    # FR-CR-05-177 — OpenAI's diarization-capable models
    # (e.g. `gpt-4o-transcribe-diarize`) have two API differences
    # vs `whisper-1`:
    #   1. They REJECT `prompt` with
    #      «Prompt is not supported for diarization models».
    #   2. They REQUIRE a `chunking_strategy` kwarg —
    #      «chunking_strategy is required for diarization models».
    #      `"auto"` lets the model pick a reasonable internal split.
    # Both shifts surfaced 2026-05-21 when OpenAI promoted the
    # diarize endpoint out of preview; we already chunk audio
    # client-side to fit the 25 MB upload cap, the chunking strategy
    # here governs the model's INTERNAL diarization windowing.
    is_diarization_model = "diarize" in (model or "").lower()
    try:
        from openai import OpenAI

        # FR-CR-05-177 — 300 s read timeout: diarize on a 20-min
        # chunk takes ~60-90 s; without an explicit timeout the
        # client default (10 min) hides hangs and burns operator
        # wall-clock. Per-chunk retries left to the caller's
        # pool wrapper, not here.
        client = OpenAI(api_key=openai_api_key, timeout=300.0)
        kwargs: dict[str, Any] = {
            "model": model,
            "file": (filename, audio_bytes, mimetype),
        }
        if prompt and not is_diarization_model:
            kwargs["prompt"] = prompt
        if language:
            kwargs["language"] = language
        if is_diarization_model:
            kwargs["chunking_strategy"] = "auto"
        resp = client.audio.transcriptions.create(**kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("whisper_call_failed", error=str(e), filename=filename)
        return None
    text = getattr(resp, "text", None)
    if not text and isinstance(resp, dict):
        text = resp.get("text")
    if not isinstance(text, str):
        return None
    return text.strip() or None


# FR-CR-05-148 — phrases Whisper LOOPS on when audio is silent /
# quiet / has long pauses. Russian dubbed-content «titles» it
# saw in training data. List grows as we hit new ones in prod.
# FR-CR-05-153 — added URL/social patterns from operator's
# Fundraising daily 05/05 regression: «Университет youtube
# Университет https://vk.com.ua» loop.
WHISPER_HALLUCINATION_MARKERS: tuple[str, ...] = (
    "Редактор субтитров",
    "Корректор",
    "Субтитры от",
    "Субтитры подготовлены",
    "Субтитры сделал",
    "Субтитры от",
    "Спасибо за просмотр",
    "Подписывайтесь",
    "Subscribe to my channel",
    "Subtitles by",
    "Edited by",
    "Translated by",
    # FR-CR-05-153 web-URL / social-domain leakage:
    "youtube",
    "https://vk.",
    "vk.com",
    "instagram.com",
    "facebook.com",
    ".com.ua",
)


def is_summary_no_content(text: str) -> tuple[bool, str | None]:
    """FR-CR-05-157 follow-up — operator-pinned: «если там мало
    текста то не выводим вообще». Detect when the LLM's short
    summary basically says «I couldn't extract anything from
    this transcript» — those posts are noise in Slack/TG and
    should be suppressed.

    Returns (True, matched_phrase) on hit, else (False, None).

    Triggers when ANY of these phrases appears in the body
    (case-insensitive, NFKD-folded). The phrases are taken
    from real LLM output that operator complained about:

      - «содержательная часть … (не зафиксирован|отсутствует)»
      - «содержательный транскрипт отсутствует»
      - «восстановить … (невозможно|не удалось)»
      - «доступны только служебные пометки»
      - «единственный надёжный вывод: для саммари нужна
         корректная расшифровка»
      - «по доступным данным можно подтвердить только факт»
    """
    if not text:
        return False, None
    haystack = text.lower()
    patterns = (
        "содержательная часть",
        "содержательный транскрипт отсутствует",
        "восстановить … невозможно",
        "восстановить … не удалось",
        "доступны только служебные пометки",
        "единственный надёжный вывод",
        "для саммари нужна корректная расшифровка",
        "можно подтвердить только факт",
        "конкретика этих обновлений не раскрыта",
        "решений, договорённостей",
    )
    for p in patterns:
        # Allow `…` as wildcard between two halves.
        if "…" in p:
            left, right = p.split("…", 1)
            li = haystack.find(left.strip())
            if li == -1:
                continue
            ri = haystack.find(right.strip(), li + len(left.strip()))
            if ri != -1 and ri - li - len(left.strip()) < 80:
                return True, p
            continue
        if p in haystack:
            return True, p
    return False, None


def is_transcript_unsummarizable(text: str) -> tuple[bool, str | None]:
    """FR-CR-05-157 — operator-pinned: «нужны только новые загружать
    и там где транскрипт нормальный». Identify recordings whose
    transcript is too thin / too garbage to summarize, so the
    pipeline can short-circuit BEFORE the detailed_summary LLM
    call, doc export, Slack post, TG cards.

    Returns (True, human-readable-reason) when one of:
      - empty / whitespace-only
      - <800 chars  (≈< 2-3 min of speech)
      - dominated by Whisper subtitle hallucination (≥2 marker hits
        in head 2KB) AND no real content
    Else (False, None).

    Threshold deliberately lower than `looks_like_whisper_hallucination`
    because we want to catch SHORT garbage (which the looser
    detector skips with «empty meeting» exemption).
    """
    if not text or not text.strip():
        return True, "transcript empty"
    n = len(text.strip())
    if n < 800:
        return True, f"transcript too short ({n} chars)"
    head = text[:2000].lower()
    sub_markers_lc = (
        "редактор субтитров",
        "корректор",
        "субтитры от",
        "субтитры подготовлены",
        "субтитры сделал",
        "subtitles by",
        "edited by",
        "translated by",
    )
    sub_hits = sum(head.count(m) for m in sub_markers_lc)
    if sub_hits >= 2:
        return True, f"transcript dominated by subtitle credits ({sub_hits} hits)"
    return False, None


def looks_like_whisper_hallucination(
    text: str,
    *,
    expected_min_chars: int = 500,
) -> bool:
    """FR-CR-05-148 — detect Whisper's silent-audio hallucination
    where it loops repeating Russian-dubbed-content subtitle
    credits («Редактор субтитров А.Семкин Корректор А.Егорова»
    etc.) instead of the actual speech.

    Three signals:
      1. Multiple `WHISPER_HALLUCINATION_MARKERS` matches in the
         first 1000 chars (≥3 hits).
      2. Very low unique-word ratio over the whole text (<8%
         when there are 100+ words). A 30-min recording that
         loops the same 4-word phrase fits this. (FR-CR-05-153
         lowered threshold from 5% to 8% — operator's «youtube
         vk.com.ua» loop had ~10 unique words ≈ 10%.)
      3. FR-CR-05-153 — bigram-loop signal: any 2-word phrase
         repeats ≥20 times. Catches cases where unique-ratio
         is OK due to a varied head but the body is pure loop
         («Университет youtube Университет youtube …»).

    Returns False on short / empty input — those are «empty
    meeting» cases, not hallucinations.
    """
    if not text or len(text) < expected_min_chars:
        return False
    head = text[:1000]
    marker_hits = sum(head.count(m) for m in WHISPER_HALLUCINATION_MARKERS)
    if marker_hits >= 3:
        return True
    import re as _re

    words = _re.findall(r"\w+", text.lower())
    if len(words) < 100:
        return False
    unique = len(set(words))
    if unique / len(words) < 0.08:
        return True
    # FR-CR-05-153 — bigram loop. If any 2-word phrase repeats
    # ≥20 times, it's a loop hallucination. Defense for the
    # «Университет youtube Университет youtube …» pattern where
    # the first chars are varied but the bulk is repetitive.
    if len(words) >= 50:
        from collections import Counter as _Counter

        bigrams = list(zip(words, words[1:]))
        if bigrams:
            _phrase, _count = _Counter(bigrams).most_common(1)[0]
            if _count >= 20:
                return True
    return False


def transcribe_chunks_parallel(
    paths: list[str],
    *,
    openai_api_key: str,
    model: str = "whisper-1",
    prompt: str | None = None,
    mimetype_for: callable | None = None,
    max_workers: int = 3,
    language: str | None = None,
) -> list[str | None]:
    """FR-CR-05-146a — transcribe a list of audio-chunk paths
    in PARALLEL via a thread-pool, preserving order. Returns a
    list of length `len(paths)` with the transcript text per
    chunk (or `None` when that chunk failed). Each thread calls
    `transcribe_bytes` independently.

    `mimetype_for(path)` (optional callable) returns the mimetype
    for a given chunk path; defaults to `audio/mpeg`. Used by
    the Zoom path that sometimes hands us .m4a / .mp4 chunks
    (different mimetypes per file).

    `max_workers` is the concurrency cap — default 3 keeps us
    well under OpenAI's 50 req/min Whisper limit even with the
    surrounding LLM calls; bump it via the kwarg if needed.

    Operator-pinned: «Whisper-чанки параллельно (сейчас 3
    подряд) → -2.5 мин. asyncio.gather поверх transcribe_bytes
    для каждого chunk».
    """
    import os
    from concurrent.futures import ThreadPoolExecutor

    if not paths:
        return []
    if not openai_api_key:
        return [None] * len(paths)
    if mimetype_for is None:
        mimetype_for = lambda p: "audio/mpeg"  # noqa: E731

    def _transcribe_one(path: str) -> str | None:
        try:
            with open(path, "rb") as f:
                audio_bytes = f.read()
        except OSError as e:
            log.warning(
                "whisper_chunk_read_failed",
                path=path, error=str(e),
            )
            return None
        return transcribe_bytes(
            audio_bytes=audio_bytes,
            mimetype=mimetype_for(path),
            filename=os.path.basename(path),
            openai_api_key=openai_api_key,
            model=model,
            prompt=prompt,
            language=language,
        )

    workers = max(1, min(max_workers, len(paths)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # `pool.map` preserves input order — we get results in
        # the same sequence as `paths`, so the joined transcript
        # comes out chronologically.
        results = list(pool.map(_transcribe_one, paths))
    return results


def transcribe_audio_files(
    files: Iterable[dict[str, Any]],
    *,
    bot_token: str,
    openai_api_key: str,
) -> list[str]:
    """Download and transcribe every audio file in `files`. Returns a
    list of non-empty transcripts in the same order; files that failed
    to download or transcribe are skipped silently."""
    transcripts: list[str] = []
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        audio = download_slack_file(url=url, bot_token=bot_token)
        if audio is None:
            continue
        text = transcribe_bytes(
            audio_bytes=audio,
            mimetype=(f.get("mimetype") or "audio/webm"),
            filename=(f.get("name") or "audio.webm"),
            openai_api_key=openai_api_key,
        )
        if text:
            transcripts.append(text)
    return transcripts


def build_whisper_bias_prompt(
    session: Any,
    *,
    meeting_title: str | None = None,
    participants: list[str] | None = None,
    max_chars: int = 800,
) -> str | None:
    """FR-CR-05-127 — pack a Whisper `prompt` string out of the
    operator's canonical name registries so brand names don't
    mutate in transcription («Tether» → «teaser», «Schaeffler»
    → «шафлера», «Goldman Sachs» → «Голдман Сакс»).

    Whisper's `prompt` parameter accepts up to ~224 tokens; we
    cap at `max_chars` (≈600-1000 chars works empirically) and
    pack in priority order:
      0. Always-include brand names (FR-CR-05-164) — operator-pinned
         core names that MUST always be biased, even if the operator
         hasn't added them to Sheets yet (e.g. own product brand).
      1. Meeting title (if provided) — high signal for the topic.
      2. Participants (if provided) — names the speakers say
         constantly.
      3. Team member real names from `team_members` (active rows).
      4. Counterparty canonical names from `counterparties`.

    Returns None when no source material is available (empty DB
    + no meeting metadata) — caller should pass nothing to
    Whisper rather than an empty string.
    """
    pieces: list[str] = []
    seen: set[str] = set()

    def _push(s: str | None) -> None:
        if not s:
            return
        s = s.strip()
        if not s:
            return
        key = s.lower()
        if key in seen:
            return
        seen.add(key)
        pieces.append(s)

    # FR-CR-05-164 — own product brand «Humanoid» reliably mutated to
    # «Gamanoid» / «Гуманоид» in transcripts because nothing in the
    # operator's Sheets row matched. Hardcode here so it survives
    # the counterparties wipe-and-replace sync. Add more entries
    # only after operator confirmation — every name burns Whisper
    # token budget.
    for brand in _ALWAYS_INCLUDE_BRANDS:
        _push(brand)

    if meeting_title:
        _push(meeting_title)
    for p in participants or []:
        _push(p)

    try:
        from app.models import Counterparty, TeamMember
    except Exception:  # pragma: no cover — model import shouldn't fail in prod
        Counterparty = None  # type: ignore[assignment]
        TeamMember = None  # type: ignore[assignment]

    if TeamMember is not None and session is not None:
        try:
            for tm in session.query(TeamMember).filter(
                TeamMember.active.is_(True)
            ).all():
                _push(getattr(tm, "real_name", None))
        except Exception as e:  # noqa: BLE001
            log.info("whisper_bias_team_query_failed", error=str(e))

    if Counterparty is not None and session is not None:
        try:
            for cp in session.query(Counterparty).all():
                _push(getattr(cp, "name", None))
        except Exception as e:  # noqa: BLE001
            log.info("whisper_bias_counterparty_query_failed", error=str(e))

    if not pieces:
        return None
    # Whisper biases on a free-text «previous-segment» blob.
    # Comma-separated list packs more proper nouns per token
    # than full sentences and Whisper still benefits from it.
    out: list[str] = []
    used = 0
    for s in pieces:
        cost = len(s) + 2  # «, »
        if used + cost > max_chars:
            break
        out.append(s)
        used += cost
    if not out:
        return None
    return ", ".join(out)


def merge_transcripts_into_text(original_text: str, transcripts: list[str]) -> str:
    """Produce the source_text the intent pipeline will see. Keeps any
    user-typed caption and appends the transcripts, joined with a
    newline. If the original text is empty, the first transcript becomes
    the main text."""
    pieces: list[str] = []
    if original_text and original_text.strip():
        pieces.append(original_text.strip())
    pieces.extend(t for t in transcripts if t)
    return "\n".join(pieces).strip()


__all__ = [
    "is_audio_file",
    "extract_audio_files",
    "download_slack_file",
    "transcribe_bytes",
    "transcribe_audio_files",
    "build_whisper_bias_prompt",
    "merge_transcripts_into_text",
]
