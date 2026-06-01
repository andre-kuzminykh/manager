"""FR-CR-05-232 — owner = addressee (not author) + clean owner names.

Live Slack dry-run (operator, 2026-06-01) showed two extraction defects on
the `#legal` channel:

  1. On sub-tasks where the LLM picked no owner, the chain fell back to the
     message AUTHOR («kaa») — wrong for delegation messages («please review
     /sign @X»). Owner must be the @mentioned addressee.
  2. Owner display names carried noise: «[Sterling Law] Ilia Martynov»
     (company prefix), «kaa» (handle, not a name).

Both fixed in the shared `_resolve_owner` chain (Telegram behaviour is
unchanged because it passes no `mention_uids`).
"""
from __future__ import annotations

from types import SimpleNamespace

from app.telegram_ingest.service import (
    _best_employee_display,
    _clean_display,
    _looks_handleish,
    _resolve_owner,
)


def _td(owner_user_id=None, owner_display_name=None):
    return SimpleNamespace(
        owner_user_id=owner_user_id, owner_display_name=owner_display_name
    )


_EMPLOYEES = [
    {"slack_user_id": "U_ILIA", "display_name": "[Sterling Law] Ilia Martynov",
     "real_name": "Ilia Martynov"},
    {"slack_user_id": "U_KAA", "display_name": "kaa", "real_name": "Kirill Antonov"},
    {"slack_user_id": "U_PM", "display_name": "Legal Project Manager",
     "real_name": None},
]


# --- name cleaning ---------------------------------------------------------
def test_clean_display_strips_bracket_prefix():
    assert _clean_display("[Sterling Law] Ilia Martynov") == "Ilia Martynov"
    assert _clean_display("Ilia Martynov") == "Ilia Martynov"
    assert _clean_display("") == ""
    assert _clean_display(None) is None


def test_looks_handleish():
    assert _looks_handleish("kaa") is True
    assert _looks_handleish("evictorov") is True
    assert _looks_handleish("Ilia Martynov") is False  # has space
    assert _looks_handleish("Andre") is False  # capitalised


def test_best_employee_display_prefers_clean_human_name():
    # bracket prefix stripped
    assert _best_employee_display(_EMPLOYEES[0]) == "Ilia Martynov"
    # handle display → fall back to proper real_name
    assert _best_employee_display(_EMPLOYEES[1]) == "Kirill Antonov"
    # role-only label with no real_name → keep as-is (no better signal)
    assert _best_employee_display(_EMPLOYEES[2]) == "Legal Project Manager"


# --- step 1: LLM picked id → cleaned registry display ----------------------
def test_resolved_id_uses_clean_display():
    td = _td(owner_user_id="U_ILIA", owner_display_name="[Sterling Law] Ilia Martynov")
    _resolve_owner(td, known_employees=_EMPLOYEES, sender_user_id="U_KAA",
                   sender_user_name="kaa", admin_uid=None)
    assert td.owner_user_id == "U_ILIA"
    assert td.owner_display_name == "Ilia Martynov"


# --- step 2.5: addressee over author ---------------------------------------
def test_addressee_preferred_over_author_fallback():
    """LLM gave no owner; message @mentions Ilia (not the author kaa) →
    owner is Ilia, NOT the author."""
    td = _td()
    _resolve_owner(
        td, known_employees=_EMPLOYEES, sender_user_id="U_KAA",
        sender_user_name="kaa", admin_uid=None,
        mention_uids=["U_ILIA", "U_PM"],
    )
    assert td.owner_user_id == "U_ILIA"
    assert td.owner_display_name == "Ilia Martynov"


def test_addressee_skips_author_mention():
    """When the only mention IS the author, addressee step must not fire —
    falls through to the (allowed) author fallback."""
    td = _td()
    _resolve_owner(
        td, known_employees=_EMPLOYEES, sender_user_id="U_KAA",
        sender_user_name="kaa", admin_uid=None,
        mention_uids=["U_KAA"],
    )
    assert td.owner_user_id == "U_KAA"  # author fallback
    assert td.owner_display_name == "Kirill Antonov"  # cleaned, not «kaa»


def test_no_mentions_keeps_author_fallback():
    """Telegram parity: no mention_uids → legacy author fallback unchanged."""
    td = _td()
    _resolve_owner(
        td, known_employees=_EMPLOYEES, sender_user_id="U_KAA",
        sender_user_name="kaa", admin_uid=None,
    )
    assert td.owner_user_id == "U_KAA"
    assert td.owner_display_name == "Kirill Antonov"


def test_addressee_ignored_when_not_in_registry():
    """A mention that isn't a known teammate is skipped (falls to author)."""
    td = _td()
    _resolve_owner(
        td, known_employees=_EMPLOYEES, sender_user_id="U_KAA",
        sender_user_name="kaa", admin_uid=None,
        mention_uids=["U_OUTSIDER"],
    )
    assert td.owner_user_id == "U_KAA"


# --- Slack mention parsing -------------------------------------------------
def test_mention_uids_parsing_excludes_author_and_dedups():
    from app.slack_ingest.listener import _mention_uids

    # realistic Slack ids ([A-Z0-9] only)
    text = "Please review <@U078W5SV6GL> <@U08C53BF3T6|label> <@U078W5SV6GL> <@U079342FNKW>"
    assert _mention_uids(text, exclude="U079342FNKW") == ["U078W5SV6GL", "U08C53BF3T6"]
    assert _mention_uids("no mentions here", exclude="U079342FNKW") == []
