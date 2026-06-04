"""FR-EC-CRITIC-2 — local CRM replica: lazy daily refresh + stale fallback.

Pure tests of `load_catalog`'s freshness decision (latest_snapshot /
refresh_snapshot monkeypatched — no DB, no MCP)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import fr_catalog_replica as repl

_NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
_S = SimpleNamespace(entity_fr_replica_max_age_hours=24, entity_fr_mcp_url="mcp://x",
                     entity_fr_catalog_ttl_seconds=3600)


class _Snap:
    def __init__(self, raw, at):
        self.raw_text, self.fetched_at, self.entity_count = raw, at, 1


def _no_refresh(*a, **k):
    raise AssertionError("refresh_snapshot must NOT be called for a fresh snapshot")


def test_fresh_snapshot_used_without_refresh(monkeypatch):
    snap = _Snap("FRESHDUMP", _NOW - timedelta(hours=1))
    monkeypatch.setattr(repl, "latest_snapshot", lambda s: snap)
    monkeypatch.setattr(repl.R, "parse_fr_dump",
                        lambda t: ["FRESH"] if t == "FRESHDUMP" else [])
    monkeypatch.setattr(repl, "refresh_snapshot", _no_refresh)
    out = repl.load_catalog(object(), settings=_S, now=lambda: _NOW)
    assert out == ["FRESH"]


def test_stale_snapshot_triggers_refresh(monkeypatch):
    snap = _Snap("OLD", _NOW - timedelta(hours=30))
    monkeypatch.setattr(repl, "latest_snapshot", lambda s: snap)
    monkeypatch.setattr(repl, "refresh_snapshot",
                        lambda *a, **k: (2, ["NEW1", "NEW2"]))
    out = repl.load_catalog(object(), settings=_S, now=lambda: _NOW)
    assert out == ["NEW1", "NEW2"]          # refreshed, not the stale dump


def test_refresh_failure_falls_back_to_stale(monkeypatch):
    snap = _Snap("OLD", _NOW - timedelta(hours=30))
    monkeypatch.setattr(repl, "latest_snapshot", lambda s: snap)
    monkeypatch.setattr(repl, "refresh_snapshot", lambda *a, **k: (0, []))
    monkeypatch.setattr(repl.R, "parse_fr_dump",
                        lambda t: ["STALE"] if t == "OLD" else [])
    out = repl.load_catalog(object(), settings=_S, now=lambda: _NOW)
    assert out == ["STALE"]                 # MCP down → keep serving last good


def test_no_snapshot_refreshes(monkeypatch):
    monkeypatch.setattr(repl, "latest_snapshot", lambda s: None)
    monkeypatch.setattr(repl, "refresh_snapshot", lambda *a, **k: (1, ["X"]))
    out = repl.load_catalog(object(), settings=_S, now=lambda: _NOW)
    assert out == ["X"]


def test_naive_timestamp_treated_as_utc(monkeypatch):
    # a naive fetched_at (no tzinfo) must not crash the tz-aware comparison
    snap = _Snap("FRESHDUMP", (_NOW - timedelta(hours=2)).replace(tzinfo=None))
    monkeypatch.setattr(repl, "latest_snapshot", lambda s: snap)
    monkeypatch.setattr(repl.R, "parse_fr_dump", lambda t: ["OK"])
    monkeypatch.setattr(repl, "refresh_snapshot", _no_refresh)
    assert repl.load_catalog(object(), settings=_S, now=lambda: _NOW) == ["OK"]


__all__: list[str] = []
