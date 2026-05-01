"""FR-CR-05-124 — one-shot wipe-and-reload of the counterparties
directory from the configured Google Sheets.

Usage::

    python -m ops.pull_counterparties

The default behaviour is wipe-and-replace (matches the listener's
auto-pull). Use ``--dry-run`` to read the sheets without touching
the DB — useful when verifying the column shape after sharing
the sheets with the service account.

Exits 0 on success, 2 on configuration problems (no sheet ids
set, no Google credentials).
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger, setup_logging
from app.sync.factories import build_counterparties_sheet_factory

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pull counterparties from Google Sheets.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print what would land without writing to the DB. "
            "Useful for verifying tab/column shape on first "
            "share."
        ),
    )
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = _parse_args()
    settings = get_settings()

    factory = build_counterparties_sheet_factory(settings)
    if factory is None:
        log.error(
            "counterparties_no_sheet_ids_configured",
            hint=(
                "set COUNTERPARTIES_STATUS_SHEET_ID and/or "
                "COUNTERPARTIES_OUTREACH_SHEET_ID in .env"
            ),
        )
        return 2
    sync = factory()
    if sync is None:
        log.error(
            "counterparties_no_credentials",
            hint=(
                "share both sheets with the service account: "
                "slack-task-bot@…iam.gserviceaccount.com"
            ),
        )
        return 2

    if args.dry_run:
        # Dump what each tab would yield, no DB writes. Errors
        # per tab are logged and skipped — same gracefulness as
        # the real pull (FR-CR-05-124 follow-up: Excel-uploaded
        # files in Drive return 400 «not supported for this
        # document», don't take the whole dry-run down with
        # them).
        from googleapiclient.errors import HttpError

        records: list[dict] = []
        try:
            records.extend(sync._read_status_outreach())  # noqa: SLF001
        except HttpError as e:  # noqa: BLE001
            log.warning(
                "counterparties_status_unreachable", error=str(e),
            )
        for sheet_id, tab in sync._name_first_tabs:  # noqa: SLF001
            try:
                records.extend(
                    sync._read_name_first_tab(sheet_id, tab)  # noqa: SLF001
                )
            except HttpError as e:  # noqa: BLE001
                log.warning(
                    "counterparties_tab_unreachable",
                    sheet_id=sheet_id, tab=tab, error=str(e),
                )
        log.info(
            "counterparties_dry_run",
            total_records=len(records),
            preview=[
                {"name": r["name"], "type": r["type"]}
                for r in records[:10]
            ],
        )
        return 0

    with session_scope() as session:
        hubs, attrs = sync.pull(session)
    log.info(
        "counterparties_cli_done",
        hubs_inserted=hubs,
        attrs_inserted=attrs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
