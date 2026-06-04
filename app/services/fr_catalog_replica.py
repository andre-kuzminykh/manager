"""FR-EC-CRITIC-2 — local replica of Viktor's fundraising CRM catalog.

The resolver used to hit the live n8n MCP for the full catalog on every meeting
(in-memory TTL cache per process). This module mirrors the dump into a local
table (`fr_catalog_snapshots`) and serves the resolver from there, refreshing at
most once a day. Benefits:
  * independence — a meeting still resolves if his MCP is down (last snapshot);
  * stability — the catalog doesn't change mid-day between meetings;
  * one daily MCP hit instead of one per process/TTL.

Entry points:
  * `refresh_snapshot(session, mcp_url=...)` — fetch the dump + store a snapshot
    (called by `ops.sync_fr_catalog` daily, or lazily by `load_catalog`).
  * `load_catalog(session, settings=...)` — what the resolver calls: returns the
    parsed catalog from the freshest snapshot, refreshing first if it's older
    than `entity_fr_replica_max_age_hours`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.logging_setup import get_logger
from app.services import entity_resolver_fr as R

log = get_logger(__name__)


def latest_snapshot(session: Any):
    from app.models import FrCatalogSnapshot
    return (session.query(FrCatalogSnapshot)
            .order_by(FrCatalogSnapshot.fetched_at.desc())
            .first())


def refresh_snapshot(
    session: Any,
    *,
    mcp_url: str,
    limit: int = 2000,
    timeout: float = 180.0,
    call_tool: Callable[..., tuple[bool, str]] | None = None,
) -> tuple[int, list[R.FrEntity]]:
    """Fetch the full CRM dump from the MCP and store a new snapshot row.

    Returns (entity_count, parsed_entities). On MCP failure / empty parse:
    stores NOTHING and returns (0, []) — the caller keeps using the last good
    snapshot. Best-effort; never raises."""
    if not mcp_url:
        return 0, []
    if call_tool is None:
        from app.ceo_brain.mcp_client import call_tool as call_tool  # noqa: PLC0415
    try:
        ok, txt = call_tool(
            url=mcp_url, tool_name="humanoid_fr_search",
            arguments={"query": "", "limit": str(limit)}, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("fr_replica_fetch_failed", error=str(e))
        return 0, []
    if not ok or not txt:
        log.warning("fr_replica_fetch_not_ok", error=(txt or "")[:200])
        return 0, []
    entities = R.parse_fr_dump(txt)
    if not entities:
        log.warning("fr_replica_parse_empty")
        return 0, []
    from app.models import FrCatalogSnapshot
    session.add(FrCatalogSnapshot(raw_text=txt, entity_count=len(entities)))
    session.flush()
    log.info("fr_replica_snapshot_stored", entity_count=len(entities))
    return len(entities), entities


def load_catalog(
    session: Any,
    *,
    settings: Any,
    call_tool: Callable[..., tuple[bool, str]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> list[R.FrEntity]:
    """Resolver entry point. Return the parsed catalog from the freshest local
    snapshot, refreshing from the MCP first when the latest snapshot is older
    than `entity_fr_replica_max_age_hours` (lazy daily sync). Falls back to the
    stale snapshot on MCP failure, and to a direct MCP fetch when there is no
    snapshot at all."""
    _now = (now or (lambda: datetime.now(timezone.utc)))()
    mcp_url = getattr(settings, "entity_fr_mcp_url", "") or ""
    max_age_h = float(getattr(settings, "entity_fr_replica_max_age_hours", 24) or 24)

    snap = latest_snapshot(session)
    fresh = False
    if snap is not None and snap.fetched_at is not None:
        ts = snap.fetched_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        fresh = (_now - ts) < timedelta(hours=max_age_h)

    if snap is not None and fresh:
        return R.parse_fr_dump(snap.raw_text)

    # stale or missing → try a refresh (the «daily check»).
    count, entities = refresh_snapshot(session, mcp_url=mcp_url, call_tool=call_tool)
    if entities:
        return entities
    # refresh failed → use the stale snapshot if we have one.
    if snap is not None:
        log.info("fr_replica_using_stale_snapshot",
                 fetched_at=str(snap.fetched_at))
        return R.parse_fr_dump(snap.raw_text)
    # no replica at all → last resort: live MCP via the resolver's own cache.
    log.info("fr_replica_no_snapshot_fallback_live")
    return R.fetch_catalog(
        mcp_url=mcp_url,
        ttl_seconds=int(getattr(settings, "entity_fr_catalog_ttl_seconds", 3600)),
        call_tool=call_tool,
    )


__all__ = ["latest_snapshot", "refresh_snapshot", "load_catalog"]
