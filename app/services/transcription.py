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
) -> str | None:
    """Send audio to OpenAI Whisper; returns the transcript or None."""
    if not openai_api_key or not audio_bytes:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=openai_api_key)
        resp = client.audio.transcriptions.create(
            model=model,
            file=(filename, audio_bytes, mimetype),
        )
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
    "merge_transcripts_into_text",
]
