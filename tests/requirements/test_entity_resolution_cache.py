"""FR-CR-05-193e — ID-locked tests для entity_resolution_cache."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


def test_fr_cr_05_193e_schema_and_ttl() -> None:
    """Table `entity_resolution_cache` со схемой:
    (cache_key sha256 PK, payload jsonb, created_at, expires_at, hits_count int).
    Default TTL = 7 days."""
    from app.models.entity_resolution_cache import (
        EntityResolutionCache, DEFAULT_TTL_DAYS,
    )

    assert DEFAULT_TTL_DAYS == 7
    # Column names check
    cols = {c.name for c in EntityResolutionCache.__table__.columns}
    assert {"cache_key", "payload", "created_at", "expires_at",
            "hits_count"}.issubset(cols)


def test_fr_cr_05_193e_cache_key_includes_known_hashes() -> None:
    """Key = sha256(text + sha(known_people) + sha(known_orgs)).
    Изменение одного из known_people / known_orgs → новый ключ."""
    from app.services.entity_resolution_cache import compute_cache_key

    text = "test"
    kp1 = [{"tm_id": 1, "real_name": "A"}]
    kp2 = [{"tm_id": 1, "real_name": "A"}, {"tm_id": 2, "real_name": "B"}]
    ko1 = [{"cp_id": 1, "name": "X"}]

    k1 = compute_cache_key(text=text, known_people=kp1, known_orgs=ko1)
    k2 = compute_cache_key(text=text, known_people=kp2, known_orgs=ko1)
    k3 = compute_cache_key(text="different", known_people=kp1, known_orgs=ko1)
    k4 = compute_cache_key(text=text, known_people=kp1, known_orgs=ko1)  # same as k1

    assert k1 != k2  # different people → different key
    assert k1 != k3  # different text → different key
    assert k1 == k4  # same inputs → same key
    assert all(len(k) == 64 for k in (k1, k2, k3))  # sha256 hex


def test_fr_cr_05_193e_logs_hit_miss(caplog) -> None:
    """get_cached / set_cached emit log events `entity_matcher_cache_hit`
    / `_miss` для observability."""
    import logging
    from app.services.entity_resolution_cache import (
        get_cached, set_cached,
    )

    mock_session = MagicMock()
    mock_session.execute().first.return_value = None  # miss

    with caplog.at_level(logging.INFO):
        get_cached(mock_session, cache_key="x" * 64)
    # Должно быть упоминание miss
    assert any("entity_matcher_cache_miss" in str(r.message) or "cache_miss" in str(r.message)
               for r in caplog.records)


def test_fr_cr_05_193e_cleanup_expired() -> None:
    """Op `cleanup_expired_cache(session)` удаляет rows where expires_at <
    now(). Возвращает кол-во удалённых."""
    from app.services.entity_resolution_cache import cleanup_expired_cache

    mock_session = MagicMock()
    mock_session.execute().rowcount = 5  # 5 expired deleted

    deleted = cleanup_expired_cache(mock_session)
    assert deleted == 5
