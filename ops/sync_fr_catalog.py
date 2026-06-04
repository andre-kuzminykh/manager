"""FR-EC-CRITIC-2 — refresh the local replica of Viktor's fundraising CRM.

Fetches the full `humanoid_fr_search` dump from the n8n MCP and stores a new
`fr_catalog_snapshots` row. The resolver reads the freshest snapshot, so this
keeps it current without hitting the live MCP per meeting.

Run daily (cron / systemd timer), e.g. once a day:
    docker exec -i manager-zoom-ff-1 python -m ops.sync_fr_catalog

The resolver also refreshes lazily when the latest snapshot is older than
ENTITY_FR_REPLICA_MAX_AGE_HOURS, so this script is a proactive belt-and-braces
daily check — handy to pre-warm the replica and to surface MCP failures early.

Exit codes: 0 stored a fresh snapshot · 1 fetch/parse failed (kept last good).
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.db import session_scope
from app.services import fr_catalog_replica as repl


def main() -> int:
    s = get_settings()
    mcp_url = getattr(s, "entity_fr_mcp_url", "") or ""
    if not mcp_url:
        print("ENTITY_FR_MCP_URL not set — nothing to sync.")
        return 1
    with session_scope() as sess:
        prev = repl.latest_snapshot(sess)
        prev_at = prev.fetched_at if prev else None
        count, entities = repl.refresh_snapshot(sess, mcp_url=mcp_url)
        if not entities:
            print(f"sync FAILED (MCP fetch/parse) — keeping last snapshot "
                  f"from {prev_at} ({getattr(prev, 'entity_count', 0)} rows).")
            return 1
        print(f"sync OK — stored snapshot: {count} entities "
              f"(previous: {getattr(prev, 'entity_count', 0)} @ {prev_at}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
