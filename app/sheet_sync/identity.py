"""Task identity helpers (spec §20.5).

Primary identity is the internal record id, attached to the Sheet row via
Google Sheets DeveloperMetadata (handled by the Sheets client, next phase).
This module provides the deterministic FALLBACK signature used when row
lineage is lost.

Stdlib-only.
"""
from __future__ import annotations

from app.sheet_sync.config import SIGNATURE_FIELDS
from app.sheet_sync.normalize import stable_hash


def task_signature(payload: dict) -> str:
    """Fallback identity signature (spec §20.5):

        hash(normalized(title + assignee_id + start_at + deadline_at + category))

    Limitation: changing all of these at once makes the row look like a new
    task and the old one like deleted. Prefer DeveloperMetadata lineage.
    """
    return stable_hash(payload, fields=SIGNATURE_FIELDS)


__all__ = ["task_signature"]
