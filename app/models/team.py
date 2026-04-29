"""FR-CR-05-10 — Cross-channel team registry.

A single authoritative directory of «people we can assign tasks
to», unified across Slack and Telegram. Each row carries every
identity we know for that person — Slack user_id, TG numeric id,
TG username, real name, role, email — so the LLM owner stage has
ONE place to look up names and the bot can DM them on whichever
channel they're reachable.

Why a separate table from `employees` (Slack-only) and
`telegram_chat_members` (per-chat upserts):

- `employees` is keyed by `slack_user_id` and only carries Slack
  identity — no way to attach a Telegram numeric id.
- `telegram_chat_members` is keyed by `(chat_id, user_id)` and
  doesn't carry Slack identity at all; same person speaking in
  three different chats produces three rows.
- Neither table is hand-edited by an admin. The team registry IS:
  the operator owns it via a Google Sheet (`Team` tab), and the
  bot reads from it for owner resolution.

Sync direction: bidirectional with the `Team` tab in the
spreadsheet pointed to by ``GOOGLE_TEAM_SHEETS_SPREADSHEET_ID``.
The sync auto-seeds new rows from chat-members + employees on
first run, then the operator polishes role / email / active flag.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TeamMember(Base, TimestampMixin):
    """One row per teammate the bot can assign work to.

    Owner-resolution authoritative source: the LLM owner stage gets
    `as_known_employees()` derived from this table (unioned with
    per-chat members for hint-only mode). When an extracted owner
    name doesn't match anything here, we fall back to admin —
    that's the «CEO Rosecliff» kill-switch for outsiders mentioned
    in chat but not actually on the team.
    """

    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    real_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Telegram identity. user_id is the numeric one (the only thing
    # the Bot API accepts for sendMessage / DM). username is the
    # @-handle without the leading @.
    telegram_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, unique=True
    )
    telegram_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # Slack identity. Same `slack_user_id` shape that `employees`
    # uses (`U…` / `W…`); not a foreign key because team_members may
    # contain people we haven't seen on Slack yet.
    slack_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )

    role: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Inactive members are kept in the table for audit but excluded
    # from the LLM's `known_employees` list. Useful when somebody
    # leaves the team — you can't delete the row without breaking
    # historical task assignments.
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)

    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["TeamMember"]
