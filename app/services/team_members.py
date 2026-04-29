"""FR-CR-05-10 — Cross-channel team registry service.

Reads / writes the `team_members` table that backs the LLM owner
stage. The table is the AUTHORITATIVE source for «who can be
assigned a task» — the operator owns it via the `Team` Google
Sheet tab and the bot validates every extracted owner against it.

Key callers:

- `TelegramIngestService` → `as_known_employees(session)` to
  populate the LLM's owner-resolution candidate list.
- `app/sync/team_sheet.py` → `pull_from_rows()` / `to_sheet_rows()`
  for bidirectional sync with the spreadsheet.
- The auto-seed step on first sync calls
  `seed_from_chat_members()` and `seed_from_slack_employees()` so
  the operator doesn't start from a blank sheet.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Employee, TeamMember, TelegramChatMember

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Read paths
# --------------------------------------------------------------------------- #


def list_active(session: Session) -> list[TeamMember]:
    """Return every active team member."""
    return list(
        session.execute(
            select(TeamMember).where(TeamMember.active.is_(True))
        )
        .scalars()
        .all()
    )


def list_all(session: Session) -> list[TeamMember]:
    return list(
        session.execute(select(TeamMember)).scalars().all()
    )


def _display_for(m: TeamMember) -> str:
    """Pick the most operator-friendly label for a member."""
    if m.telegram_username:
        return f"@{m.telegram_username}"
    if m.real_name:
        return m.real_name
    if m.slack_user_id:
        return m.slack_user_id
    if m.telegram_user_id is not None:
        return str(m.telegram_user_id)
    return f"team#{m.id}"


def as_known_employees(
    session: Session, *, prefer_telegram: bool = True
) -> list[dict[str, str]]:
    """Build the `known_employees` list the intent pipeline expects.
    Each row is shaped:

        {"slack_user_id": "<numeric TG id or Slack uid>",
         "display_name":  "@handle | Real Name",
         "real_name":     "Real Name",
         "role":          "Project Manager / аналитик" or "",
         "notes":         "free-form context" or ""}

    The field is named `slack_user_id` for legacy reasons — the
    classifier treats it as opaque «id the LLM should round-trip
    back when it picks an owner». When ``prefer_telegram=True``
    (the default for the TG ingest) we use the numeric Telegram
    user_id so the post-classification handler can DM the assignee.
    On Slack ingest the caller flips the flag and we feed
    slack_user_id instead.

    Role + notes are surfaced because they help the LLM
    disambiguate when several team members share a first name
    («Алина» vs «Валентина» both match a partial cue) — the role
    text often carries enough signal («Project Manager / аналитик»
    vs «founder») to pick the right one.

    Inactive members are excluded (they're kept in the table for
    historical task assignments but shouldn't appear as new owner
    candidates).
    """
    out: list[dict[str, str]] = []
    for m in list_active(session):
        # Pick the id field that matches the channel we're routing
        # against. When the preferred id is missing on this row we
        # gracefully fall back to the other one — beats dropping
        # the row entirely.
        primary_id: str | None = None
        if prefer_telegram and m.telegram_user_id is not None:
            primary_id = str(m.telegram_user_id)
        elif not prefer_telegram and m.slack_user_id:
            primary_id = m.slack_user_id
        elif m.slack_user_id:
            primary_id = m.slack_user_id
        elif m.telegram_user_id is not None:
            primary_id = str(m.telegram_user_id)
        if not primary_id:
            continue
        out.append(
            {
                "slack_user_id": primary_id,
                "display_name": _display_for(m),
                "real_name": m.real_name or _display_for(m),
                "role": (m.role or ""),
                "notes": (m.notes or ""),
            }
        )
    return out


def find_by_telegram_user_id(
    session: Session, telegram_user_id: int
) -> TeamMember | None:
    return (
        session.execute(
            select(TeamMember).where(TeamMember.telegram_user_id == telegram_user_id)
        )
        .scalars()
        .first()
    )


def find_by_slack_user_id(
    session: Session, slack_user_id: str
) -> TeamMember | None:
    return (
        session.execute(
            select(TeamMember).where(TeamMember.slack_user_id == slack_user_id)
        )
        .scalars()
        .first()
    )


# --------------------------------------------------------------------------- #
# Auto-seed
# --------------------------------------------------------------------------- #


def seed_from_chat_members(session: Session) -> int:
    """Pull every distinct (telegram_user_id, profile) out of the
    chat-members registry and create a team_members row when one
    doesn't already exist. Returns the count of rows added.

    Same person speaking in multiple chats deduplicates: only the
    first observation wins (later observations may have richer
    profile data, but the Sheet-driven sync is authoritative for
    edits, not re-import).
    """
    rows = (
        session.execute(select(TelegramChatMember))
        .scalars()
        .all()
    )
    by_user: dict[int, TelegramChatMember] = {}
    for r in rows:
        existing = by_user.get(int(r.user_id))
        if existing is None or (r.username and not existing.username):
            by_user[int(r.user_id)] = r
    added = 0
    for uid, m in by_user.items():
        if find_by_telegram_user_id(session, uid) is not None:
            continue
        full_name = " ".join(
            p for p in (m.first_name, m.last_name) if p
        ).strip() or None
        is_bot = _looks_like_bot(full_name, m.username)
        session.add(
            TeamMember(
                telegram_user_id=uid,
                telegram_username=m.username,
                real_name=full_name,
                # FR-CR-05-13 — bot rows default inactive so they
                # never appear in the owner-candidate list.
                active=not is_bot,
                notes="auto: looks like bot account" if is_bot else None,
                last_synced_at=datetime.now(timezone.utc),
            )
        )
        added += 1
    if added:
        session.flush()
    return added


def _looks_like_bot(name: str | None, username: str | None) -> bool:
    """Heuristic: a row is a bot account when its name or username
    has obvious bot markers. Used at seed time to mark such rows
    `active=False` so they never appear in the LLM's owner-
    candidate list.

    Patterns we catch:
      - explicit `bot` / `_bot` suffix (e.g. `CEO_office1 bot`)
      - `bot` substring with a separator on either side
      - common bot prefixes: `office1`, `notif`, `support`,
        `assistant`, `webhook`, `crm`
    Conservative — better to leave a real person flagged inactive
    (operator can flip it on the sheet) than to leave a bot active
    and end up with «CEO_office1 bot» as task owner again.
    """
    blob = " ".join(filter(None, [name, username])).lower()
    if not blob:
        return False
    if " bot" in blob or blob.endswith("bot") or "_bot" in blob:
        return True
    for marker in ("office1", "notif", "support_", "assistant_", "webhook", "crm_"):
        if marker in blob:
            return True
    return False


def backfill_team_members_from_chat_members(session: Session) -> int:
    """FR-CR-05-23 — one-shot backfill that fills BLANK
    `telegram_username` / `real_name` fields on existing
    `team_members` rows from whatever the listener has captured
    in `telegram_chat_members`.

    The FR-CR-05-21 auto-enrich runs on every NEW observation;
    this pass takes care of existing rows that were seeded with
    only a numeric id but the same user has been observed (with a
    username) in `chat_members` from earlier traffic.

    Operator-edited values are NEVER overwritten — only blanks
    get filled. Returns the count of rows actually changed.
    """
    rows = (
        session.execute(select(TeamMember))
        .scalars()
        .all()
    )
    changed = 0
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.telegram_user_id is None:
            continue
        # Skip rows that are already populated.
        if r.telegram_username and r.real_name:
            continue
        # Most recent observation wins.
        chat_row = (
            session.query(TelegramChatMember)
            .filter(TelegramChatMember.user_id == int(r.telegram_user_id))
            .order_by(TelegramChatMember.last_seen_at.desc())
            .first()
        )
        if chat_row is None:
            continue
        row_changed = False
        if not r.telegram_username and chat_row.username:
            r.telegram_username = chat_row.username
            row_changed = True
        if not r.real_name:
            full = " ".join(
                p for p in (chat_row.first_name, chat_row.last_name) if p
            ).strip() or None
            if full:
                r.real_name = full
                row_changed = True
        if row_changed:
            r.last_synced_at = now
            changed += 1
    if changed:
        session.flush()
    return changed


def enrich_team_members_from_bot_api(session: Session, sender) -> int:
    """FR-CR-05-24 — for every `team_members` row that has a
    numeric `telegram_user_id` but a blank `telegram_username`,
    call Telegram Bot API `getChat(user_id)` and adopt the
    returned profile fields.

    Works only for users the bot has ever interacted with — the
    user has /started the bot, replied to a bot message, or is a
    member of a chat the bot is in. For never-seen users
    `getChat` returns `{}` and we leave the row alone.

    Operator-edited values are NEVER overwritten. Returns the
    count of rows actually changed.
    """
    if sender is None or not getattr(sender, "enabled", False):
        return 0
    rows = (
        session.execute(select(TeamMember))
        .scalars()
        .all()
    )
    changed = 0
    now = datetime.now(timezone.utc)
    for r in rows:
        if r.telegram_user_id is None:
            continue
        if r.telegram_username and r.real_name:
            continue
        try:
            info = sender.get_chat(chat_id=int(r.telegram_user_id))
        except Exception as e:  # noqa: BLE001
            log.info(
                "team_member_bot_api_enrich_failed",
                user_id=r.telegram_user_id,
                error=str(e),
            )
            continue
        if not isinstance(info, dict) or not info.get("id"):
            continue
        row_changed = False
        if not r.telegram_username and info.get("username"):
            r.telegram_username = info["username"]
            row_changed = True
        if not r.real_name:
            full = " ".join(
                p for p in (info.get("first_name"), info.get("last_name")) if p
            ).strip() or None
            if full:
                r.real_name = full
                row_changed = True
        if row_changed:
            r.last_synced_at = now
            changed += 1
    if changed:
        session.flush()
    return changed


def seed_from_telegram_source(session: Session, reader) -> int:
    """FR-CR-05-10 — pull every distinct sender out of the Supabase
    `humanoid_tg_chats_readonly` view (via the same reader the
    ingest uses) and insert a `team_members` row when one doesn't
    exist for that telegram_user_id.

    Differs from `seed_from_chat_members` in that it doesn't depend
    on the live listener having observed the user — works against
    the colleague's pre-existing message archive directly. Useful
    on first deploy when chat_members is empty but you want every
    historical sender bootstrapped into the registry.

    The view typically only has a combined `user_name` field (no
    first/last split). We store it as `real_name` so the operator
    sees something readable on the sheet. They'll likely polish
    the names by hand after the seed.
    """
    if reader is None or not getattr(reader, "configured", False):
        return 0
    try:
        users = reader.distinct_users()
    except Exception as e:  # noqa: BLE001
        log.warning("seed_from_telegram_source_failed", error=str(e))
        return 0
    added = 0
    enriched = 0
    now = datetime.now(timezone.utc)
    for u in users:
        uid = u.get("user_id")
        if uid is None:
            continue
        # FR-CR-05-25 — the view now ships a dedicated
        # `sender_username` column when present; otherwise fall
        # back to the legacy heuristic (treat ASCII-only no-space
        # `user_name` as a handle, anything else as a real name).
        explicit_username = (u.get("username") or "").strip().lstrip("@") or None
        name = (u.get("user_name") or "").strip() or None
        if explicit_username:
            username = explicit_username
            real_name = name
        elif name and " " not in name and name.replace("_", "").isalnum() and not name.isdigit():
            username = name
            real_name = None
        else:
            username = None
            real_name = name

        existing = find_by_telegram_user_id(session, int(uid))
        if existing is not None:
            # FR-CR-05-25 — backfill missing fields when re-seeding
            # against an enriched view. Operator-edited values are
            # NEVER overwritten — only blanks get filled.
            row_changed = False
            if not existing.telegram_username and username:
                existing.telegram_username = username
                row_changed = True
            if not existing.real_name and real_name:
                existing.real_name = real_name
                row_changed = True
            if row_changed:
                existing.last_synced_at = now
                enriched += 1
            continue
        is_bot = _looks_like_bot(real_name, username)
        session.add(
            TeamMember(
                telegram_user_id=int(uid),
                telegram_username=username,
                real_name=real_name,
                active=not is_bot,
                notes="auto: looks like bot account" if is_bot else None,
                last_synced_at=now,
            )
        )
        added += 1
    if added or enriched:
        session.flush()
    return added + enriched


def seed_from_slack_employees(session: Session) -> int:
    """Same idea for Slack — pull every Employee row, create a
    team_members entry when there isn't one yet."""
    rows = (
        session.execute(select(Employee))
        .scalars()
        .all()
    )
    added = 0
    for e in rows:
        if not e.slack_user_id or e.is_bot:
            continue
        if find_by_slack_user_id(session, e.slack_user_id) is not None:
            continue
        session.add(
            TeamMember(
                slack_user_id=e.slack_user_id,
                real_name=e.real_name or e.display_name,
                email=e.email,
                role=e.title,
                active=True,
                last_synced_at=datetime.now(timezone.utc),
            )
        )
        added += 1
    if added:
        session.flush()
    return added


# --------------------------------------------------------------------------- #
# Sheet round-trip
# --------------------------------------------------------------------------- #

# Header order is part of the sheet contract — `app/sync/team_sheet.py`
# writes / reads in this exact column order. Adding a new field?
# Append it — never reorder, or the next pull misaligns.
SHEET_HEADERS: list[str] = [
    "id",
    "real_name",
    "telegram_user_id",
    "telegram_username",
    "slack_user_id",
    "role",
    "email",
    "active",
    "notes",
]


def to_sheet_rows(session: Session) -> list[list[str]]:
    """Materialise the full registry as a list of cell lists,
    starting with the header row. Used by `push_to_sheet`."""
    out: list[list[str]] = [list(SHEET_HEADERS)]
    for m in list_all(session):
        out.append(
            [
                str(m.id) if m.id is not None else "",
                m.real_name or "",
                str(m.telegram_user_id) if m.telegram_user_id is not None else "",
                m.telegram_username or "",
                m.slack_user_id or "",
                m.role or "",
                m.email or "",
                "true" if m.active else "false",
                m.notes or "",
            ]
        )
    return out


def upsert_from_sheet_rows(
    session: Session, rows: Iterable[list[str]]
) -> tuple[int, int]:
    """Apply a sheet pull onto the DB. ``rows`` should NOT include
    the header. Returns ``(updated, inserted)`` counts.

    Match strategy in priority order:
      1. ``id`` column when populated and the row exists.
      2. ``telegram_user_id`` when populated.
      3. ``slack_user_id`` when populated.
      4. Otherwise insert as a new row.

    Empty-string cells are normalised to NULL. ``active`` parses
    truthy strings (`true`, `1`, `yes`, `да`).

    FR-CR-05-29 — when the new values for ``telegram_user_id`` or
    ``slack_user_id`` would conflict with a DIFFERENT row's
    UNIQUE column, the operator is clearly consolidating
    duplicates (e.g. an auto-seeded TG-only row + an auto-seeded
    Slack-only row for the same teammate). The orphan row is
    deleted so the merge lands instead of crashing on
    ``UniqueViolation``.
    """
    updated = 0
    inserted = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        cells = list(row) + [""] * (len(SHEET_HEADERS) - len(row))
        as_dict = dict(zip(SHEET_HEADERS, cells))
        id_str = (as_dict.get("id") or "").strip()
        tg_user_str = (as_dict.get("telegram_user_id") or "").strip()
        slack_id = (as_dict.get("slack_user_id") or "").strip() or None

        target: TeamMember | None = None
        if id_str.isdigit():
            target = session.get(TeamMember, int(id_str))
        if target is None and tg_user_str.lstrip("-").isdigit():
            target = find_by_telegram_user_id(session, int(tg_user_str))
        if target is None and slack_id:
            target = find_by_slack_user_id(session, slack_id)

        active = _parse_bool(as_dict.get("active") or "true")
        new_values = dict(
            real_name=(as_dict.get("real_name") or "").strip() or None,
            telegram_user_id=(int(tg_user_str) if tg_user_str.lstrip("-").isdigit() else None),
            telegram_username=(as_dict.get("telegram_username") or "").strip() or None,
            slack_user_id=slack_id,
            role=(as_dict.get("role") or "").strip() or None,
            email=(as_dict.get("email") or "").strip() or None,
            active=active,
            notes=(as_dict.get("notes") or "").strip() or None,
            last_synced_at=now,
        )

        # FR-CR-05-29 — clear any orphan row that owns one of the
        # UNIQUE columns we're about to set on the target. The
        # operator is consolidating; the orphan is the row that's
        # losing the merge.
        target_id = target.id if target is not None else None
        if new_values["telegram_user_id"] is not None:
            conflict = (
                session.query(TeamMember)
                .filter(
                    TeamMember.telegram_user_id == new_values["telegram_user_id"]
                )
                .filter(TeamMember.id != target_id)
                .first()
            )
            if conflict is not None:
                session.delete(conflict)
                session.flush()
        if new_values["slack_user_id"]:
            conflict = (
                session.query(TeamMember)
                .filter(TeamMember.slack_user_id == new_values["slack_user_id"])
                .filter(TeamMember.id != target_id)
                .first()
            )
            if conflict is not None:
                session.delete(conflict)
                session.flush()

        if target is None:
            session.add(TeamMember(**new_values))
            inserted += 1
        else:
            # FR-CR-05-30 — only count + write when an actual data
            # field changed. Without this guard the listener's
            # 60-second poll wrote `last_synced_at=now` to all 54
            # rows on every tick and reported `updated=53` even
            # when the operator didn't touch anything. Compare
            # field-by-field; bump `last_synced_at` only when at
            # least one real field actually moved.
            real_diff = False
            for k, v in new_values.items():
                if k == "last_synced_at":
                    continue
                if getattr(target, k) != v:
                    setattr(target, k, v)
                    real_diff = True
            if real_diff:
                target.last_synced_at = now
                updated += 1
    if inserted or updated:
        session.flush()
    return updated, inserted


def _parse_bool(s: str) -> bool:
    return (s or "").strip().lower() in {"true", "1", "yes", "y", "да", "+"}
