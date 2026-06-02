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
from datetime import datetime, timezone
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
    """Parse Fireflies' meeting date into a tz-aware **UTC** datetime.

    Fireflies sends the date as Unix-millis (int) or an ISO string. We ALWAYS
    return aware-UTC so downstream calendar math (attendee resolution, title
    match) never hits «can't subtract offset-naive and offset-aware datetimes».
    Bug 2026-06-02: the millis branch used `datetime.fromtimestamp(secs)` which
    returns a NAIVE local datetime → broke `_step_match_calendar_title` /
    `_populate_calendar_attendees` (meeting stayed «Jun 02, 01:03 PM», no
    calendar rename, no attendees).
    """
    if value is None:
        return None
    dt: datetime | None = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Unix-millis timestamp; convert to seconds for fromtimestamp.
        try:
            secs = float(value)
            if secs > 1e11:  # millis (~year 5138 in seconds)
                secs /= 1000.0
            dt = datetime.fromtimestamp(secs, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    # Normalise to aware-UTC (assume UTC for naive inputs).
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
                    # FR-CR-05-195 — Fireflies API `duration` field is in
                    # MINUTES, not seconds. Multiply by 60 to get the value
                    # we actually store in `duration_seconds`. A 50-min
                    # interview comes in as `duration=50.0` → 3000 sec.
                    duration_seconds=(
                        int(r["duration"] * 60)
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

    def fetch_transcript_text(self, fireflies_id: str) -> str:
        """FR-CR-05-115 — fall back to Fireflies' GraphQL
        `sentences` field when the audio file exceeds the
        Whisper 25 MB cap. Returns the joined sentence text
        (or empty string on any failure).

        Operator regression: 26 MB meeting audio failed Whisper;
        Fireflies already provides per-sentence transcript via
        their API, so we don't NEED Whisper for those — pull
        the transcript directly.
        """
        if not self.enabled or not fireflies_id:
            return ""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        query = (
            "query Sentences($id: String!) { transcript(id: $id) "
            "{ sentences { text } } }"
        )
        body = {"query": query, "variables": {"id": fireflies_id}}
        try:
            payload = self._request_func(self._endpoint, headers, body)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_fetch_transcript_failed",
                fireflies_id=fireflies_id,
                error=str(e),
            )
            return ""
        data = (payload or {}).get("data") or {}
        tx = data.get("transcript") or {}
        sentences = tx.get("sentences") or []
        chunks: list[str] = []
        for s in sentences:
            if not isinstance(s, dict):
                continue
            t = (s.get("text") or "").strip()
            if t:
                chunks.append(t)
        return "\n".join(chunks)

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


    def update_transcript_title(
        self, transcript_id: str, title: str,
    ) -> bool:
        """FR-CR-05-154 — push a new title back to Fireflies via
        their `updateMeetingTitle` GraphQL mutation. Used after
        `_step_match_calendar_title` rewrites our DB title to
        the canonical «DD/MM - <calendar event>» — operator-
        pinned «зум не надо переименовывать, только firefiles»
        / «встреча все равно называется: '30/04 - James Morgon'»
        (in Fireflies UI).

        Returns True on success, False on any failure (network,
        HTTP error, mutation-level error). Failures NEVER raise
        — caller wraps in try/except so the rest of the pipeline
        keeps going."""
        if not self.enabled or not transcript_id or not title:
            return False
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        query = (
            "mutation UpdateMeetingTitle($title: String!, "
            "$transcript_id: String!) { "
            "updateMeetingTitle(input: { title: $title, "
            "transcript_id: $transcript_id }) "
            "{ title success message } }"
        )
        body = {
            "query": query,
            "variables": {"title": title, "transcript_id": transcript_id},
        }
        try:
            payload = self._request_func(self._endpoint, headers, body)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "fireflies_update_title_request_failed",
                transcript_id=transcript_id, error=str(e),
            )
            return False
        if not isinstance(payload, dict):
            return False
        # GraphQL errors come at the top-level `errors` key.
        if payload.get("errors"):
            log.warning(
                "fireflies_update_title_graphql_errors",
                transcript_id=transcript_id,
                errors=payload.get("errors"),
            )
            return False
        data = (payload or {}).get("data") or {}
        result = data.get("updateMeetingTitle") or {}
        if not result.get("success"):
            log.warning(
                "fireflies_update_title_returned_unsuccessful",
                transcript_id=transcript_id,
                message=result.get("message"),
            )
            return False
        log.info(
            "fireflies_title_updated_remote",
            transcript_id=transcript_id,
            new_title=title,
            message=result.get("message"),
        )
        return True


__all__ = ["FirefliesClient", "FirefliesTranscript"]
