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


def get_humans_for_matcher(session: Session) -> list[dict]:
    """FR-CR-05-193d-3 — humans-only filter для Step 2 matcher prompt.

    Включает: active=True, real_name non-empty, не bot.
    Возвращает list[{tm_id, real_name, role, notes, tg_username, slack_user_id}].
    """
    rows = list(
        session.execute(
            select(TeamMember).where(TeamMember.active.is_(True))
        ).scalars().all()
    )
    out: list[dict] = []
    for tm in rows:
        rn = (tm.real_name or "").strip()
        if not rn:
            continue
        if "bot" in rn.lower() or "linkedin" in rn.lower():
            continue
        out.append({
            "tm_id": tm.id,
            "real_name": rn,
            "role": (tm.role or "").strip() or None,
            "notes": (tm.notes or "").strip() or None,
            "tg_username": getattr(tm, "telegram_username", None),
            "slack_user_id": getattr(tm, "slack_user_id", None),
        })
    return out


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

    FR-CR-05-60 — rows that USED to be in the sheet but are now
    missing get `active=False` (soft-deactivated). This stops
    the LLM owner picker from suggesting people the operator
    just removed from the team. The seen-id set is built across
    every match strategy so a row matched by `id`, by
    `telegram_user_id`, or by `slack_user_id` all count as
    «still in sheet».
    """
    updated = 0
    inserted = 0
    seen_team_ids: set[int] = set()
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
            new_member = TeamMember(**new_values)
            session.add(new_member)
            session.flush()
            seen_team_ids.add(new_member.id)
            inserted += 1
        else:
            seen_team_ids.add(target.id)
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

    # FR-CR-05-60 — soft-deactivate every team member that we
    # didn't see in this pull. Operator removed them from the
    # sheet → they shouldn't appear in `known_employees` anymore.
    # We DON'T hard-delete: historical task rows still reference
    # them via `owner_user_id`, and an inactive member can be
    # re-activated by adding the row back to the sheet.
    if seen_team_ids:
        deactivated_q = (
            session.query(TeamMember)
            .filter(TeamMember.active.is_(True))
            .filter(~TeamMember.id.in_(seen_team_ids))
        )
        for stale in deactivated_q.all():
            stale.active = False
            stale.last_synced_at = now
            updated += 1

    if inserted or updated:
        session.flush()
    return updated, inserted


def _parse_bool(s: str) -> bool:
    return (s or "").strip().lower() in {"true", "1", "yes", "y", "да", "+"}


# --------------------------------------------------------------------------- #
# FR-CR-05-142 — never-null owner: pipeline-side fallback picker.
# --------------------------------------------------------------------------- #

# Notes/role keywords that mark a teammate as the «principal»
# (CEO / founder / decision-maker) — pipeline falls back to the
# principal among present participants when the LLM emits
# owner=null. Match is case-insensitive substring.
PRINCIPAL_NOTE_MARKERS: tuple[str, ...] = (
    "principal",
    "ceo",
    "founder",
    "руководитель",
    "руководит",
    "основатель",
    "генеральный",
)

# Notes-clause prefix the operator uses to forbid a teammate
# from a domain («не вести fundraising-задачи», «не участвует в
# Fundrising sync»). Pipeline + prompt both honour this for
# FR-CR-05-142b. Match is case-insensitive substring; we look
# for the prefix AND a topic keyword in the same notes block.
NOTES_FORBIDS_PREFIX: tuple[str, ...] = (
    "не вести",
    "не назначать",
    "не участвует",
    "не участвую",
    "не присутствует",
    "не ходит",
    "do not assign",
    "do not own",
    "doesn't attend",
    "does not attend",
    "not in fundrais",
    "not in fundrais",
)


def _employee_forbids_topic(notes: str, topic_keywords: list[str]) -> bool:
    """Return True iff `notes` contains a «не вести X»-style
    clause where X overlaps with any keyword in `topic_keywords`.
    Used by `pick_meeting_owner_fallback` to skip teammates whose
    own notes forbid the meeting/task domain.
    """
    if not notes or not topic_keywords:
        return False
    n = notes.lower()
    for prefix in NOTES_FORBIDS_PREFIX:
        if prefix not in n:
            continue
        # Look for keyword in the same notes (a teammate with
        # ANY «не вести fundraising» line is forbidden from
        # fundraising tasks; we don't try to parse the clause
        # boundaries — operator's notes are short).
        for kw in topic_keywords:
            if kw and kw.lower() in n:
                return True
    return False


import re as _re

_DELEGATE_MARKER_RE = _re.compile(
    r"DELEGATE_TASKS_TO:\s*([^.\n]+)", _re.IGNORECASE,
)


def apply_delegate_marker(
    owner_user_id: str | None,
    known_employees: list[dict],
) -> tuple[str | None, str | None]:
    """FR-CR-05-192r — operator-pinned delegate chain.

    If ``owner_user_id`` resolves to a teammate whose ``notes`` field
    carries a ``DELEGATE_TASKS_TO: <real_name>`` marker, swap to the
    delegate's ``slack_user_id``. Single-hop, case-insensitive,
    silently no-op when the delegate target isn't itself in
    ``known_employees`` (operator-pinned sanity guard from
    FR-CR-05-192k: never typo into the void).

    Returns ``(new_owner_user_id, delegate_real_name_or_None)``. The
    second element lets callers tag their owner-resolution trace
    with the delegate's name without re-looking-up.
    """
    if not owner_user_id or not known_employees:
        return owner_user_id, None
    src_employee = None
    for e in known_employees:
        if e.get("slack_user_id") == owner_user_id:
            src_employee = e
            break
    if src_employee is None:
        return owner_user_id, None
    notes = src_employee.get("notes") or ""
    m = _DELEGATE_MARKER_RE.search(notes)
    if not m:
        return owner_user_id, None
    delegate_name = m.group(1).strip()
    delegate_norm = delegate_name.lower()
    for e2 in known_employees:
        if (e2.get("real_name") or "").lower() == delegate_norm:
            new_id = e2.get("slack_user_id")
            if new_id:
                return new_id, e2.get("real_name") or delegate_name
            # Delegate exists but has no slack_user_id — surface still
            # by name, leave owner_user_id intact for the caller's
            # fallback chain.
            return owner_user_id, None
    return owner_user_id, None


def employee_forbids_topic(notes: str, topic_keywords: list[str]) -> bool:
    """Public alias of `_employee_forbids_topic`. Kept stable
    for FR-CR-05-145 — pipeline-level participants post-filter
    needs to import this and the leading-underscore name is
    fragile in import lists."""
    return _employee_forbids_topic(notes, topic_keywords)


def filter_participants_by_notes_forbid(
    participants_real_names: list[str],
    *,
    known_employees: list[dict[str, str]],
    topic_keywords: list[str],
) -> tuple[list[str], list[str]]:
    """FR-CR-05-145 — Python-side defense for the notes-forbids
    rule. Even when the LLM `extract_zoom_participants_via_llm`
    ignores the «не участвует в X» clause, drop forbidden
    teammates from the resulting participants list.

    Returns `(kept, dropped)`. `topic_keywords` is the output
    of `infer_topic_keywords_from_text` against meeting title +
    transcript / detailed summary excerpts. Empty
    `topic_keywords` → no filtering (returns input as-is).
    """
    if not topic_keywords or not participants_real_names:
        return list(participants_real_names), []
    notes_by_name: dict[str, str] = {}
    for e in known_employees:
        rn = (e.get("real_name") or "").strip()
        if rn:
            notes_by_name[rn] = e.get("notes") or ""
    kept: list[str] = []
    dropped: list[str] = []
    for p in participants_real_names:
        notes = notes_by_name.get((p or "").strip(), "")
        if _employee_forbids_topic(notes, topic_keywords):
            dropped.append(p)
        else:
            kept.append(p)
    return kept, dropped


def _is_admin_uid(uid: str | None) -> bool:
    """True if `uid` is in TELEGRAM_ADMIN_USER_IDS."""
    if not uid:
        return False
    try:
        from app.telegram_bot.handlers import admin_user_ids

        return uid in admin_user_ids()
    except Exception:  # noqa: BLE001
        return False


def pick_meeting_owner_fallback(
    *,
    known_employees: list[dict[str, str]],
    participants_real_names: list[str],
    topic_keywords: list[str] | None = None,
) -> str | None:
    """FR-CR-05-142 — pipeline-side never-null owner fallback.

    Cascade (matches the prompt's Rule 5d / 5e):

      1. PRINCIPAL among `participants_real_names` whose notes /
         role mark them as principal/CEO/founder AND whose notes
         do NOT forbid the topic.
      2. First participant (in `participants_real_names` order)
         whose notes do NOT forbid the topic.
      3. First participant unconditionally (last resort).
      4. None — only when `participants_real_names` is empty.

    NEVER picks the admin/AI Lead row (FR-CR-05-134) — admin uids
    are filtered out of all candidate sets.

    `topic_keywords` (optional): lowercase strings like
    `["fundraising","ir","investor"]`. When a candidate's notes
    contain a `не вести X` / `do not assign` clause matching any
    keyword, that candidate is skipped (FR-CR-05-142b).
    """
    if not known_employees or not participants_real_names:
        return None
    topic_keywords = topic_keywords or []
    by_name: dict[str, dict[str, str]] = {}
    for e in known_employees:
        rn = (e.get("real_name") or "").strip()
        if rn:
            by_name[rn] = e
    present: list[dict[str, str]] = []
    for n in participants_real_names:
        if not n:
            continue
        e = by_name.get(n.strip())
        if not e:
            continue
        if _is_admin_uid(e.get("slack_user_id")):
            continue
        present.append(e)
    if not present:
        return None
    # Pass 1: principal not forbidden.
    for e in present:
        notes = (e.get("notes") or "").lower()
        role = (e.get("role") or "").lower()
        is_principal = any(
            mk in notes or mk in role for mk in PRINCIPAL_NOTE_MARKERS
        )
        if is_principal and not _employee_forbids_topic(
            e.get("notes") or "", topic_keywords
        ):
            return e.get("slack_user_id")
    # Pass 2: any present teammate not forbidden.
    for e in present:
        if not _employee_forbids_topic(
            e.get("notes") or "", topic_keywords
        ):
            return e.get("slack_user_id")
    # Pass 3: last resort — first present (even if forbidden,
    # better than null per operator's «всегда ответственный»).
    return present[0].get("slack_user_id")


def infer_topic_keywords_from_text(text: str) -> list[str]:
    """Best-effort topic keyword extraction from a meeting title /
    description / task title. Returns lowercase keywords used by
    `pick_meeting_owner_fallback` for FR-CR-05-142b
    notes-forbids-domain checks.

    Conservative — operator-driven topic taxonomy. If a meeting
    falls outside these buckets, returns []; the cascade then
    skips the «forbids-topic» filter entirely.
    """
    if not text:
        return []
    t = text.lower()
    out: set[str] = set()
    fundraising_markers = (
        "fundraising", "fundrais", "fundrising", "ir",
        "investor", "инвест", "раунд", "round", "first close",
        "эксклюзив", "term sheet", "термшит",
        "fund close", "fund-close", "private fund",
    )
    research_markers = (
        "research", "data analysis", "dashboard", "аналитик",
        "репортинг", "reporting",
    )
    if any(m in t for m in fundraising_markers):
        out.add("fundraising")
        out.add("fundrais")
        out.add("fundrising")
        out.add("ir")
        out.add("investor")
        out.add("инвест")
    if any(m in t for m in research_markers):
        out.add("research")
    return sorted(out)


def names_with_first_name(
    known_employees: list[dict[str, str]], first_name: str
) -> list[str]:
    """Return real_names whose first token matches `first_name`
    (case-insensitive, supports «Дима»/«Дмитрий» short-form via
    common-prefix). Used by self-name-in-task disambiguation
    (FR-CR-05-142a, Rule 5b).
    """
    if not first_name or not known_employees:
        return []
    target = first_name.strip().lower()
    if not target:
        return []
    out: list[str] = []
    for e in known_employees:
        rn = (e.get("real_name") or "").strip()
        if not rn:
            continue
        head = rn.split()[0].lower()
        if head == target:
            out.append(rn)
            continue
        # Short-form common-prefix («Дима» ↔ «Дмитрий»; require
        # ≥3 chars overlap to keep this conservative).
        if len(target) >= 3 and len(head) >= 3 and (
            head.startswith(target) or target.startswith(head)
        ):
            out.append(rn)
    return out
