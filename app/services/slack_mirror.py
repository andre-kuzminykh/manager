"""FR-CR-05-137 / FR-CR-05-141 — mirror meeting short-summary
DMs from Telegram to a Slack channel (typically the Artem-AI
bot DM `D0AUXKND35Y`).

Operator-pinned: «и потом еще в слак надо отдавать зум встречи,
в Artem AI отдаем как только в телеграм приходят, сразу же
туда» + «можешь в слак отправлять в зум саммери все сообщение
целиком до 35 тыс символов, а что уже не вмещается то след
сообщением» (FR-CR-05-141 — chunked sequential delivery).

Pipeline integration: ZoomPipeline._step_send_short_summary
already builds + sends the body to admin Telegram users; this
service hooks the same body off into Slack as one OR MORE
sequential posts. Failures here MUST NOT break the rest of the
pipeline.

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


# FR-CR-05-141 — Slack chat.postMessage `text` param has a soft
# cap that's higher than this, but we keep a 35 000-char split
# bound (operator-pinned) to leave headroom for mrkdwn renderer
# + safe under any per-block / per-attachment limits.
SLACK_TEXT_CHUNK_CHARS = 35_000


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


# FR-CR-05-147 — recognise numbered task list lines: `1) ...`,
# `12) ...` etc. Used by `_compact_for_slack` to fuse blank-
# line-separated tasks into a single visual paragraph.
_NUMBERED_LINE_RE = re.compile(r"\n\n(?=\d+\)\s)")


def _compact_for_slack(text: str) -> str:
    """FR-CR-05-147 — operator-pinned «опять в слаке отдельные
    сообщения - сделай под слак отдельную функцию которая
    соединяет все в одно».

    The Telegram body uses `\\n\\n` (double-newline) between
    every numbered task so each one gets a visual gap on
    mobile. Slack renders each `\\n\\n`-separated paragraph as
    a SEPARATE message bubble for long messages, so a 60-task
    To-Do reads as 60 floating cards instead of one tidy list.

    This compactor:
      1. Collapses `\\n\\n` → `\\n` BETWEEN consecutive numbered
         items (`1)` … `2)` …) so the To-Do list reads as a
         single paragraph in Slack.
      2. Leaves the `\\n\\n` BEFORE the first numbered item
         (after «To-Do:») alone — keeps separation between the
         heading and the list.
      3. Leaves all OTHER `\\n\\n` (between Header / Участники /
         Суть / To-Do) intact — those should stay paragraph-
         separated.
      4. Collapses 3+ consecutive newlines anywhere → exactly 2.

    Pure text in / out, no markdown injection.
    """
    if not text:
        return ""
    # Collapse any 3+ blank-line runs to exactly 2.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Iteratively collapse `\n\n` between consecutive numbered
    # items. We don't anchor to the FIRST `1)` so the heading-
    # to-list gap is preserved (To-Do:\n\n1) ... → unchanged on
    # first item; subsequent 2), 3), ... get fused).
    prev: str = ""
    while text != prev:
        prev = text
        # Match `\n\n` that's preceded by an item line (`...)` or
        # text ending without `:`) and followed by another `N)`.
        # Lookbehind: not a colon (so «To-Do:\n\n1)» stays).
        text = re.sub(
            r"(?<=[^:])\n\n(?=\d+\)\s)",
            "\n",
            text,
            count=1,
        )
    return text


def _split_for_slack(
    text: str, *, limit: int = SLACK_TEXT_CHUNK_CHARS
) -> list[str]:
    """FR-CR-05-141 — split `text` into ≤ `limit`-char chunks
    on paragraph (then line) boundaries so multi-message
    delivery doesn't cut tasks mid-sentence.

    Strategy:
      1. If `text` ≤ limit → single-element list.
      2. Otherwise split by double-newline paragraphs and
         greedily pack each paragraph into the current chunk.
      3. If a single paragraph is still too long, fall back to
         single-newline splits inside it.
      4. If even a single line is too long (extremely rare),
         hard-cut at `limit` chars.

    Empty input → empty list (caller treats as no-op).
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    def _flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    def _add_block(block: str, sep: str) -> None:
        nonlocal current
        if not block:
            return
        if len(block) > limit:
            # Block alone exceeds limit — split on lines.
            lines = block.split("\n")
            line_buf = ""
            for line in lines:
                if len(line) > limit:
                    # Even a single line is too long — flush
                    # what we have, hard-cut the line.
                    if line_buf:
                        _add_block(line_buf, sep="\n")
                        line_buf = ""
                    for i in range(0, len(line), limit):
                        chunks.append(line[i : i + limit])
                    continue
                candidate = (
                    line if not line_buf
                    else f"{line_buf}\n{line}"
                )
                if len(candidate) > limit:
                    _add_block(line_buf, sep="\n")
                    line_buf = line
                else:
                    line_buf = candidate
            if line_buf:
                _add_block(line_buf, sep="\n")
            return
        candidate = block if not current else f"{current}{sep}{block}"
        if len(candidate) > limit:
            _flush()
            current = block
        else:
            current = candidate

    for para in paragraphs:
        _add_block(para, sep="\n\n")
    _flush()
    return [c for c in chunks if c]


def post_meeting_summary_to_slack(
    *,
    slack_token: str,
    channel_id: str,
    body: str,
) -> list[dict[str, Any]]:
    """POST `chat.postMessage` to the configured channel.

    FR-CR-05-141: returns a list of Slack response dicts —
    multi-message delivery for bodies > 35K chars. Each chunk
    is sent sequentially in a single thread-flat sequence (no
    Slack threads — operator wants chronological reading in
    the channel).

    Returns:
      - `[]` when disabled (no token / no channel / blank body)
        OR when slack_sdk is missing.
      - `[resp_dict, ...]` — one dict per successfully-sent
        chunk. Failed chunks are skipped (logged); short return
        list is the signal of partial success.

    Caller wraps in try/except so any unexpected exception
    doesn't take down the meeting pipeline.
    """
    if not slack_token or not channel_id:
        return []
    if not body or not body.strip():
        return []
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        log.warning("slack_mirror_slack_sdk_missing")
        return []

    text = _to_slack_mrkdwn(body)
    # FR-CR-05-147 — compact numbered-list spacing so the To-Do
    # block doesn't render as 60 separate bubbles in Slack.
    text = _compact_for_slack(text)
    chunks = _split_for_slack(text, limit=SLACK_TEXT_CHUNK_CHARS)
    if not chunks:
        return []

    try:
        client = WebClient(token=slack_token)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "slack_mirror_post_unexpected_error",
            channel_id=channel_id, error=str(e),
        )
        return []

    responses: list[dict[str, Any]] = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        try:
            resp = client.chat_postMessage(
                channel=channel_id,
                text=chunk,
                unfurl_links=False,
                unfurl_media=False,
            )
            data = resp.data if hasattr(resp, "data") else dict(resp)
            responses.append(data)
            log.info(
                "slack_mirror_chunk_posted",
                channel_id=channel_id,
                chunk_index=i, chunk_total=total,
                chunk_chars=len(chunk),
                ts=data.get("ts") if isinstance(data, dict) else None,
            )
        except SlackApiError as e:  # noqa: BLE001
            status = (
                e.response.status_code
                if e.response is not None else None
            )
            err = (
                e.response.data.get("error")
                if e.response is not None
                and isinstance(e.response.data, dict)
                else None
            )
            log.warning(
                "slack_mirror_post_failed",
                channel_id=channel_id, http_status=status,
                slack_error=err,
                chunk_index=i, chunk_total=total,
            )
            # Don't try more chunks on auth / rate-limit errors;
            # operator can re-trigger with the same content.
            break
        except Exception as e:  # noqa: BLE001
            log.warning(
                "slack_mirror_post_unexpected_error",
                channel_id=channel_id, error=str(e),
                chunk_index=i, chunk_total=total,
            )
            break
    return responses


__all__ = [
    "post_meeting_summary_to_slack",
    "_to_slack_mrkdwn",
    "_compact_for_slack",
    "_split_for_slack",
    "SLACK_TEXT_CHUNK_CHARS",
]
