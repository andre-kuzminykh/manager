"""FR-CR-05-193d-2 / 193d-4 — Counterparty aliases storage + retrieval."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text as sql_text

from app.logging_setup import get_logger

log = get_logger(__name__)


ALIASES_SOURCE = "aliases"
"""Constant source-tag для counterparty_attrs.source field."""


def save_aliases(
    session: Any,
    *,
    counterparty_id: int,
    aliases: list[str],
) -> None:
    """UPSERT aliases для counterparty в counterparty_attrs.

    No-op если aliases пуст.
    """
    if not aliases:
        return
    # Filter empty strings, dedupe
    clean = []
    seen: set[str] = set()
    for a in aliases:
        a = (a or "").strip()
        if a and a.lower() not in seen:
            clean.append(a)
            seen.add(a.lower())
    if not clean:
        return

    session.execute(
        sql_text(
            """
            INSERT INTO counterparty_attrs
                (counterparty_id, source, attributes, captured_at)
            VALUES (:cp_id, :source, CAST(:attrs AS json), now())
            ON CONFLICT (counterparty_id, source)
            DO UPDATE SET attributes = EXCLUDED.attributes,
                          captured_at = now()
            """
        ),
        {
            "cp_id": counterparty_id,
            "source": ALIASES_SOURCE,
            "attrs": _to_json({"aliases": clean}),
        },
    )


def _to_json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def get_orgs_with_aliases(session: Any) -> list[dict]:
    """JOIN counterparties + counterparty_attrs(source='aliases').

    Returns list[{cp_id, name, aliases:list[str]}].
    """
    rows = session.execute(
        sql_text(
            """
            SELECT c.id, c.name,
                   ca.attributes AS attrs
            FROM counterparties c
            LEFT JOIN counterparty_attrs ca
              ON ca.counterparty_id = c.id AND ca.source = :source
            ORDER BY c.name
            """
        ),
        {"source": ALIASES_SOURCE},
    ).all()
    out: list[dict] = []
    for r in rows:
        # r может быть tuple или Row — оба поддерживают индекс
        cp_id = r[0]
        name = r[1]
        attrs = r[2]
        aliases: list[str] = []
        if attrs and isinstance(attrs, dict):
            raw = attrs.get("aliases")
            if isinstance(raw, list):
                aliases = [str(a) for a in raw if a]
        out.append({"cp_id": cp_id, "name": name, "aliases": aliases})
    return out
