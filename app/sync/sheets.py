from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger
from app.models import Employee, GoogleSheetsSync, SyncStatus, Task

_MENTION_RE = re.compile(r"^<@([UW][A-Z0-9]+)>$")

log = get_logger(__name__)

def _col_letter(n: int) -> str:
    """1-indexed column index → A1-style letter. 1→'A', 22→'V',
    27→'AA'. The schema currently has ≤26 columns so the simple
    ASCII path is enough; the algorithm handles 27+ for future-
    proofing."""
    if n < 1:
        raise ValueError(f"column index must be ≥1, got {n}")
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


_HEADER_ROW = [
    "task_id",
    "title",
    "description",
    "owner",
    "priority",
    "category",
    "start_date",
    "start_time",
    "due_date",
    "due_time",
    "is_recurring",
    "recurring_weekdays",
    "recurring_start_time",
    "recurring_end_time",
    "status",
    "parent_task_id",
    "source",
    "source_permalink",
    "created_at",
    "updated_at",
    "deleted_at",
    "completion_artifact",
]


def _resolve_owner_name(session: Session | None, task: Task) -> str:
    """Pick the most human-readable owner string for the spreadsheet.

    Order of preference:
    1. Employee.real_name / display_name (looked up by `owner_user_id`).
       Real name first because Slack's `display_name` often falls back
       to the @username (e.g. "admin"), while `real_name_normalized`
       almost always carries the actual person's name.
    2. `owner_display_name`, with a leading `<@Uxxx>` Slack mention
       stripped to a bare uid (so the cell never shows raw mention syntax).
    3. `owner_user_id` as last resort.
    """
    if task.owner_user_id and session is not None:
        emp = session.get(Employee, task.owner_user_id)
        if emp is not None:
            name = emp.real_name or emp.display_name
            if name:
                return name
    raw = (task.owner_display_name or "").strip()
    m = _MENTION_RE.match(raw)
    if m:
        return m.group(1)  # bare uid — better than "<@Uxxx>"
    if raw:
        return raw
    return task.owner_user_id or ""


def _task_row(task: Task, *, session: Session | None = None) -> list[str]:
    # Soft-deleted tasks: keep the row in the sheet but flip status to
    # "deleted" so the user sees what happened. The `deleted_at`
    # timestamp carries the audit info.
    status_text = "deleted" if task.deleted_at is not None else task.status.value
    # FR-CR-04-26 / FR-CR-05-* — surface the source channel
    # (slack / telegram) so a glance at the sheet shows where each
    # task came from. Falls back to the enum's value as plain text.
    source_text = task.source_kind.value if task.source_kind else "slack"
    return [
        str(task.id),
        task.title,
        task.description or "",
        _resolve_owner_name(session, task),
        task.priority.value,
        task.category or "",
        task.start_date.isoformat() if task.start_date else "",
        task.start_time.strftime("%H:%M") if task.start_time else "",
        task.due_date.isoformat() if task.due_date else "",
        task.due_time.strftime("%H:%M") if task.due_time else "",
        "yes" if task.is_recurring else "",
        ",".join(task.recurring_weekdays) if task.recurring_weekdays else "",
        task.recurring_start_time.strftime("%H:%M") if task.recurring_start_time else "",
        task.recurring_end_time.strftime("%H:%M") if task.recurring_end_time else "",
        status_text,
        str(task.parent_task_id) if task.parent_task_id else "",
        source_text,
        task.source_permalink or "",
        task.created_at.isoformat() if task.created_at else "",
        task.updated_at.isoformat() if task.updated_at else "",
        task.deleted_at.isoformat() if task.deleted_at else "",
        task.completion_artifact or "",
    ]


class SheetsSyncService:
    """Create/update a row in Google Sheets for each Task.

    Sync is idempotent per task: on first sync we append a row and remember
    row_id; subsequent syncs update the same row.
    """

    def __init__(
        self,
        *,
        credentials: Credentials,
        spreadsheet_id: str,
        sheet_name: str = "Main",
    ) -> None:
        # `Credentials` here is the type from google.oauth2.credentials, but
        # google.oauth2.service_account.Credentials also satisfies the
        # signed-request protocol — googleapiclient accepts both.
        self._service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name
        # Headers are written at most once per process; flip after the
        # first call to `_ensure_headers()` so we don't hit Sheets on
        # every sync.
        self._headers_checked = False

    def _ensure_headers(self) -> None:
        """Write our header row to row 1 if it's missing or doesn't match
        the current schema.

        This costs one extra GET per process. After the first call we
        flip a flag so subsequent syncs skip it. If the existing headers
        already match `_HEADER_ROW`, we don't touch them; if they
        differ, we overwrite row 1 — the bot's column order is the
        source of truth for the sheet.
        """
        if getattr(self, "_headers_checked", False):
            return
        self._headers_checked = True
        if getattr(self, "_service", None) is None:
            return  # tests inject `_service=None`; nothing to call
        end_col = _col_letter(len(_HEADER_ROW))
        rng = f"{self._sheet_name}!A1:{end_col}1"
        try:
            resp = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._spreadsheet_id, range=rng)
                .execute()
            )
            current = (resp.get("values") or [[]])[0]
            if current == _HEADER_ROW:
                return
            self._service.spreadsheets().values().update(
                spreadsheetId=self._spreadsheet_id,
                range=rng,
                valueInputOption="RAW",
                body={"values": [_HEADER_ROW]},
            ).execute()
            log.info("sheets_headers_written", sheet=self._sheet_name)
        except HttpError as e:
            log.warning("sheets_headers_write_failed", error=str(e))

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _append(self, row: list[str]) -> dict[str, Any]:
        # FR-CR-05-86 — operator regression: new rows landed
        # shifted ~22 columns to the right (data starting at
        # column W/X instead of A). Root cause: range=A:Z (26
        # cols) plus legacy data in columns W-Z (the rolled-back
        # `dialogue` column from before FR-CR-05-15) made
        # Google's append heuristic detect the "table" as wider
        # than 22 columns and place new rows past the schema.
        # Pinning the range to the EXACT schema width forces
        # Sheets to ignore stray content in W-Z.
        end_col = _col_letter(len(_HEADER_ROW))
        return (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A:{end_col}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            .execute()
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _update(self, row_id: int, row: list[str]) -> dict[str, Any]:
        end_col = _col_letter(len(_HEADER_ROW))
        return (
            self._service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A{row_id}:{end_col}{row_id}",
                valueInputOption="RAW",
                body={"values": [row]},
            )
            .execute()
        )

    def sync(self, session: Session, task: Task) -> GoogleSheetsSync:
        # Make sure the header row exists / matches the current schema.
        # No-op after the first call per process.
        self._ensure_headers()

        record = (
            session.query(GoogleSheetsSync).filter_by(task_id=task.id).one_or_none()
        )
        if record is None:
            record = GoogleSheetsSync(
                task_id=task.id,
                spreadsheet_id=self._spreadsheet_id,
                status=SyncStatus.pending,
            )
            session.add(record)
            session.flush()

        row = _task_row(task, session=session)
        record.attempts += 1
        try:
            if record.row_id is None:
                resp = self._append(row)
                # updatedRange looks like "Tasks!A12:J12"; parse the row number
                updated_range = resp.get("updates", {}).get("updatedRange", "")
                row_id = _parse_row_id(updated_range)
                record.row_id = row_id
                task.google_sheets_row_id = row_id
            else:
                self._update(record.row_id, row)

            record.status = SyncStatus.success
            record.last_error = None
            record.last_synced_at = datetime.now(timezone.utc)
        except HttpError as e:
            record.status = SyncStatus.failed
            record.last_error = str(e)
            log.warning("sheets_sync_failed", task_id=task.id, error=str(e))
            raise
        finally:
            session.flush()
        return record


def _parse_row_id(updated_range: str) -> int | None:
    # Example: "Tasks!A12:J12" → 12
    try:
        _, cells = updated_range.split("!", 1)
        start = cells.split(":", 1)[0]
        digits = "".join(ch for ch in start if ch.isdigit())
        return int(digits) if digits else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# FR-CR-05-11 — Sheet → DB pull (operator edits propagate back)
# --------------------------------------------------------------------------- #


# Fields the operator can edit on the spreadsheet and have those edits
# applied to the DB on the next pull. Anything not listed here is
# read-only from the sheet's side (changes are silently ignored).
_EDITABLE_FIELDS: set[str] = {
    "title",
    "description",
    "owner",
    "priority",
    "category",
    "start_date",
    "start_time",
    "due_date",
    "due_time",
    "status",
    "completion_artifact",
}


def _parse_date_or_none(s: str | None):
    from datetime import date as _date

    if not s:
        return None
    try:
        return _date.fromisoformat(s.strip())
    except ValueError:
        return None


def _parse_time_or_none(s: str | None):
    from datetime import time as _time

    if not s:
        return None
    try:
        hh, mm = s.strip().split(":")[:2]
        return _time(int(hh), int(mm))
    except (ValueError, IndexError):
        return None


def _resolve_owner_back_to_id(
    session: Session, raw: str
) -> tuple[str | None, str | None]:
    """FR-CR-05-11 — operator types an owner name on the sheet; we
    map it back to a user id for the DB. Strategy:

      1. Bare uid (`U…` or all-digits) → keep as-is.
      2. `@handle` → match against `team_members.telegram_username`.
      3. Display / real name → match against
         `team_members.real_name` / Slack `employees.real_name`.
      4. Otherwise → owner_user_id stays None, display_name keeps
         the raw text so the operator's intent isn't lost.

    Returns ``(owner_user_id, owner_display_name)``."""
    s = (raw or "").strip()
    if not s:
        return None, None
    # Bare uid?
    if s.startswith("U") or s.startswith("W") or s.lstrip("-").isdigit():
        return s, None
    # @handle?
    if s.startswith("@"):
        handle = s[1:].lower()
        try:
            from app.models import TeamMember

            row = (
                session.query(TeamMember)
                .filter(TeamMember.telegram_username.ilike(handle))
                .first()
            )
            if row is not None:
                if row.telegram_user_id is not None:
                    return str(row.telegram_user_id), s
                if row.slack_user_id:
                    return row.slack_user_id, s
        except Exception:  # noqa: BLE001
            pass
    # Real / display name match.
    try:
        from app.models import TeamMember

        row = (
            session.query(TeamMember)
            .filter(TeamMember.real_name.ilike(s))
            .first()
        )
        if row is not None:
            if row.telegram_user_id is not None:
                return str(row.telegram_user_id), s
            if row.slack_user_id:
                return row.slack_user_id, s
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.models import Employee

        row = (
            session.query(Employee)
            .filter(Employee.real_name.ilike(s))
            .first()
        )
        if row is not None:
            return row.slack_user_id, s
    except Exception:  # noqa: BLE001
        pass
    # Couldn't resolve — keep the typed text as display_name so the
    # operator sees their input on the next push.
    return None, s


def _apply_sheet_row(
    session: Session, task: Task, row: dict[str, str]
) -> dict[str, str]:
    """Diff one sheet row against the DB Task. Returns a dict of
    fields that actually changed (for logging). Status changes are
    routed through `TransitionService` so audit + subscribers fire."""
    from app.models import TaskPriority, TaskStatus
    from app.services.transitions import InvalidTransition, TransitionService

    changes: dict[str, str] = {}

    new_title = (row.get("title") or "").strip()
    if new_title and new_title != (task.title or ""):
        task.title = new_title[:_MAX_TITLE_CHARS]
        changes["title"] = new_title

    if "description" in row:
        new_desc = (row.get("description") or "").strip() or None
        if new_desc != (task.description or None):
            task.description = new_desc[:_MAX_DESCRIPTION_CHARS] if new_desc else None
            changes["description"] = new_desc or ""

    if "owner" in row:
        raw = row.get("owner") or ""
        new_uid, new_display = _resolve_owner_back_to_id(session, raw)
        # Compare against the rendered cell — that's what the operator
        # sees, so a no-op edit shouldn't fire a diff.
        old_render = _resolve_owner_name(session, task)
        if (raw.strip() or "") != (old_render or ""):
            task.owner_user_id = new_uid
            task.owner_display_name = new_display
            changes["owner"] = raw.strip()

    if "priority" in row:
        raw = (row.get("priority") or "").strip().lower()
        if raw and raw != task.priority.value:
            try:
                task.priority = TaskPriority(raw)
                changes["priority"] = raw
            except ValueError:
                log.info(
                    "sheet_pull_invalid_priority",
                    task_id=task.id,
                    raw=raw,
                )

    if "category" in row:
        cat = (row.get("category") or "").strip() or None
        if cat != (task.category or None):
            task.category = cat
            changes["category"] = cat or ""

    if "start_date" in row:
        d = _parse_date_or_none(row.get("start_date"))
        if d != task.start_date:
            task.start_date = d
            changes["start_date"] = d.isoformat() if d else ""

    if "start_time" in row:
        t = _parse_time_or_none(row.get("start_time"))
        if t != task.start_time:
            task.start_time = t
            changes["start_time"] = t.strftime("%H:%M") if t else ""

    if "due_date" in row:
        d = _parse_date_or_none(row.get("due_date"))
        if d != task.due_date:
            task.due_date = d
            changes["due_date"] = d.isoformat() if d else ""

    if "due_time" in row:
        t = _parse_time_or_none(row.get("due_time"))
        if t != task.due_time:
            task.due_time = t
            changes["due_time"] = t.strftime("%H:%M") if t else ""

    if "completion_artifact" in row:
        art = (row.get("completion_artifact") or "").strip() or None
        if art != (task.completion_artifact or None):
            task.completion_artifact = art
            changes["completion_artifact"] = art or ""

    if "status" in row:
        raw = (row.get("status") or "").strip().lower()
        if raw and raw != "deleted" and raw != task.status.value:
            try:
                target = TaskStatus(raw)
            except ValueError:
                log.info(
                    "sheet_pull_invalid_status", task_id=task.id, raw=raw
                )
                return changes
            try:
                TransitionService().apply(
                    session,
                    task=task,
                    new_status=target,
                    actor_slack_user_id="sheet_sync",
                    reason="sheet_edit",
                )
                changes["status"] = raw
            except InvalidTransition as e:
                log.info(
                    "sheet_pull_invalid_transition",
                    task_id=task.id,
                    error=str(e),
                )

    return changes


# Field-level caps for safety. Keep matched to TaskDraft._MAX_FIELD_CHARS.
_MAX_TITLE_CHARS = 10_000
_MAX_DESCRIPTION_CHARS = 10_000


class SheetsPullService:
    """FR-CR-05-11 — read every row from the Tasks sheet and apply
    field-level diffs back to the corresponding DB Task.

    Authoritative direction: the Sheet wins. An operator's manual
    edit always propagates to the DB on the next pull tick. The
    DB → Sheet push (via `SheetsSyncService.sync`) keeps running
    on every Task change as before, so the Sheet stays current
    when the bot makes changes too.

    Conflict window: at most one tick-interval (~5 min by default).
    No `updated_at` comparison — keeping the rule simple beats
    fighting clock skew between the bot and the operator's edits.
    """

    def __init__(
        self,
        *,
        credentials: Credentials,
        spreadsheet_id: str,
        sheet_name: str = "Main",
    ) -> None:
        self._service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _read_all(self) -> list[list[str]]:
        end_col = chr(ord("A") + len(_HEADER_ROW) - 1)
        rng = f"{self._sheet_name}!A1:{end_col}"
        resp = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=rng)
            .execute()
        )
        return list(resp.get("values") or [])

    def pull(self, session: Session) -> tuple[int, int, int]:
        """Read every row from the sheet, diff against DB, apply
        edits in place. Returns ``(rows_seen, rows_changed,
        rows_skipped)``.

        Skipped rows: missing / non-numeric `task_id`; task already
        soft-deleted; task not found in DB.
        """
        rows = self._read_all()
        if not rows:
            return 0, 0, 0
        # Drop header.
        if rows and rows[0] and rows[0][0].strip().lower() == "task_id":
            rows = rows[1:]

        seen = changed = skipped = 0
        for cells in rows:
            seen += 1
            cells = list(cells) + [""] * (len(_HEADER_ROW) - len(cells))
            row_dict = dict(zip(_HEADER_ROW, cells))
            tid_raw = (row_dict.get("task_id") or "").strip()
            if not tid_raw.isdigit():
                skipped += 1
                continue
            task = session.get(Task, int(tid_raw))
            if task is None or task.deleted_at is not None:
                skipped += 1
                continue
            diffs = _apply_sheet_row(session, task, row_dict)
            if diffs:
                changed += 1
                log.info(
                    "sheet_pull_applied",
                    task_id=task.id,
                    fields=list(diffs.keys()),
                )
        if changed:
            session.flush()
        return seen, changed, skipped


__all__ = ["SheetsSyncService", "SheetsPullService"]
