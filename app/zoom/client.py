"""FR-CR-05-116 — Zoom Cloud Recordings API client.

Mirror of `app/fireflies/client.py` for Zoom. Uses the
Server-to-Server OAuth flow (account_id + client_id +
client_secret → short-lived access_token), pulls cloud
recording metadata via REST, and downloads audio via the
recording-file `download_url` with bearer auth.

Same `request_func` injection seam as the Fireflies client so
tests can stub the network without touching urllib.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class ZoomRecordingMeta:
    """One Zoom cloud recording, normalised for the pipeline.

    `id` is Zoom's UUID (the dedup key); `meeting_id` is the
    numeric room id. `audio_url` is the `download_url` of the
    smallest audio-only file we can find (M4A first, MP4 as
    fallback). `host_email` is the recording's host (operator
    uses this to filter to only Artem-hosted meetings — FR-CR-05-143)."""

    id: str
    meeting_id: str | None
    title: str | None
    meeting_date: datetime | None
    duration_seconds: int | None
    participants: list[str]
    audio_url: str | None
    share_url: str | None
    host_email: str | None = None
    raw: dict = field(default_factory=dict)


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _pick_audio_url(recording_files: list[dict]) -> str | None:
    """Prefer M4A (audio-only, small), fall back to MP4 with
    audio. Skip transcripts, chat logs, etc."""
    audio_pref = ("M4A", "MP3")
    video_fallback = ("MP4",)
    for ft in audio_pref:
        for f in recording_files:
            if (f.get("file_type") or "").upper() == ft:
                u = f.get("download_url")
                if u:
                    return str(u)
    for ft in video_fallback:
        for f in recording_files:
            if (f.get("file_type") or "").upper() == ft:
                u = f.get("download_url")
                if u:
                    return str(u)
    return None


def _coerce_participants(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for p in value:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            label = p.get("name") or p.get("user_name") or p.get("email")
            if label:
                out.append(str(label))
    return out


class ZoomClient:
    """Synchronous wrapper around the Zoom REST API.

    Empty `account_id` / `client_id` / `client_secret` ⇒ client
    is disabled and every method returns the equivalent of «no
    recordings available».
    """

    def __init__(
        self,
        *,
        account_id: str,
        client_id: str,
        client_secret: str,
        api_base: str = "https://api.zoom.us/v2",
        oauth_url: str = "https://zoom.us/oauth/token",
        timeout: float = 30.0,
        request_func: Callable[[str, dict, dict | bytes | None, str], dict] | None = None,
    ) -> None:
        self._account_id = account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_base = api_base.rstrip("/")
        self._oauth_url = oauth_url
        self._timeout = timeout
        # `request_func(url, headers, body_or_none, method)` lets
        # tests bypass the network. Default = stdlib urllib.
        self._request_func = request_func or self._default_request
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(
            self._account_id and self._client_id and self._client_secret
        )

    def _default_request(
        self,
        url: str,
        headers: dict[str, str],
        body: Any,
        method: str = "GET",
    ) -> dict[str, Any]:
        data: bytes | None = None
        if isinstance(body, dict):
            data = urllib.parse.urlencode(body).encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                body_text = ""
            log.warning(
                "zoom_api_http_error",
                status=e.code,
                url=url,
                body=body_text[:500],
            )
            return {}
        except Exception as e:  # noqa: BLE001
            log.warning("zoom_api_call_failed", error=str(e), url=url)
            return {}
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

    # --- OAuth -------------------------------------------------

    def _ensure_access_token(self) -> str | None:
        """FR-CR-05-116 — Zoom S2S OAuth: POST /oauth/token with
        Basic auth and `grant_type=account_credentials`. Token
        is short-lived (1 h); cached until 60 s before expiry."""
        if not self.enabled:
            return None
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        creds = f"{self._client_id}:{self._client_secret}".encode()
        basic = base64.b64encode(creds).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = {
            "grant_type": "account_credentials",
            "account_id": self._account_id,
        }
        payload = self._request_func(
            self._oauth_url, headers, body, "POST"
        )
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            log.warning(
                "zoom_oauth_token_unavailable",
                payload_keys=list(payload.keys())[:10] if isinstance(payload, dict) else None,
            )
            return None
        self._token = str(token)
        ttl = int(payload.get("expires_in") or 3600)
        self._token_expires_at = time.time() + ttl
        return self._token

    # --- list recordings --------------------------------------

    def list_recordings(
        self, *, limit: int = 20, page_size: int = 30,
        from_date: str | None = None, to_date: str | None = None,
        required_email: str | None = None,
    ) -> list[ZoomRecordingMeta]:
        """Return up to `limit` most-recent cloud recordings.

        FR-CR-05-135 — Zoom Server-to-Server OAuth tokens are
        NOT bound to a user, so the user-scope `/users/me/recordings`
        endpoint 400s («Invalid access token, does not contain
        scopes»). We use the account-wide endpoint
        `/accounts/me/recordings` instead, which requires the
        `cloud_recording:read:list_account_recordings:admin`
        scope on the Zoom Marketplace app.

        `from_date` / `to_date` are ISO `YYYY-MM-DD`. When None,
        Zoom defaults to the last month. The migrator uses this
        same call to grab «last N meetings» (mirrors Fireflies).
        """
        if not self.enabled:
            return []
        token = self._ensure_access_token()
        if not token:
            return []
        params = [f"page_size={int(page_size)}"]
        if from_date:
            params.append(f"from={from_date}")
        if to_date:
            params.append(f"to={to_date}")
        url = (
            f"{self._api_base}/accounts/me/recordings?"
            + "&".join(params)
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = self._request_func(url, headers, None, "GET")
        meetings = (payload or {}).get("meetings") or []
        # FR-CR-05-143 — operator-pinned: keep only recordings
        # where `required_email` is the host OR appears in the
        # `/past_meetings/{uuid}/participants` list. `/accounts/me/
        # recordings` returns the whole workspace; operator wants
        # Artem-only.
        required = (required_email or "").strip().lower()
        out: list[ZoomRecordingMeta] = []
        skipped_no_match = 0
        skipped_participants_check = 0
        for m in meetings:
            if not isinstance(m, dict):
                continue
            uuid = m.get("uuid")
            if not uuid:
                continue
            row_host = (m.get("host_email") or "").strip().lower()
            if required:
                host_matches = (row_host == required)
                if not host_matches:
                    # Host doesn't match — fall back to checking
                    # the participants list (extra API call).
                    p_emails = self._fetch_meeting_participant_emails(
                        str(uuid), token=token,
                    )
                    if required not in p_emails:
                        skipped_no_match += 1
                        continue
                    skipped_participants_check += 1
            audio_url = _pick_audio_url(m.get("recording_files") or [])
            out.append(
                ZoomRecordingMeta(
                    id=str(uuid),
                    meeting_id=(
                        str(m["id"]) if "id" in m and m["id"] is not None
                        else None
                    ),
                    title=m.get("topic") or None,
                    meeting_date=_coerce_dt(m.get("start_time")),
                    duration_seconds=(
                        int(m["duration"]) * 60
                        if isinstance(m.get("duration"), (int, float))
                        else None
                    ),
                    participants=_coerce_participants(
                        m.get("participants") or []
                    ),
                    audio_url=audio_url,
                    share_url=m.get("share_url") or None,
                    host_email=(m.get("host_email") or None),
                    raw=m,
                )
            )
            if len(out) >= limit:
                break
        if required:
            log.info(
                "zoom_recordings_listed",
                required_email=required, kept=len(out),
                skipped_no_match=skipped_no_match,
                kept_via_participants=skipped_participants_check,
                page_total=len(meetings),
            )
        return out

    def _fetch_meeting_participant_emails(
        self, uuid: str, *, token: str | None = None,
    ) -> set[str]:
        """FR-CR-05-143 — fetch participant emails for a single
        recording via `/past_meetings/{uuid}/participants`. Used
        by the host-or-participant filter when `host_email`
        doesn't match (so we still don't skip recordings where
        Artem joined someone else's call).

        Returns lowercase emails. Empty set on any failure
        (auth, 404, network) — never raises. Zoom UUIDs that
        contain `/` or start with `/` need to be DOUBLE
        URL-encoded; we always single-encode the `==`/normal
        chars and double-encode when the uuid starts with `/`
        or contains `//`.
        """
        if not uuid:
            return set()
        token = token or self._ensure_access_token()
        if not token:
            return set()
        # Per Zoom docs: double-encode UUIDs that start with `/`
        # or contain `//`. Single-encode otherwise.
        encoded = urllib.parse.quote(uuid, safe="")
        if uuid.startswith("/") or "//" in uuid:
            encoded = urllib.parse.quote(encoded, safe="")
        url = (
            f"{self._api_base}/past_meetings/{encoded}/participants"
            "?page_size=300"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = self._request_func(url, headers, None, "GET")
        rows = (payload or {}).get("participants") or []
        emails: set[str] = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            e = (r.get("user_email") or r.get("email") or "").strip().lower()
            if e:
                emails.add(e)
        return emails

    # --- audio download ---------------------------------------

    def download_audio(
        self, *, url: str, dest_path: str, max_bytes: int
    ) -> int | None:
        """Download `url` to `dest_path`. Returns bytes written
        on success, None on failure / cap-exceeded.

        Zoom recording-file `download_url`s require bearer auth
        — append `access_token=<token>` query param OR use
        Authorization header (we use the latter).
        """
        token = self._ensure_access_token()
        if not token:
            log.warning("zoom_audio_no_token")
            return None
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                # Stream-copy with a cap.
                import os

                os.makedirs(
                    os.path.dirname(dest_path) or ".", exist_ok=True
                )
                written = 0
                with open(dest_path, "wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            log.warning(
                                "zoom_audio_too_large",
                                bytes=written,
                                cap=max_bytes,
                                url=url[:200],
                            )
                            f.close()
                            os.remove(dest_path)
                            return None
                        f.write(chunk)
                return written
        except urllib.error.HTTPError as e:
            log.warning(
                "zoom_audio_http_error", status=e.code, url=url[:200]
            )
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("zoom_audio_download_failed", error=str(e))
            return None


__all__ = ["ZoomClient", "ZoomRecordingMeta"]
