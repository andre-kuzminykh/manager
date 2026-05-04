"""FR-CR-05-137 — mirror meeting short-summary DMs from
Telegram to a Slack channel (typically the Artem-AI bot DM
`D0AUXKND35Y`).

Operator-pinned: «и потом еще в слак надо отдавать зум встречи,
в Artem AI отдаем как только в телеграм приходят, сразу же
туда».

Pipeline integration: ZoomPipeline._step_send_short_summary
already builds + sends the body to admin Telegram users; this
service hooks the same body off into Slack as a separate post.
Failures here MUST NOT break the rest of the pipeline.

Telegram body uses HTML `<a href="<doc_url>">title</a>` for the
title-as-hyperlink contract (FR-CR-05-127). Slack's mrkdwn uses
`<doc_url|title>` instead — `_to_slack_mrkdwn` does the swap +
strips any leftover HTML escaping.
"""
from __future__ import annotations

import re
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


def _to_slack_mrkdwn(html_body: str) -> str:
    """Convert the Telegram HTML body to Slack mrkdwn.

    Specifically: `<a href="URL">TITLE</a>` → `<URL|TITLE>`.
    HTML-escaped chars (`&amp;` / `&lt;` / `&gt;`) are decoded.
    Operator-pinned: keep everything else verbatim — Slack
    renders newlines + plain text fine.
    """
    if not html_body:
        return ""
    text = re.sub(
        r'<a\s+href=["\'](?P<url>[^"\']+)["\']>(?P<label>[^<]+)</a>',
        lambda m: f"<{m.group('url')}|{m.group('label')}>",
        html_body,
    )
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return text


def post_meeting_summary_to_slack(
    *,
    slack_token: str,
    channel_id: str,
    body: str,
) -> dict[str, Any] | None:
    """POST `chat.postMessage` to the configured channel.

    Returns the Slack response dict on success, ``None`` when
    disabled (no token / no channel) or when the call failed
    (caller logs + continues — never raises).

    Pipeline-side wiring guarantees this is called in a
    try/except that swallows any unexpected error, so even a
    transient Slack 5xx doesn't take down the meeting pipeline.
    """
    if not slack_token or not channel_id:
        return None
    if not body or not body.strip():
        return None
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        log.warning("slack_mirror_slack_sdk_missing")
        return None

    text = _to_slack_mrkdwn(body)
    try:
        client = WebClient(token=slack_token)
        resp = client.chat_postMessage(
            channel=channel_id,
            text=text,
            unfurl_links=False,
            unfurl_media=False,
        )
        return resp.data if hasattr(resp, "data") else dict(resp)
    except SlackApiError as e:  # noqa: BLE001
        status = (
            e.response.status_code
            if e.response is not None else None
        )
        err = (
            e.response.data.get("error")
            if e.response is not None and isinstance(e.response.data, dict)
            else None
        )
        log.warning(
            "slack_mirror_post_failed",
            channel_id=channel_id, http_status=status, slack_error=err,
        )
        return None
    except Exception as e:  # noqa: BLE001
        log.warning(
            "slack_mirror_post_unexpected_error",
            channel_id=channel_id, error=str(e),
        )
        return None


__all__ = ["post_meeting_summary_to_slack", "_to_slack_mrkdwn"]
