"""FR-CB2-3.39 — bilingual transcript restoration (OpenAI-based).

When a Zoom meeting is partly Russian / partly English (typical
operator pattern: investor calls), the primary Whisper pass tends to
garble the English segments — names mutate, technical terms drift,
some lines turn into Russian-sounding nonsense. Operator-pinned:

    «отдельный вызов ллм скажет нужно ли сделать ещё транскрипт на
    английском, если нужно — отдельный вызов STT делает на английском,
    а далее берём оба текста и прогоняем через LLM чтобы восстановить
    окончательный смысл // в нужных местах нужный язык // тот же STT
    что брал, но язык англ».

This module isolates that three-step flow:

  1. ``should_re_stt_english(transcript, openai_client, model)`` — a
     fast yes/no call to a small OpenAI model.
  2. ``re_stt_english_via_whisper(...)`` — re-transcribe the SAME
     `audio_path` that produced the primary transcript, but pass
     `language="en"` to the same Whisper endpoint we already use
     (`app.services.transcription.transcribe_bytes`). Returns the
     raw text or ``None`` on failure / when audio is missing.
  3. ``merge_transcripts(primary, secondary, openai_client, model)`` —
     larger OpenAI call that emits the reconciled transcript.

``restore_transcript_bilingual()`` glues them together and returns
``(final_text, trace)`` where ``trace`` is a structured dict for the
DB / Slack debug payload. On ANY exception the function returns the
original transcript unchanged — the feature must never break the
main pipeline (FR-CB2-3.39).
"""
from __future__ import annotations

import os
from typing import Any

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


def merge_transcripts_chunked(
    *,
    primary: str,
    secondary: str,
    openai_client: Any,
    model: str = "gpt-4o",
    batch_input_chars: int = 30_000,
    max_tokens_per_batch: int = 16_384,
) -> str | None:
    """Chunked reconciler — always returns a reconciled output that
    covers the ENTIRE primary transcript regardless of length.
    Operator-pinned: «мне надо весь текст выдать транскрипта всегда
    чтобы выдавал, то есть если он не вмещается, надо несколько
    батчей и чтобы всегда так работало».

    Strategy:
      1. Decide how many batches we need from the LARGER of the two
         transcripts so each batch's input is ≤ ``batch_input_chars``.
      2. Cut both transcripts at proportional offsets, snapping to
         nearby newline / sentence-end boundaries via
         ``_find_cut_point`` so cuts don't strand mid-sentence.
      3. Reconcile each (primary_i, secondary_i) pair via
         ``merge_transcripts``; concatenate outputs.

    If both fit comfortably in one batch — collapses to a single
    call (no overhead). Returns ``None`` only when ANY batch
    fails — partial results are not returned (the caller falls
    back to the primary, which is at least complete).
    """
    if not primary or not primary.strip():
        return None
    if not secondary or not secondary.strip():
        return None
    longer = max(len(primary), len(secondary))
    n_batches = max(1, -(-longer // batch_input_chars))  # ceil division
    if n_batches == 1:
        return merge_transcripts(
            primary=primary,
            secondary=secondary,
            openai_client=openai_client,
            model=model,
            max_input_chars=batch_input_chars,
            max_tokens=max_tokens_per_batch,
        )

    def _split(text: str, n: int) -> list[str]:
        cuts = [0]
        for i in range(1, n):
            ideal = i * len(text) // n
            cuts.append(_find_cut_point(text, ideal))
        cuts.append(len(text))
        # Dedupe accidental zero-width slices (text shorter than n).
        cuts = sorted(set(cuts))
        return [text[cuts[i]:cuts[i + 1]] for i in range(len(cuts) - 1)]

    p_chunks = _split(primary, n_batches)
    s_chunks = _split(secondary, n_batches)
    # Align lengths — pad the shorter list with empty strings so
    # we can zip cleanly.
    while len(p_chunks) < n_batches:
        p_chunks.append("")
    while len(s_chunks) < n_batches:
        s_chunks.append("")

    log.info(
        "ceo_brain_bilingual_reconcile_chunked_start",
        n_batches=n_batches,
        primary_chars=len(primary),
        secondary_chars=len(secondary),
        batch_input_chars=batch_input_chars,
    )
    merged_parts: list[str] = []
    for i, (p, s) in enumerate(zip(p_chunks, s_chunks), start=1):
        if not p.strip() and not s.strip():
            continue
        # If one side is empty for this slice, hand the other through
        # untouched — nothing to reconcile against.
        if not s.strip():
            merged_parts.append(p)
            continue
        if not p.strip():
            merged_parts.append(s)
            continue
        out = merge_transcripts(
            primary=p,
            secondary=s,
            openai_client=openai_client,
            model=model,
            max_input_chars=batch_input_chars,
            max_tokens=max_tokens_per_batch,
        )
        if not out:
            log.warning(
                "ceo_brain_bilingual_reconcile_chunked_batch_failed",
                batch=i, of=n_batches,
            )
            return None
        merged_parts.append(out)
        log.info(
            "ceo_brain_bilingual_reconcile_chunked_batch_ok",
            batch=i, of=n_batches,
            in_primary_chars=len(p),
            in_secondary_chars=len(s),
            out_chars=len(out),
        )

    return "\n\n".join(merged_parts) if merged_parts else None


def _load_audio_path_for_zoom_id(zoom_id: str) -> str | None:
    """Look up the on-disk audio path for a Zoom recording. Returns
    ``None`` when the recording row is missing, the audio file
    isn't on disk, or any DB-side error happens."""
    if not zoom_id:
        return None
    try:
        from app.db import session_scope  # type: ignore
        from app.models import ZoomRecording  # type: ignore

        with session_scope() as session:
            row = (
                session.query(ZoomRecording)
                .filter(ZoomRecording.zoom_id == zoom_id)
                .one_or_none()
            )
            if row is None:
                return None
            path = (row.audio_path or "").strip()
            if not path:
                return None
            if not os.path.exists(path):
                log.info(
                    "ceo_brain_bilingual_audio_missing_on_disk",
                    zoom_id=zoom_id, audio_path=path,
                )
                return None
            return path
    except Exception as e:  # noqa: BLE001
        log.info(
            "ceo_brain_bilingual_audio_lookup_failed",
            zoom_id=zoom_id, error=str(e),
        )
        return None


def _load_audio_url_for_zoom_id(zoom_id: str) -> str | None:
    """Look up the Zoom cloud download URL for a recording (filled
    when the recording was first ingested — survives local-disk
    audio cleanup). Returns ``None`` on lookup error."""
    if not zoom_id:
        return None
    try:
        from app.db import session_scope  # type: ignore
        from app.models import ZoomRecording  # type: ignore

        with session_scope() as session:
            row = (
                session.query(ZoomRecording)
                .filter(ZoomRecording.zoom_id == zoom_id)
                .one_or_none()
            )
            if row is None:
                return None
            url = (row.audio_url or "").strip()
            return url or None
    except Exception as e:  # noqa: BLE001
        log.info(
            "ceo_brain_bilingual_audio_url_lookup_failed",
            zoom_id=zoom_id, error=str(e),
        )
        return None


def _download_zoom_audio_to_temp(audio_url: str) -> str | None:
    """Pull the recording bytes from Zoom cloud via the existing
    `ZoomClient.download_audio` (needs OAuth + bearer token, both
    already configured in settings). Returns a temp-file path the
    caller must delete, or ``None`` on failure.

    Cap matches the production `ZOOM_AUDIO_MAX_BYTES` so we don't
    accidentally pull a gigabyte file on a misconfigured row.
    """
    if not audio_url:
        return None
    try:
        import tempfile

        from app.config import get_settings  # type: ignore
        from app.zoom.client import ZoomClient  # type: ignore

        s = get_settings()
        if not (s.zoom_client_id and s.zoom_client_secret
                and s.zoom_account_id):
            log.info("ceo_brain_bilingual_zoom_oauth_missing")
            return None
        client = ZoomClient(
            account_id=s.zoom_account_id,
            client_id=s.zoom_client_id,
            client_secret=s.zoom_client_secret,
        )
        tmp = tempfile.NamedTemporaryFile(
            prefix="bilingual_", suffix=".m4a", delete=False,
        )
        tmp.close()
        written = client.download_audio(
            url=audio_url,
            dest_path=tmp.name,
            max_bytes=getattr(s, "zoom_audio_max_bytes", 1024 * 1024 * 1024),
        )
        if not written:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            return None
        log.info(
            "ceo_brain_bilingual_audio_downloaded",
            tmp_path=tmp.name, bytes=written,
        )
        return tmp.name
    except Exception as e:  # noqa: BLE001
        log.warning(
            "ceo_brain_bilingual_audio_download_failed",
            error=str(e), error_type=type(e).__name__,
        )
        return None


def re_stt_english_via_whisper(
    *,
    zoom_id: str | None = None,
    audio_path: str | None = None,
    openai_api_key: str,
    model: str = "whisper-1",
    whisper_prompt: str | None = None,
) -> str | None:
    """Second-STT pass. Re-transcribes the SAME audio file used for
    the primary transcript, but passes ``language="en"`` to Whisper
    so it stops auto-detecting Russian on English-language segments.

    Resolves the audio in this order:
      1. explicit ``audio_path`` arg (CLI smoke-test);
      2. ``ZoomRecording.audio_path`` lookup by ``zoom_id`` (in-disk
         cached audio from the original ingestion);
      3. on-demand download from ``ZoomRecording.audio_url`` (Zoom
         cloud) via `ZoomClient.download_audio` — uses the same
         OAuth credentials the ingestion pipeline already has. The
         temp file is deleted after Whisper returns.

    Returns ``None`` on any failure — caller falls back to the
    primary transcript.
    """
    if not openai_api_key:
        return None
    path = (audio_path or "").strip()
    tmp_to_cleanup: str | None = None
    if not path and zoom_id:
        path = _load_audio_path_for_zoom_id(zoom_id) or ""
    if not path and zoom_id:
        url = _load_audio_url_for_zoom_id(zoom_id)
        if url:
            log.info(
                "ceo_brain_bilingual_audio_local_miss_trying_cloud",
                zoom_id=zoom_id,
            )
            path = _download_zoom_audio_to_temp(url) or ""
            tmp_to_cleanup = path or None
    if not path:
        log.info("ceo_brain_bilingual_re_stt_skipped_no_audio")
        return None
    if not os.path.exists(path):
        log.info(
            "ceo_brain_bilingual_re_stt_skipped_missing",
            audio_path=path,
        )
        return None
    from app.services.transcription import transcribe_chunks_parallel

    # Whisper's per-request cap is 25 MB. The Zoom pipeline already
    # has a chunk-splitter that keeps chunks ≤24 MB and parallel-
    # transcribes them preserving order — reuse both. For files
    # under the cap we still go through `transcribe_chunks_parallel`
    # with a single-path list so the code path stays identical
    # whether the recording is 5 min or 90 min.
    whisper_max = 24 * 1024 * 1024
    chunk_paths: list[str]
    extra_tmp_chunks: list[str] = []
    size = os.path.getsize(path)
    if size <= whisper_max:
        chunk_paths = [path]
    else:
        try:
            from app.fireflies.pipeline import _split_audio_into_chunks

            chunk_paths = _split_audio_into_chunks(
                path, max_bytes=whisper_max,
            )
            # Splitter writes new chunks alongside the source; keep
            # track so we can clean up.
            extra_tmp_chunks = [c for c in chunk_paths if c != path]
            log.info(
                "ceo_brain_bilingual_audio_chunked",
                size=size, chunks=len(chunk_paths),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ceo_brain_bilingual_audio_chunk_failed",
                error=str(e), error_type=type(e).__name__,
            )
            if tmp_to_cleanup:
                try:
                    os.unlink(tmp_to_cleanup)
                except OSError:
                    pass
            return None

    def _mimetype_for(p: str) -> str:
        e = os.path.splitext(p)[1].lower()
        if e == ".m4a":
            return "audio/mp4"
        if e == ".mp4":
            return "video/mp4"
        return "audio/mpeg"

    # FR-CB2-3.39 — pass `language="en"` through to every chunk so
    # Whisper biases toward English on every segment.
    try:
        parts = transcribe_chunks_parallel(
            chunk_paths,
            openai_api_key=openai_api_key,
            model=model,
            prompt=whisper_prompt,
            mimetype_for=_mimetype_for,
            max_workers=3,
            language="en",
        )
    finally:
        if tmp_to_cleanup:
            try:
                os.unlink(tmp_to_cleanup)
            except OSError:
                pass
        for c in extra_tmp_chunks:
            try:
                os.unlink(c)
            except OSError:
                pass

    joined = "\n".join(p for p in (parts or []) if p)
    if not joined.strip():
        return None
    return joined.strip()


def _find_cut_point(text: str, target: int, window: int = 2000) -> int:
    """Return an offset close to ``target`` that lands on a natural
    boundary (newline → sentence end → whitespace). Falls back to
    the exact ``target`` when nothing better is within ``window``
    chars on either side. Keeps chunked-reconciler cuts from
    landing mid-sentence and stranding context."""
    n = len(text)
    if target <= 0 or target >= n:
        return max(0, min(target, n))
    lo = max(0, target - window)
    hi = min(n, target + window)
    # 1st choice — newline closest to target
    best = -1
    best_dist = window + 1
    for i in range(lo, hi):
        if text[i] == "\n":
            d = abs(i + 1 - target)
            if d < best_dist:
                best = i + 1
                best_dist = d
    if best != -1:
        return best
    # 2nd — sentence-end punctuation followed by space
    for i in range(lo, hi - 1):
        if text[i] in ".!?…" and text[i + 1] == " ":
            d = abs(i + 1 - target)
            if d < best_dist:
                best = i + 1
                best_dist = d
    if best != -1:
        return best
    # 3rd — any whitespace
    for i in range(lo, hi):
        if text[i].isspace():
            d = abs(i - target)
            if d < best_dist:
                best = i
                best_dist = d
    return best if best != -1 else target


def merge_transcripts(
    *,
    primary: str,
    secondary: str,
    openai_client: Any,
    model: str = "gpt-4o",
    max_input_chars: int = 60_000,
    max_tokens: int = 16_384,
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
    zoom_id: str | None = None,
    audio_path: str | None = None,
    openai_client: Any | None = None,
    openai_api_key: str = "",
    detector_model: str = "gpt-4o-mini",
    reconciler_model: str = "gpt-4o",
    whisper_model: str = "whisper-1",
    reconcile_batch_input_chars: int = 30_000,
    reconcile_max_tokens_per_batch: int = 16_384,
) -> tuple[str, dict[str, Any]]:
    """End-to-end orchestrator. Returns ``(final_text, trace)``.

    ``trace`` captures each decision so the responder can persist
    it to ``claude_responder_runs.request_payload['_bilingual']``
    for diagnostics. Possible ``stage`` values:

      * ``detector_skipped_empty`` — no transcript content.
      * ``detector_no_client`` — OpenAI client not provided.
      * ``detector_said_no`` — LLM voted to skip.
      * ``re_stt_no_audio`` — flag on, detector said yes, but the
        audio path could not be resolved (no zoom_id row or file
        missing on disk).
      * ``re_stt_failed`` — Whisper returned nothing.
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
        if not openai_api_key:
            trace["stage"] = "re_stt_no_audio"
            return transcript, trace
        secondary = re_stt_english_via_whisper(
            zoom_id=zoom_id,
            audio_path=audio_path,
            openai_api_key=openai_api_key,
            model=whisper_model,
        )
        if not secondary:
            # Distinguish «no audio path» from «whisper returned
            # nothing» — both end up in ``re_stt_failed`` here for
            # simplicity; structured-log lines in
            # ``re_stt_english_via_whisper`` cover the why.
            trace["stage"] = (
                "re_stt_no_audio" if not (zoom_id or audio_path)
                else "re_stt_failed"
            )
            return transcript, trace
        trace["secondary_chars"] = len(secondary)
        merged = merge_transcripts_chunked(
            primary=transcript,
            secondary=secondary,
            openai_client=openai_client,
            model=reconciler_model,
            batch_input_chars=reconcile_batch_input_chars,
            max_tokens_per_batch=reconcile_max_tokens_per_batch,
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
    "re_stt_english_via_whisper",
    "merge_transcripts",
    "merge_transcripts_chunked",
    "restore_transcript_bilingual",
]
