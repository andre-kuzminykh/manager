"""Google Sheets Versioned Sync (spec: Feature Spec: Google Sheets Versioned Sync).

ISOLATED feature — own `gs_*` tables, own runner container, `SHEET_SYNC_ENABLED`
flag. Never mutates the existing `tasks` / `action_drafts` tables.

Deterministic core (config / normalize / validation / identity) is stdlib-only
and unit-testable without DB or network.
"""
