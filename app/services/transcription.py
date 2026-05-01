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
) -> str | None:
    """Send audio to OpenAI Whisper; returns the transcript or None.

    `prompt` is Whisper's optional context-biasing string (≤224
    tokens, hard-truncated server-side). Used by the meeting
    pipelines (FR-CR-05-127) to push proper-noun spellings the
    model otherwise mangles — e.g. «Tether» → «teaser»,
    «Schaeffler» → «шафлера», team member real names. Empty /
    None skips the parameter entirely so the Slack voice-message
    path stays unchanged.
    """
    if not openai_api_key or not audio_bytes:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=openai_api_key)
        kwargs: dict[str, Any] = {
            "model": model,
            "file": (filename, audio_bytes, mimetype),
        }
        if prompt:
            kwargs["prompt"] = prompt
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
