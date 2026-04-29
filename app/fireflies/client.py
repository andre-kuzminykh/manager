"""FR-CR-05-40 — Fireflies GraphQL API client.

Thin wrapper around the public Fireflies API
(``https://api.fireflies.ai/graphql``). Two operations:

  - ``list_transcripts(limit, ...)`` — pull the most recent N
    transcripts. Each row carries `id`, `title`, `date`,
    `duration`, `participants`, `audio_url`, `transcript_url`.
  - ``download_audio(url, dest_path, max_bytes)`` — fetch the
    mp3 to local disk for Whisper.

The client is deliberately pure stdlib (urllib + json) so it
ships with no extra dependency on the bot's Docker image. Tests
inject a `request_func` to bypass the network.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class FirefliesTranscript:
    """One row from the `transcripts` GraphQL query, normalised."""

    id: str
    title: str | None
    meeting_date: datetime | None
    duration_seconds: int | None
    participants: list[str]
    audio_url: str | None
    share_url: str | None
    raw: dict[str, Any]


_TRANSCRIPTS_QUERY = """
query Transcripts($limit: Int!, $skip: Int) {
  transcripts(limit: $limit, skip: $skip) {
    id
    title
    date
    duration
    transcript_url
    audio_url
    participants
    meeting_attendees {
      displayName
      email
    }
  }
}
"""


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Fireflies returns the meeting date as a Unix-millis
        # timestamp; convert to seconds for fromtimestamp.
        try:
            secs = float(value)
            if secs > 1e11:  # millis (~year 5138 in seconds)
                secs /= 1000.0
            return datetime.fromtimestamp(secs)
        except (TypeError, ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _coerce_participants(value: Any) -> list[str]:
    """Fireflies sends participants as either a list of strings
    (emails) or a list of dicts (`{displayName, email}`); we
    normalise to a list of «Display (email)» strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for p in value:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            disp = p.get("displayName") or p.get("name")
            email = p.get("email")
            if disp and email:
                out.append(f"{disp} <{email}>")
            elif disp:
                out.append(str(disp))
            elif email:
                out.append(str(email))
    return out


class FirefliesClient:
    """Synchronous wrapper around the Fireflies GraphQL API.

    Disabled when ``token`` is empty — every method short-
    circuits to a no-op and the pipeline treats the source as
    «no recordings available».
    """

    def __init__(
        self,
        *,
        token: str,
        endpoint: str = "https://api.fireflies.ai/graphql",
        timeout: float = 30.0,
        request_func: Callable[[str, dict, dict], dict] | None = None,
    ) -> None:
        self._token = token
        self._endpoint = endpoint
        self._timeout = timeout
        # `request_func(url, headers, body_dict)` lets tests
        # bypass the network. Default impl is the stdlib urllib.
        self._request_func = request_func or self._default_request

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _default_request(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                body_text = ""
            log.warning(
                "fireflies_api_http_error",
                status=e.code,
                body=body_text[:500],
            )
            return {}
        except Exception as e:  # noqa: BLE001
            log.warning("fireflies_api_call_failed", error=str(e))
            return {}
        if isinstance(payload, dict):
            return payload
        return {}

    def list_transcripts(
        self, *, limit: int = 20, skip: int = 0
    ) -> list[FirefliesTranscript]:
        """Return up to `limit` most-recent transcripts.

        Fireflies' default sort is reverse-chronological (newest
        first), which is what we want — the migrator uses this
        same call to grab «last N meetings»."""
        if not self.enabled:
            return []
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        body = {
            "query": _TRANSCRIPTS_QUERY,
            "variables": {"limit": int(limit), "skip": int(skip)},
        }
        payload = self._request_func(self._endpoint, headers, body)
        data = (payload or {}).get("data") or {}
        rows = data.get("transcripts") or []
        out: list[FirefliesTranscript] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            tid = r.get("id")
            if not tid:
                continue
            participants = _coerce_participants(
                r.get("meeting_attendees") or r.get("participants")
            )
            out.append(
                FirefliesTranscript(
                    id=str(tid),
                    title=r.get("title") or None,
                    meeting_date=_coerce_dt(r.get("date")),
                    duration_seconds=(
                        int(r["duration"])
                        if isinstance(r.get("duration"), (int, float))
                        else None
                    ),
                    participants=participants,
                    audio_url=r.get("audio_url") or None,
                    share_url=r.get("transcript_url") or None,
                    raw=r,
                )
            )
        return out

    def download_audio(
        self,
        *,
        url: str,
        dest_path: str,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> int | None:
        """Download the mp3 at ``url`` to ``dest_path``. Returns
        the byte count on success, ``None`` when the file blew
        past ``max_bytes`` or the GET failed.

        ``dest_path``'s parent directory is auto-created.
        """
        if not url:
            return None
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                content = resp.read(max_bytes + 1)
        except urllib.error.URLError as e:
            log.warning(
                "fireflies_audio_download_failed",
                url=url,
                error=str(e),
            )
            return None
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_audio_download_failed",
                url=url,
                error=str(e),
            )
            return None
        if len(content) > max_bytes:
            log.warning(
                "fireflies_audio_too_large",
                url=url,
                bytes=len(content),
                cap=max_bytes,
            )
            return None
        with open(dest_path, "wb") as f:
            f.write(content)
        return len(content)


__all__ = ["FirefliesClient", "FirefliesTranscript"]
