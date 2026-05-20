"""FR-CB2-3.39 — bilingual transcript restoration (OpenAI-based).

When a Zoom meeting is partly Russian / partly English (typical
operator pattern: investor calls), the primary Whisper pass tends to
garble the English segments — names mutate, technical terms drift,
some lines turn into Russian-sounding nonsense. Operator-pinned:

    «отдельный вызов ллм скажет нужно ли сделать ещё транскрипт на
    английском, если нужно — отдельный вызов STT делает на английском,
    а далее берём оба текста и прогоняем через LLM чтобы восстановить
    окончательный смысл // в нужных местах нужный язык».

This module isolates that three-step flow:

  1. ``should_re_stt_english(transcript, openai_client, model)`` — a
     fast yes/no call to a small OpenAI model.
  2. ``re_stt_english(...)`` — POST to the operator-configured STT
     endpoint with ``lang=en``. Returns the raw text or ``None`` on
     failure / when no URL is configured.
  3. ``merge_transcripts(primary, secondary, openai_client, model)`` —
     larger OpenAI call that emits the reconciled transcript.

``restore_transcript_bilingual()`` glues them together and returns
``(final_text, trace)`` where ``trace`` is a structured dict for the
DB / Slack debug payload. On ANY exception the function returns the
original transcript unchanged — the feature must never break the
main pipeline (FR-CB2-3.39).
"""
from __future__ import annotations

from typing import Any

import httpx

from app.logging_setup import get_logger

log = get_logger(__name__)


_DETECTOR_SYSTEM = (
    "You are a precision classifier. Decide whether the given "
    "transcript would meaningfully benefit from being re-transcribed "
    "in English and merged back together. Answer YES only when the "
    "transcript contains noticeable English-language content that "
    "looks garbled, partially transliterated, or rendered as "
    "Russian-sounding nonsense words (typical Whisper failure mode "
    "on bilingual calls). Answer NO when the transcript is cleanly "
    "in a single language, or when the English portion is already "
    "intelligible. Reply with a single token: YES or NO."
)


_RECONCILER_SYSTEM = (
    "You receive two transcripts of the SAME meeting produced by two "
    "different speech-to-text passes — PRIMARY (any language, often "
    "Russian-biased) and SECONDARY (English-biased). Your job is to "
    "emit ONE final transcript that uses the correct language for "
    "each segment: keep Russian segments verbatim from PRIMARY, "
    "replace garbled English segments with the corresponding lines "
    "from SECONDARY. Preserve speaker order, timecodes (if any), "
    "and the natural flow of the meeting. Do NOT translate — pick "
    "the better-quality version per segment. Do NOT add commentary."
)


def should_re_stt_english(
    *,
    transcript: str,
    openai_client: Any,
    model: str = "gpt-4o-mini",
    max_sample_chars: int = 6000,
) -> bool:
    """Detector step. Returns True when the transcript should be
    re-transcribed in English, False otherwise (including on any
    error — fail-closed so we don't burn cost or call a possibly
    missing STT endpoint).
    """
    if not transcript or not transcript.strip():
        return False
    sample = transcript[:max_sample_chars]
    try:
        resp = openai_client.chat.completions.create(
            model=model,
            max_tokens=4,
            temperature=0,
            messages=[
                {"role": "system", "content": _DETECTOR_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "Transcript sample:\n\n"
                        f"<<<\n{sample}\n>>>\n\n"
                        "YES or NO?"
                    ),
                },
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.info(
            "ceo_brain_bilingual_detector_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return False
    try:
        text = (resp.choices[0].message.content or "").strip().upper()
    except Exception as e:  # noqa: BLE001
        log.info(
            "ceo_brain_bilingual_detector_parse_failed",
            error=str(e),
        )
        return False
    decision = text.startswith("YES")
    log.info(
        "ceo_brain_bilingual_detector_decided",
        decision=decision, raw=text[:32],
    )
    return decision


def re_stt_english(
    *,
    stt_url: str,
    meeting_id: str | None = None,
    audio_url: str | None = None,
    timeout: float = 90.0,
) -> str | None:
    """Second-STT step. POSTs to the operator-configured endpoint
    with payload ``{meeting_id, audio_url, lang}`` and reads
    ``text`` from the JSON response. Returns ``None`` on any error
    or when neither identifier is set.

    The endpoint contract is intentionally minimal — operator can
    wire it to any STT provider (re-call Whisper with a different
    `language=en` hint, OpenAI gpt-4o-transcribe, Deepgram, etc.).
    """
    if not stt_url:
        return None
    if not meeting_id and not audio_url:
        log.info("ceo_brain_bilingual_re_stt_skipped_no_id")
        return None
    payload: dict[str, Any] = {"lang": "en"}
    if meeting_id:
        payload["meeting_id"] = meeting_id
    if audio_url:
        payload["audio_url"] = audio_url
    try:
        resp = httpx.post(stt_url, json=payload, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_bilingual_re_stt_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return None
    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_bilingual_re_stt_parse_failed", error=str(e),
        )
        return None
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()


def merge_transcripts(
    *,
    primary: str,
    secondary: str,
    openai_client: Any,
    model: str = "gpt-4o",
    max_input_chars: int = 60_000,
    max_tokens: int = 4096,
) -> str | None:
    """Reconciler step. Asks the OpenAI model to emit a single
    final transcript combining the two passes. Returns ``None``
    on any error so the caller can fall back to the primary.
    """
    if not primary or not primary.strip():
        return None
    if not secondary or not secondary.strip():
        return None
    p = primary[:max_input_chars]
    s = secondary[:max_input_chars]
    try:
        resp = openai_client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            messages=[
                {"role": "system", "content": _RECONCILER_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "PRIMARY (Russian-biased pass):\n"
                        f"<<<\n{p}\n>>>\n\n"
                        "SECONDARY (English-biased pass):\n"
                        f"<<<\n{s}\n>>>\n\n"
                        "Emit the reconciled transcript now."
                    ),
                },
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_bilingual_reconciler_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return None
    try:
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_bilingual_reconciler_parse_failed", error=str(e),
        )
        return None
    return text or None


def restore_transcript_bilingual(
    *,
    transcript: str,
    meeting_id: str | None = None,
    audio_url: str | None = None,
    openai_client: Any | None = None,
    stt_url: str = "",
    detector_model: str = "gpt-4o-mini",
    reconciler_model: str = "gpt-4o",
) -> tuple[str, dict[str, Any]]:
    """End-to-end orchestrator. Returns ``(final_text, trace)``.

    ``trace`` captures each decision so the responder can persist
    it to ``claude_responder_runs.request_payload['_bilingual']``
    for diagnostics. Possible ``stage`` values:

      * ``detector_skipped_empty`` — no transcript content.
      * ``detector_no_client`` — OpenAI client not provided.
      * ``detector_said_no`` — LLM voted to skip.
      * ``re_stt_no_url`` — flag on, detector said yes, but no STT
        endpoint configured — falls back to original.
      * ``re_stt_failed`` — STT endpoint returned nothing.
      * ``reconciler_failed`` — merge call errored.
      * ``restored`` — happy path; ``final_text`` differs from
        ``transcript``.

    No exception ever escapes — wrapped in a final try/except so
    the main pipeline keeps moving on any unexpected error.
    """
    trace: dict[str, Any] = {
        "primary_chars": len(transcript or ""),
        "stage": "",
        "decision": None,
        "secondary_chars": 0,
        "final_chars": len(transcript or ""),
    }
    try:
        if not transcript or not transcript.strip():
            trace["stage"] = "detector_skipped_empty"
            return transcript, trace
        if openai_client is None:
            trace["stage"] = "detector_no_client"
            return transcript, trace
        decision = should_re_stt_english(
            transcript=transcript,
            openai_client=openai_client,
            model=detector_model,
        )
        trace["decision"] = decision
        if not decision:
            trace["stage"] = "detector_said_no"
            return transcript, trace
        if not stt_url:
            trace["stage"] = "re_stt_no_url"
            return transcript, trace
        secondary = re_stt_english(
            stt_url=stt_url,
            meeting_id=meeting_id,
            audio_url=audio_url,
        )
        if not secondary:
            trace["stage"] = "re_stt_failed"
            return transcript, trace
        trace["secondary_chars"] = len(secondary)
        merged = merge_transcripts(
            primary=transcript,
            secondary=secondary,
            openai_client=openai_client,
            model=reconciler_model,
        )
        if not merged:
            trace["stage"] = "reconciler_failed"
            return transcript, trace
        trace["stage"] = "restored"
        trace["final_chars"] = len(merged)
        return merged, trace
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_bilingual_restore_unexpected_error",
            error=str(e), error_type=type(e).__name__,
        )
        trace["stage"] = trace["stage"] or "unexpected_error"
        return transcript, trace


__all__ = [
    "should_re_stt_english",
    "re_stt_english",
    "merge_transcripts",
    "restore_transcript_bilingual",
]
