"""FR-CB2-200 — CEO Brain Bot.

Two capabilities sharing a Slack-event stream:

  * **Archive** — every message in subscribed channels is appended
    to ``<archive_dir>/<channel>/YYYY-MM-DD.jsonl`` and mirrored
    into the ``slack_message_archive`` PG table.
  * **Responder** — when the operator @mentions the bot or DMs
    it, the event is forwarded to the Anthropic Messages API
    along with a list of MCP servers the operator's claude.ai
    account is connected to. Claude decides which connectors to
    call and streams the answer back into the same Slack thread.

See ``SPEC_CEO_BRAIN_BOT_v0.1.md`` for the full requirements.
"""
