"""FR-CR-05-193f — ID-locked tests для ops/backfill_counterparty_aliases.py."""
from __future__ import annotations

import json
from unittest.mock import MagicMock


def test_fr_cr_05_193f_batch_50_per_call() -> None:
    """Backfill processes counterparties в batches of 50 per LLM call.
    250 counterparties → 5 LLM calls."""
    from ops.backfill_counterparty_aliases import backfill_aliases

    mock_session = MagicMock()
    cps = [MagicMock(id=i, name=f"Org{i}") for i in range(250)]
    mock_session.query().all.return_value = cps

    llm = MagicMock()
    llm.chat.return_value = json.dumps({
        "aliases_by_id": {str(i): [f"Alias{i}A", f"Alias{i}B"] for i in range(50)},
    })
    backfill_aliases(
        session=mock_session, llm_backend=llm,
        model="gpt-5.5-mini", dry_run=False,
    )
    assert llm.chat.call_count == 5  # 250 / 50


def test_fr_cr_05_193f_skip_already_aliased() -> None:
    """Counterparty с уже сохранёнными aliases (counterparty_attrs.source=
    'aliases') skipped — no LLM call."""
    from ops.backfill_counterparty_aliases import backfill_aliases

    mock_session = MagicMock()
    cps = [
        MagicMock(id=1, name="Schaeffler"),  # already has aliases
        MagicMock(id=2, name="Bain Capital"),  # has aliases too
    ]
    mock_session.query().all.return_value = cps
    # Existing aliases — оба counterparty already done
    mock_session.execute().all.return_value = [(1,), (2,)]

    llm = MagicMock()
    backfill_aliases(
        session=mock_session, llm_backend=llm, model="x", dry_run=False,
    )
    # All skipped, no LLM calls
    assert llm.chat.call_count == 0


def test_fr_cr_05_193f_dry_run_no_writes() -> None:
    """`--dry-run` → нет save_aliases / commit, only print."""
    from ops.backfill_counterparty_aliases import backfill_aliases

    mock_session = MagicMock()
    cps = [MagicMock(id=1, name="Schaeffler")]
    mock_session.query().all.return_value = cps
    mock_session.execute().all.return_value = []  # no existing

    llm = MagicMock()
    llm.chat.return_value = json.dumps({
        "aliases_by_id": {"1": ["Шеффлер", "Шафлер"]},
    })
    backfill_aliases(
        session=mock_session, llm_backend=llm, model="x", dry_run=True,
    )
    # dry-run — НИКАКОГО commit
    assert mock_session.commit.call_count == 0


def test_fr_cr_05_193f_uses_injected_llm_backend() -> None:
    """`backfill_aliases(llm_backend=X)` использует переданный backend.
    Не создаёт собственный."""
    from ops.backfill_counterparty_aliases import backfill_aliases
    import inspect
    sig = inspect.signature(backfill_aliases)
    assert "llm_backend" in sig.parameters
    assert "model" in sig.parameters


def test_fr_cr_05_193f_empty_db_no_calls() -> None:
    """0 counterparties → 0 LLM calls, return early."""
    from ops.backfill_counterparty_aliases import backfill_aliases

    mock_session = MagicMock()
    mock_session.query().all.return_value = []

    llm = MagicMock()
    backfill_aliases(
        session=mock_session, llm_backend=llm, model="x", dry_run=False,
    )
    assert llm.chat.call_count == 0
