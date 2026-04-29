"""FR-CR-05-07 — Telegram chat-members registry.

The live listener observes a stream of messages and for each one we
upsert a `TelegramChatMember` row keyed by `(chat_id, user_id)`.
The registry self-populates from real traffic — no separate
discovery RPC, no Bot-API admin enumeration (which would only
return chat admins anyway). Any user that's ever spoken in a chat
the bot can see lands in the table.

Two main consumers:

- *Owner resolution*. `TelegramIngestService.process_all` /
  `prepare_drafts` pull the per-chat member list and pass it as
  `known_employees` to the classifier. The LLM owner stage then
  maps natural-language hints («Валя сделай X», «по поручению
  Артема») to the matching numeric user_id, with the existing
  hallucination guard from FR-CR-04-22 protecting against names
  that don't actually exist in the chat.

- *Direct DM-able assignees*. When an Accept-on-draft fires and
  `task.owner_user_id` matches a registered member with
  `has_started_bot=True`, post_initial_card / refresh_card can DM
  the assignee directly instead of relying on admin fan-out. Until
  the assignee has hit `/start` once, Telegram refuses the DM —
  there's no way around it on the bot's side.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import TelegramChatMember

log = get_logger(__name__)


def _enrich_team_member_row(
    session: Session,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> None:
    """FR-CR-05-21 / FR-CR-05-27 — opportunistic upsert on the
    cross-channel `team_members` row for ``user_id``.

    Two paths:

      - Existing row: fill ONLY blank fields. Operator-edited
        values on the Sheet are never overwritten.
      - No row yet: INSERT a new row with whatever fields the
        observation provides. Likely-bot rows (heuristic match
        on `bot` / `_bot` / `office1` / `notif` / etc.) start
        ``active=False`` so they don't pollute the LLM's owner-
        candidate list. New teammates appearing in any chat the
        bot is in show up in the Team registry automatically —
        operator polishes on the Sheet later.

    Wrapped in try/except so a missing migration in a stale test
    fixture doesn't break the listener tick.
    """
    try:
        from datetime import datetime as _dt, timezone as _tz

        from app.models import TeamMember as _TM
        from app.services.team_members import _looks_like_bot

        row = (
            session.query(_TM)
            .filter(_TM.telegram_user_id == int(user_id))
            .first()
        )
        full_name = " ".join(
            p for p in (first_name, last_name) if p
        ).strip() or None
        if row is None:
            # FR-CR-05-27 — auto-create when a new user surfaces.
            is_bot = _looks_like_bot(full_name, username)
            session.add(
                _TM(
                    telegram_user_id=int(user_id),
                    telegram_username=username or None,
                    real_name=full_name,
                    active=not is_bot,
                    notes="auto: looks like bot account" if is_bot else None,
                    last_synced_at=_dt.now(_tz.utc),
                )
            )
            session.flush()
            return
        changed = False
        if (not row.telegram_username) and username:
            row.telegram_username = username
            changed = True
        if not row.real_name and full_name:
            row.real_name = full_name
            changed = True
        if changed:
            session.flush()
    except Exception as e:  # noqa: BLE001
        log.info("team_member_enrich_skipped", error=str(e))


def upsert_member(
    session: Session,
    *,
    chat_id: int,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    has_started_bot: bool | None = None,
) -> None:
    """Idempotent upsert keyed by `(chat_id, user_id)`. Runs on
    every observed message, so we re-touch `last_seen_at` and re-
    write the profile fields if Telegram sent newer values.

    Profile fields are written only when the new value is non-null.
    `has_started_bot` is sticky: once set to True, subsequent calls
    don't flip it back to False (a member who has /start-ed the bot
    once stays reachable).
    """
    if user_id is None or chat_id is None:
        return

    now = datetime.now(timezone.utc)

    # Try DB-side upsert first (Postgres). Fall back to
    # query-then-update for SQLite (test backend).
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        values = {
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "last_seen_at": now,
        }
        if has_started_bot is True:
            values["has_started_bot"] = True
        stmt = pg_insert(TelegramChatMember.__table__).values(**values)
        # On conflict — keep `has_started_bot=True` if either side
        # is True, refresh other fields when the new value isn't
        # null (so we don't blank out a known username with a None
        # from a user who later messages without one).
        update_set = {
            "last_seen_at": now,
        }
        for f in ("username", "first_name", "last_name"):
            update_set[f] = (
                stmt.excluded[f] if values.get(f) is not None
                else getattr(TelegramChatMember.__table__.c, f)
            )
        if has_started_bot is True:
            update_set["has_started_bot"] = True
        stmt = stmt.on_conflict_do_update(
            index_elements=["chat_id", "user_id"],
            set_=update_set,
        )
        session.execute(stmt)
        _enrich_team_member_row(
            session,
            user_id=int(user_id),
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        return

    # Generic fallback (SQLite tests). Flush pending writes first
    # so the per-row identity-map lookup actually sees a sibling
    # `add()` from the same session — without a flush a back-to-back
    # upsert against the same `(chat_id, user_id)` would emit two
    # INSERTs and trip the PK constraint.
    session.flush()
    existing = session.get(TelegramChatMember, (int(chat_id), int(user_id)))
    if existing is None:
        session.add(
            TelegramChatMember(
                chat_id=int(chat_id),
                user_id=int(user_id),
                username=username,
                first_name=first_name,
                last_name=last_name,
                has_started_bot=bool(has_started_bot),
                last_seen_at=now,
            )
        )
        session.flush()
        _enrich_team_member_row(
            session,
            user_id=int(user_id),
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        return
    if username is not None:
        existing.username = username
    if first_name is not None:
        existing.first_name = first_name
    if last_name is not None:
        existing.last_name = last_name
    if has_started_bot:
        existing.has_started_bot = True
    existing.last_seen_at = now
    session.flush()
    _enrich_team_member_row(
        session,
        user_id=int(user_id),
        username=username,
        first_name=first_name,
        last_name=last_name,
    )


def list_members_for_chat(
    session: Session, chat_id: int
) -> list[TelegramChatMember]:
    """Return every member ever seen in ``chat_id``."""
    rows = (
        session.execute(
            select(TelegramChatMember).where(
                TelegramChatMember.chat_id == int(chat_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def members_as_known_employees(
    session: Session, chat_id: int
) -> list[dict[str, str]]:
    """Build the ``known_employees`` list the intent pipeline
    expects. The schema is:

        [{"slack_user_id": "<numeric tg id>",
          "display_name":  "@username | First Last",
          "real_name":     "First Last"}]

    Even though the field is called ``slack_user_id`` for legacy
    reasons, the classifier doesn't care about the prefix shape; it
    treats it as «whatever id the model should round-trip back when
    it picks an owner». For TG members we feed the numeric user_id
    so a downstream resolution lands a value that
    `post_draft_confirmation` knows how to DM.
    """
    out: list[dict[str, str]] = []
    for m in list_members_for_chat(session, chat_id):
        full_name = " ".join(
            p for p in (m.first_name, m.last_name) if p
        ).strip()
        if m.username:
            display = f"@{m.username}"
        elif full_name:
            display = full_name
        else:
            display = str(m.user_id)
        out.append(
            {
                "slack_user_id": str(m.user_id),
                "display_name": display,
                "real_name": full_name or display,
            }
        )
    return out
