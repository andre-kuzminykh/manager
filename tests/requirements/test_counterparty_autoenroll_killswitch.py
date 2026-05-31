"""FR-CR-05-230 — auto-enrollment kill-switch.

The 3 paths that minted Counterparty rows from unmatched mentions
(summary canonicalize + 2 enrollment services) are now gated behind
`counterparty_autoenroll_enabled` (default False). With it off the
directory changes ONLY via the Google-Sheet sync, so garbled Whisper
variants stop accumulating alias/dup cards.
"""
from __future__ import annotations

import pathlib

_SVC = pathlib.Path(__file__).resolve().parents[2] / "app" / "services"


def test_flag_defaults_off():
    from app.config import Settings

    s = Settings()
    assert s.counterparty_autoenroll_enabled is False


def test_all_three_creation_sites_are_gated():
    """Each file that constructs Counterparty from a mention must guard
    on the kill-switch right before creation."""
    for fname in (
        "summary_canonicalize.py",
        "counterparty_enrollment.py",
        "counterparty_enrollment_batch.py",
    ):
        src = (_SVC / fname).read_text(encoding="utf-8")
        assert "counterparty_autoenroll_enabled" in src, f"{fname} not gated"


def test_enrollment_returns_none_when_disabled(session, monkeypatch):
    """Behavioural: with the switch off, _ensure_counterparty returns
    None for an unknown name and creates no row."""
    from app.config import get_settings
    from app.models.counterparty import Counterparty
    from app.services import counterparty_enrollment as ce

    # ensure flag is off in the resolved settings
    s = get_settings()
    monkeypatch.setattr(s, "counterparty_autoenroll_enabled", False, raising=False)

    before = session.query(Counterparty).count()
    out = ce._ensure_counterparty(session, name="Совершенно Новый Контрагент XYZ")
    assert out is None
    assert session.query(Counterparty).count() == before


def test_enrollment_creates_when_enabled(session, monkeypatch):
    """Behavioural: with the switch ON, the unknown name is created."""
    from app.config import get_settings
    from app.models.counterparty import Counterparty
    from app.services import counterparty_enrollment as ce

    s = get_settings()
    monkeypatch.setattr(s, "counterparty_autoenroll_enabled", True, raising=False)

    out = ce._ensure_counterparty(session, name="Brand New Co ABC")
    assert out is not None
    assert out.name == "Brand New Co ABC"
    assert session.query(Counterparty).filter(
        Counterparty.id == out.id
    ).one_or_none() is not None
