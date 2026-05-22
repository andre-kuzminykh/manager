"""FR-CR-05-193e — entity_resolution_cache key/get/set/cleanup."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text as sql_text

from app.logging_setup import get_logger

log = get_logger(__name__)


def compute_cache_key(
    *,
    text: str,
    known_people: list[dict],
    known_orgs: list[dict],
) -> str:
    """SHA-256 hex от concat(text, sha(known_people), sha(known_orgs))."""
    def _stable_hash(items):
        s = json.dumps(items, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    parts = "|".join([
        text or "",
        _stable_hash(known_people or []),
        _stable_hash(known_orgs or []),
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def get_cached(session: Any, *, cache_key: str) -> dict | None:
    """Поиск кэша по ключу. Возвращает payload (dict) или None.

    Также обновляет hits_count если cache hit."""
    row = session.execute(
        sql_text(
            """
            SELECT payload, expires_at
            FROM entity_resolution_cache
            WHERE cache_key = :k AND expires_at > now()
            """
        ),
        {"k": cache_key},
    ).first()
    if row is None:
        log.info("entity_matcher_cache_miss", cache_key=cache_key[:16])
        return None
    log.info("entity_matcher_cache_hit", cache_key=cache_key[:16])
    # increment hits async (best-effort)
    try:
        session.execute(
            sql_text(
                """
                UPDATE entity_resolution_cache
                SET hits_count = hits_count + 1
                WHERE cache_key = :k
                """
            ),
            {"k": cache_key},
        )
    except Exception:  # noqa: BLE001
        pass
    payload = row[0]
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload


def set_cached(
    session: Any,
    *,
    cache_key: str,
    payload: dict,
    ttl_days: int = 7,
) -> None:
    """UPSERT в cache table с TTL."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    session.execute(
        sql_text(
            """
            INSERT INTO entity_resolution_cache
                (cache_key, payload, created_at, expires_at, hits_count)
            VALUES (:k, CAST(:p AS jsonb), now(), :exp, 0)
            ON CONFLICT (cache_key)
            DO UPDATE SET payload = EXCLUDED.payload,
                          expires_at = EXCLUDED.expires_at,
                          hits_count = entity_resolution_cache.hits_count
            """
        ),
        {
            "k": cache_key,
            "p": json.dumps(payload, ensure_ascii=False),
            "exp": expires_at,
        },
    )


def cleanup_expired_cache(session: Any) -> int:
    """Удаляет rows where expires_at < now(). Returns deleted count."""
    r = session.execute(
        sql_text("DELETE FROM entity_resolution_cache WHERE expires_at < now()")
    )
    return r.rowcount
