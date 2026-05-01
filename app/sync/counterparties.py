"""FR-CR-05-124 — pull counterparties from two Google Sheets
into the `counterparties` hub + `counterparty_attrs` satellite.

Two source patterns:

A) **Status outreach** (single tab on the investor sheet):
   - Column A: `type` (operator-defined category, e.g.
     «investor», «client», «partner»).
   - Column B: `name` (canonical counterparty name).
   - All other columns: captured into the satellite's JSONB
     payload using the header-row labels as keys.

B) **Outreach / Rejections / Looking for intros** (three tabs
   on a separate sheet):
   - Column A: `name` (canonical name, only field operator uses).
   - The TAB NAME becomes the `type` (so an entry in the
     «Rejections» tab gets `type="Rejections"`).
   - Other columns are captured into the satellite when present.

Sync semantics: **wipe-and-replace**. Each pull deletes every
row in `counterparties` (cascades to `counterparty_attrs`) and
re-inserts from the sheets. Operator-pinned: directory always
reflects the current state of the sheets, no leftover stale
rows. The migration's `ON DELETE CASCADE` on the FK makes the
wipe a single statement.

Match key: `(name_normalised, type)` UNIQUE — case-insensitive,
ASCII-folded form of the name. Two satellite rows can attach to
the same hub if the operator listed the same counterparty in
multiple tabs (rare but possible, e.g. moved from Outreach to
Rejections).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_setup import get_logger
from app.models import Counterparty, CounterpartyAttribute

log = get_logger(__name__)


def normalise_name(s: str | None) -> str:
    """FR-CR-05-124 — produce a fuzzy-match-friendly form of a
    counterparty name. Used as the unique key on the hub and
    later as the lookup key when matching transcript mentions.

    Strategy:
      - NFKD-normalise (decompose accents into base + combining).
      - Drop combining marks (so «ё» → «е», «é» → «e»).
      - Lowercase.
      - Collapse whitespace runs and strip.
      - Drop trailing «inc.» / «llc» / «ltd» / «ооо» / «ао» /
        «pjsc» / «jsc» — common legal-form suffixes that vary
        between mentions in speech vs the canonical form on the
        sheet.
    """
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    no_marks = "".join(
        c for c in decomposed if not unicodedata.combining(c)
    )
    lowered = no_marks.lower()
    collapsed = re.sub(r"\s+", " ", lowered).strip()
    # Strip trailing legal forms — case-insensitive already (we
    # lowercased), so this is a plain regex.
    suffix_re = re.compile(
        r"[\s,]*(?:inc\.?|llc|ltd\.?|corp\.?|co\.?|gmbh|"
        r"sa\.?|ag|s\.?p\.?a\.?|pjsc|jsc|plc|"
        r"ооо|оао|зао|пао|ао|llp)\.?$"
    )
    return suffix_re.sub("", collapsed).strip()


class CounterpartiesSheetSync:
    """Wipe-and-replace pull from the configured Google Sheets
    into the `counterparties` hub + `counterparty_attrs`
    satellite. Service-account auth via the credentials passed
    in at construction time."""

    def __init__(
        self,
        *,
        credentials: Any,
        status_spreadsheet_id: str,
        status_tab_name: str,
        name_first_tabs: list[tuple[str, str]],
    ) -> None:
        """`name_first_tabs` is a list of `(sheet_id, tab_name)`
        pairs — each pair is a sheet/tab where column A is the
        canonical name and the tab name itself becomes the
        `type` on the hub. Lets the operator add new sources
        without changing code (FR-CR-05-124 follow-up after a
        third sheet was added)."""
        self._service = build(
            "sheets", "v4", credentials=credentials,
            cache_discovery=False,
        )
        self._status_id = status_spreadsheet_id
        self._status_tab = status_tab_name
        self._name_first_tabs = list(name_first_tabs)
        # Back-compat shim for tests / CLI dry-run that read the
        # old attribute names.
        self._outreach_tabs = [tab for _id, tab in self._name_first_tabs]

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        retry=retry_if_exception_type(HttpError),
    )
    def _read_tab(self, spreadsheet_id: str, tab: str) -> list[list[str]]:
        rng = f"{tab}!A:Z"
        resp = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=rng)
            .execute()
        )
        return list(resp.get("values") or [])

    def _read_status_outreach(self) -> list[dict[str, Any]]:
        """Return list of `{name, type, attributes}` from the
        Status outreach tab. Field A = type, field B = name,
        all other columns join into `attributes` keyed by header.
        """
        if not self._status_id:
            return []
        rows = self._read_tab(self._status_id, self._status_tab)
        if not rows:
            return []
        header = rows[0]
        out: list[dict[str, Any]] = []
        for row in rows[1:]:
            if not row or all(not (c or "").strip() for c in row):
                continue
            cells = list(row) + [""] * (len(header) - len(row))
            type_ = (cells[0] or "").strip() if len(cells) > 0 else ""
            name = (cells[1] or "").strip() if len(cells) > 1 else ""
            if not name:
                continue
            attrs = {
                (header[i] if i < len(header) else f"col_{i}"): (
                    cells[i] or ""
                )
                for i in range(len(cells))
            }
            out.append({
                "name": name,
                "type": type_ or "uncategorised",
                "attributes": attrs,
                "source": self._status_tab,
            })
        return out

    def _read_name_first_tab(
        self, sheet_id: str, tab: str
    ) -> list[dict[str, Any]]:
        """Return list of `{name, type=tab, attributes}` from the
        given name-first tab. Field A is the canonical name;
        other columns are captured into the satellite's JSONB."""
        if not sheet_id:
            return []
        rows = self._read_tab(sheet_id, tab)
        if not rows:
            return []
        header = rows[0]
        out: list[dict[str, Any]] = []
        for row in rows[1:]:
            if not row or not (row[0] or "").strip():
                continue
            cells = list(row) + [""] * (len(header) - len(row))
            name = (cells[0] or "").strip()
            attrs = {
                (header[i] if i < len(header) else f"col_{i}"): (
                    cells[i] or ""
                )
                for i in range(len(cells))
            }
            out.append({
                "name": name,
                "type": tab,
                "attributes": attrs,
                "source": tab,
            })
        return out

    def pull(self, session: Session) -> tuple[int, int]:
        """Wipe-and-replace pull. Returns ``(hubs_inserted,
        attrs_inserted)``. Empty input → no changes (we don't
        wipe when both sheets are misconfigured / unreachable —
        keeps the directory available rather than blanking it
        on a transient failure)."""
        records: list[dict[str, Any]] = []
        records.extend(self._read_status_outreach())
        for sheet_id, tab in self._name_first_tabs:
            try:
                records.extend(self._read_name_first_tab(sheet_id, tab))
            except HttpError as e:  # noqa: BLE001
                # 400 «Unable to parse range» when a tab is
                # missing — log and continue with the rest.
                log.warning(
                    "counterparties_tab_unreachable",
                    sheet_id=sheet_id, tab=tab, error=str(e),
                )
        if not records:
            log.info("counterparties_pull_empty")
            return 0, 0

        # Wipe — cascade drops the satellite rows.
        session.query(CounterpartyAttribute).delete()
        session.query(Counterparty).delete()
        session.flush()

        hubs_by_norm: dict[str, Counterparty] = {}
        # `(counterparty_id, source)` is UNIQUE — when the same
        # canonical name appears twice on the same source tab
        # (operator typo / merger duplicates) we keep the FIRST
        # row's satellite and skip the rest.
        seen_attr_keys: set[tuple[int, str]] = set()
        attrs_inserted = 0
        now = datetime.now(timezone.utc)
        for rec in records:
            name = rec["name"]
            type_ = rec["type"]
            normalised = normalise_name(name)
            if not normalised:
                continue
            # FR-CR-05-126 follow-up — dedupe by `name_normalised`
            # ONLY (not by `(name_normalised, type)`). When the
            # same canonical counterparty appears in multiple
            # tabs / sheets («Balderton» in both «Outreach» and
            # «Rejections», «Tencent» in «Outreach» and
            # «Strategic», «Nvidia» in «Outreach» and
            # «Strategic»), we keep ONE hub row with the
            # first-seen `type`, and attach a satellite per
            # source so all the per-tab metadata still survives.
            cp = hubs_by_norm.get(normalised)
            if cp is None:
                cp = Counterparty(
                    name=name,
                    type=type_,
                    name_normalised=normalised,
                )
                session.add(cp)
                session.flush()
                hubs_by_norm[normalised] = cp
            attr_key = (cp.id, rec["source"])
            if attr_key in seen_attr_keys:
                continue
            seen_attr_keys.add(attr_key)
            session.add(
                CounterpartyAttribute(
                    counterparty_id=cp.id,
                    source=rec["source"],
                    attributes=rec["attributes"],
                    captured_at=now,
                )
            )
            attrs_inserted += 1
        session.flush()
        log.info(
            "counterparties_pull_done",
            hubs=len(hubs_by_norm),
            attrs=attrs_inserted,
        )
        return len(hubs_by_norm), attrs_inserted


__all__ = ["CounterpartiesSheetSync", "normalise_name"]
