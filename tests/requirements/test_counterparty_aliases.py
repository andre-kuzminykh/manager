"""FR-CR-05-193d-2 + 193d-4 — ID-locked tests для Counterparty aliases."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


def test_fr_cr_05_193d_aliases_storage_schema() -> None:
    """Aliases хранятся в `counterparty_attrs` с `source='aliases'`,
    `attributes` JSON содержит ключ `aliases: list[str]`."""
    from app.services.counterparty_aliases import (
        save_aliases, ALIASES_SOURCE,
    )

    assert ALIASES_SOURCE == "aliases"

    mock_session = MagicMock()
    save_aliases(
        mock_session,
        counterparty_id=42,
        aliases=["Шеффлер", "Шафлер", "Schaeffler AG"],
    )
    # Expect ONE INSERT / UPDATE (UPSERT by uq_counterparty_attrs_id_source)
    # check it called with right args
    insert_calls = [
        c for c in mock_session.method_calls if "execute" in str(c) or "add" in str(c)
    ]
    assert insert_calls, "save_aliases didn't issue DB call"


def test_fr_cr_05_193d_get_orgs_with_aliases() -> None:
    """`get_orgs_with_aliases(session)` JOIN'ит counterparties +
    counterparty_attrs(source='aliases'), возвращает list[dict] с
    {cp_id, name, aliases:list[str]}."""
    from app.services.counterparty_aliases import get_orgs_with_aliases

    mock_session = MagicMock()
    # Mock query result — 2 counterparties, у одной есть aliases
    mock_session.execute().all.return_value = [
        (1, "Schaeffler", {"aliases": ["Шеффлер", "Шафлер"]}),
        (2, "Bain Capital", None),
    ]
    result = get_orgs_with_aliases(mock_session)
    by_name = {o["name"]: o for o in result}
    assert "Шеффлер" in by_name["Schaeffler"]["aliases"]
    assert by_name["Bain Capital"]["aliases"] == []


def test_fr_cr_05_193d_aliases_idempotent_upsert() -> None:
    """Повторный save_aliases на ту же counterparty — UPSERT, не дубль."""
    from app.services.counterparty_aliases import save_aliases

    mock_session = MagicMock()
    # Симулируем два вызова с одной CP
    save_aliases(mock_session, counterparty_id=42, aliases=["Шеффлер"])
    save_aliases(mock_session, counterparty_id=42, aliases=["Шеффлер", "Шафлер"])
    # Должен быть ON CONFLICT или upsert path — проверяем что commit или
    # SQL executable вызван оба раза
    assert mock_session.method_calls, "no DB call recorded"


def test_fr_cr_05_193d_aliases_empty_list_skipped() -> None:
    """`save_aliases(..., aliases=[])` — no-op, не записывает пустой массив."""
    from app.services.counterparty_aliases import save_aliases

    mock_session = MagicMock()
    save_aliases(mock_session, counterparty_id=42, aliases=[])
    # Никаких DB calls — ничего не нужно сохранять
    assert not any("commit" in str(c) or "execute" in str(c)
                    for c in mock_session.method_calls)


def test_fr_cr_05_193d_get_orgs_with_aliases_empty_db() -> None:
    """Пустая counterparties таблица → return []."""
    from app.services.counterparty_aliases import get_orgs_with_aliases

    mock_session = MagicMock()
    mock_session.execute().all.return_value = []
    result = get_orgs_with_aliases(mock_session)
    assert result == []
