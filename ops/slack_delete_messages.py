"""Delete one or more Slack messages by (channel, ts) via chat.delete.

The bot must have chat:write scope and the message must be authored
by the bot (same token). Posting + deleting are symmetric.

Usage:
    docker exec manager-bot-1 python -m ops.slack_delete_messages \\
        --channel D0ASY5QF6UX \\
        --ts 1779408352.011039 \\
        --ts 1779408263.310459 \\
        --ts 1779408263.626729

Multiple --ts flags allowed — script iterates and prints result per ts.
Defaults to the ceo_brain bot token (same as send_one_*).
"""
from __future__ import annotations

import argparse
import sys
import time

from app.config import get_settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument(
        "--ts", required=True, action="append",
        help="Slack message ts (e.g. 1779408352.011039). Repeatable.",
    )
    ap.add_argument(
        "--token-key", default="ceo_brain_slack_bot_token",
        choices=[
            "ceo_brain_slack_bot_token",
            "slack_bot_token",
            "agenda_slack_bot_token",
        ],
    )
    ap.add_argument(
        "--sleep", type=float, default=0.3,
        help="Seconds between deletes (Slack rate limit cushion).",
    )
    args = ap.parse_args()

    s = get_settings()
    token = getattr(s, args.token_key, "") or ""
    if not token:
        print(f"ERROR: settings.{args.token_key} is empty.", file=sys.stderr)
        return 2

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=token)
    failed = 0
    deleted = 0
    for ts in args.ts:
        try:
            resp = client.chat_delete(channel=args.channel, ts=ts)
            ok = (resp.data or {}).get("ok", False)
            if ok:
                print(f"  ✓ deleted {ts}")
                deleted += 1
            else:
                print(f"  ✗ {ts}: response not ok: {resp.data}")
                failed += 1
        except SlackApiError as e:
            err = (e.response.data or {}).get("error", "?")
            print(f"  ✗ {ts}: {err}")
            failed += 1
        time.sleep(args.sleep)

    print()
    print(f"Deleted: {deleted}   Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
