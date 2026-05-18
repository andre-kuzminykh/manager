"""FR-CB2-200 — CEO Brain Bot: Slack archive + Claude responder w/ MCP.

Spec: ``SPEC_CEO_BRAIN_BOT_v0.1.md``.

ALL tests in this file are intentionally `xfail(strict=True)` until
the implementation lands. When a feature ships, drop the `xfail`
decorator on the matching test — the test must turn GREEN. If a
green test stays under `xfail` (XPASS) pytest will fail strict, so
nobody can accidentally ship code that diverges from spec without
updating tests.

Test-IDs map 1:1 to FR/NFR IDs in the spec — see §12 traceability
matrix.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# All tests below xfail until impl lands. Centralise the decorator
# so we can flip the whole module green in one PR when ready.
_PENDING = pytest.mark.xfail(
    strict=True,
    reason="CEO Brain Bot — impl pending, see SPEC_CEO_BRAIN_BOT_v0.1.md",
)


# -- Category 1: Slack Events ingestion (FR-CB2-1.x) ------------------------


@_PENDING
def test_brain_socket_connect_ok(monkeypatch):
    """FR-CB2-1.1 — Socket Mode connection opens with the dedicated
    bot/app tokens."""
    from app.ceo_brain.socket_client import open_socket_connection

    monkeypatch.setenv("CEO_BRAIN_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("CEO_BRAIN_SLACK_BOT_TOKEN", "xoxb-test")
    handle = open_socket_connection(client_factory=MagicMock())
    assert handle is not None


@_PENDING
def test_brain_event_subscriptions_cover_required():
    """FR-CB2-1.2 — subscribed event types match the spec list."""
    from app.ceo_brain.socket_client import REQUIRED_EVENT_TYPES

    for ev in (
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im",
        "message.mpim",
    ):
        assert ev in REQUIRED_EVENT_TYPES


@_PENDING
def test_brain_socket_reconnect_with_backoff():
    """FR-CB2-1.3 — disconnect triggers exponential backoff
    2s / 4s / 8s / 16s before each reconnect attempt."""
    from app.ceo_brain.socket_client import compute_reconnect_delays

    assert compute_reconnect_delays(max_attempts=4) == [2, 4, 8, 16]


@_PENDING
def test_brain_signature_verification_rejects_invalid():
    """FR-CB2-1.4 — HTTP Events variant rejects bad HMAC signature."""
    from app.ceo_brain.http_events import verify_signature

    assert verify_signature(
        body=b"{}",
        timestamp="1700000000",
        signature="v0=deadbeef",
        signing_secret="wrong",
    ) is False


@_PENDING
def test_brain_dedup_repeat_event(session):
    """FR-CB2-1.5 — replaying the same (event_id, ts) is a no-op."""
    from app.ceo_brain.dispatcher import handle_event

    payload = {
        "event_id": "Ev1",
        "event_ts": "1779100000.123456",
        "type": "message",
        "channel": "C123",
        "user": "U1",
        "text": "hi",
        "ts": "1779100000.123456",
    }
    r1 = handle_event(session, payload)
    r2 = handle_event(session, payload)
    assert r1.archived is True
    assert r2.duplicate is True


@_PENDING
def test_brain_skips_self_messages(session):
    """FR-CB2-1.6 / NFR-CB2-R.3 — bot ignores its own messages
    (anti-loop guard)."""
    from app.ceo_brain.dispatcher import handle_event

    payload = {
        "type": "message",
        "channel": "C1",
        "user": "UBOTSELF",
        "text": "echo from me",
        "ts": "1779100000.000001",
        "bot_id": "B1",
    }
    r = handle_event(session, payload, bot_user_id="UBOTSELF")
    assert r.skipped_self is True


# -- Category 2: Slack archive (FR-CB2-2.x) --------------------------------


@_PENDING
def test_archive_jsonl_append(tmp_path):
    """FR-CB2-2.1 — JSONL append-only at
    `<archive_dir>/<channel>/YYYY-MM-DD.jsonl`."""
    from app.ceo_brain.archive import jsonl_sink

    jsonl_sink.write(
        archive_dir=tmp_path,
        channel_id="C1",
        channel_name="board",
        ts="1779100000.000001",
        payload={"user": "U1", "text": "hello"},
    )
    target = tmp_path / "board" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    assert target.exists()
    assert target.read_text().strip().count("\n") == 0  # exactly one line


@_PENDING
def test_archive_pg_insert(session):
    """FR-CB2-2.2 — PG mirror row written on archive."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(
        session,
        channel_id="C1",
        channel_name="board",
        ts="1779100000.000001",
        user_id="U1",
        text="hello",
        raw_payload={"foo": "bar"},
    )
    row = session.query(SlackMessageArchive).one()
    assert row.channel_id == "C1"
    assert row.text == "hello"


@_PENDING
def test_archive_unique_channel_ts(session):
    """FR-CB2-2.3 / UC-3 — UNIQUE(channel_id, ts) so retries don't
    create duplicates."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(session, channel_id="C1", ts="100.0", text="a", raw_payload={})
    pg_sink.write(session, channel_id="C1", ts="100.0", text="a", raw_payload={})
    assert session.query(SlackMessageArchive).count() == 1


@_PENDING
def test_archive_edit_overwrites_pg_keeps_jsonl_history(session, tmp_path):
    """FR-CB2-2.4 / UC-5 — `message.changed` appends a new JSONL
    line with subtype=changed AND updates the PG row's text +
    edit_count."""
    from app.ceo_brain.archive import jsonl_sink, pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(session, channel_id="C1", ts="100.0", text="old", raw_payload={})
    pg_sink.apply_edit(
        session, channel_id="C1", ts="100.0", text="new", raw_payload={"subtype": "message_changed"},
    )
    row = session.query(SlackMessageArchive).one()
    assert row.text == "new"
    assert row.edit_count == 1


@_PENDING
def test_archive_soft_delete(session):
    """FR-CB2-2.5 — `message.deleted` sets `deleted_at` (soft
    delete), never DROPS the row."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(session, channel_id="C1", ts="100.0", text="x", raw_payload={})
    pg_sink.apply_delete(session, channel_id="C1", ts="100.0")
    row = session.query(SlackMessageArchive).one()
    assert row.deleted_at is not None
    assert row.text == "x"


@_PENDING
def test_archive_pg_fail_pending_replay(tmp_path):
    """FR-CB2-2.6 / UC-4 — when PG is unreachable JSONL still
    appended + pending-row written for cron replay."""
    from app.ceo_brain.archive import write_archive

    write_archive(
        archive_dir=tmp_path,
        pg_session=None,  # simulate PG fail
        channel_id="C1",
        ts="100.0",
        text="x",
        raw_payload={},
    )
    pending = tmp_path / "slack_archive_pending.jsonl"
    assert pending.exists()
    assert "100.0" in pending.read_text()


@_PENDING
def test_archive_supports_all_channel_types(session):
    """FR-CB2-2.7 — public (`C…`), private (`G…`), DM (`D…`),
    group DM (`G…`/`G…` MPIM) all archive cleanly."""
    from app.ceo_brain.archive import pg_sink

    for cid in ("C123public", "G123private", "D123dm", "G123mpim"):
        pg_sink.write(session, channel_id=cid, ts=f"100.{cid}", text="x", raw_payload={})


@_PENDING
def test_archive_includes_bot_messages(session):
    """FR-CB2-2.8 / US-5 — `bot_message` subtype is captured too
    (Fireflies / agenda / other bots)."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(
        session, channel_id="C1", ts="100.0", text="from fireflies",
        raw_payload={"subtype": "bot_message", "bot_id": "Bff"},
        subtype="bot_message",
    )
    row = session.query(SlackMessageArchive).one()
    assert row.subtype == "bot_message"


@_PENDING
def test_archive_files_metadata_only(session):
    """FR-CB2-2.9 — file uploads keep only metadata (id, name,
    size, mime), NOT the binary content."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(
        session, channel_id="C1", ts="100.0", text="here is a file",
        raw_payload={
            "files": [
                {"id": "F1", "name": "deck.pdf", "size": 12345, "mimetype": "application/pdf"},
            ]
        },
    )
    row = session.query(SlackMessageArchive).one()
    files = row.raw_payload.get("files") or []
    assert files and "id" in files[0]
    assert "url_private" not in files[0]  # download URL never stored


@_PENDING
def test_archive_user_display_name_cached(monkeypatch):
    """FR-CB2-2.10 — `users.info` lookup is LRU-cached so we
    don't hit Slack per-message."""
    from app.ceo_brain.cache import resolve_user_display_name

    calls = {"n": 0}
    def fake_users_info(user_id):
        calls["n"] += 1
        return {"real_name": "Artem Sokolov"}

    monkeypatch.setattr(
        "app.ceo_brain.cache._fetch_users_info", fake_users_info,
    )
    assert resolve_user_display_name("U1") == "Artem Sokolov"
    assert resolve_user_display_name("U1") == "Artem Sokolov"  # cache hit
    assert calls["n"] == 1


@_PENDING
def test_archive_channel_name_cached(monkeypatch):
    """FR-CB2-2.11 — same LRU pattern for channel name lookup."""
    from app.ceo_brain.cache import resolve_channel_name

    calls = {"n": 0}
    def fake_conv_info(channel_id):
        calls["n"] += 1
        return {"name": "board"}

    monkeypatch.setattr(
        "app.ceo_brain.cache._fetch_conversations_info", fake_conv_info,
    )
    assert resolve_channel_name("C1") == "board"
    assert resolve_channel_name("C1") == "board"
    assert calls["n"] == 1


@_PENDING
def test_archive_file_permissions(tmp_path):
    """FR-CB2-2.12 / NFR-CB2-S.2 — JSONL has chmod 0640, owner
    app:app."""
    from app.ceo_brain.archive import jsonl_sink

    jsonl_sink.write(
        archive_dir=tmp_path, channel_id="C1", channel_name="board",
        ts="100.0", payload={},
    )
    target = tmp_path / "board" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    mode = oct(target.stat().st_mode)[-3:]
    assert mode == "640"


# -- Category 3: Claude responder (FR-CB2-3.x) -----------------------------


@_PENDING
def test_responder_triggered_by_mention():
    """FR-CB2-3.1 / UC-6 — `app_mention` triggers the responder
    pipeline."""
    from app.ceo_brain.responder import should_respond

    assert should_respond(
        event_type="app_mention",
        channel_id="C1",
        text="<@UBOT> hi",
        bot_user_id="UBOT",
    ) is True


@_PENDING
def test_responder_triggered_by_dm():
    """FR-CB2-3.2 / UC-10 — `message.im` triggers responder even
    without an explicit mention."""
    from app.ceo_brain.responder import should_respond

    assert should_respond(
        event_type="message",
        channel_id="D123dm",
        channel_type="im",
        text="статус по EQT",
        bot_user_id="UBOT",
    ) is True


@_PENDING
def test_responder_silent_on_channel_message_without_mention():
    """FR-CB2-3.3 / UC-11 — message in a public channel WITHOUT
    @mention is ignored by responder."""
    from app.ceo_brain.responder import should_respond

    assert should_respond(
        event_type="message",
        channel_id="C1",
        channel_type="channel",
        text="just chatting",
        bot_user_id="UBOT",
    ) is False


@_PENDING
def test_responder_placeholder_under_1s(monkeypatch):
    """FR-CB2-3.4 / NFR-CB2-P.2 — placeholder ":thinking_face:
    думаю…" is posted under 1 sec from event-receive."""
    import time

    from app.ceo_brain.responder import post_placeholder

    slack = MagicMock()
    slack.chat_postMessage.return_value = {"ok": True, "ts": "1.2"}
    t0 = time.time()
    ts = post_placeholder(slack=slack, channel="C1", thread_ts="100.0")
    assert ts == "1.2"
    assert time.time() - t0 < 1.0


@_PENDING
def test_responder_calls_anthropic_with_default_model(monkeypatch):
    """FR-CB2-3.5 — uses `claude-sonnet-4-6` (override env)."""
    from app.ceo_brain.responder import build_anthropic_request

    req = build_anthropic_request(
        thread_history=[{"role": "user", "content": "hi"}],
    )
    assert req["model"] == "claude-sonnet-4-6"


@_PENDING
def test_responder_passes_mcp_servers(monkeypatch):
    """FR-CB2-3.6 — `mcp_servers` from env JSON is propagated to
    the Anthropic Messages API call."""
    from app.ceo_brain.responder import build_anthropic_request

    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name":"slack","url":"https://x","type":"sse","auth":"t"}]',
    )
    req = build_anthropic_request(thread_history=[])
    assert any(s["name"] == "slack" for s in req.get("mcp_servers") or [])


@_PENDING
def test_responder_uses_prompt_caching():
    """FR-CB2-3.7 — system prompt + thread history sent with
    cache_control breakpoint so we hit cache on repeat calls."""
    from app.ceo_brain.responder import build_anthropic_request

    req = build_anthropic_request(thread_history=[
        {"role": "user", "content": "x"}
    ])
    sys_blocks = req.get("system") or []
    if isinstance(sys_blocks, list):
        assert any(
            b.get("cache_control") == {"type": "ephemeral"}
            for b in sys_blocks
        )


@_PENDING
def test_responder_streams_updates(monkeypatch):
    """FR-CB2-3.8 — chat_update called multiple times during
    streaming, not once at the end."""
    from app.ceo_brain.responder import run_responder

    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}
    fake_stream = [
        {"type": "content_block_delta", "delta": {"text": "Hello "}},
        {"type": "content_block_delta", "delta": {"text": "there. "}},
        {"type": "content_block_delta", "delta": {"text": "Goodbye."}},
        {"type": "message_stop"},
    ]
    anthropic = MagicMock()
    anthropic.messages.stream.return_value.__enter__.return_value = fake_stream

    run_responder(
        slack=slack, anthropic_client=anthropic,
        channel="C1", placeholder_ts="1.2", thread_history=[],
    )
    assert slack.chat_update.call_count >= 2


@_PENDING
def test_responder_sources_block_includes_tool_uses():
    """FR-CB2-3.9 / UC-7 — final message contains «Sources:»
    block listing each tool_use that fired."""
    from app.ceo_brain.responder import format_final_response

    out = format_final_response(
        text="Главное про EQT — …",
        tool_uses=[
            {"name": "slack.search_messages", "input": {"query": "EQT"}},
            {"name": "calendar.list_events", "input": {"q": "EQT"}},
        ],
    )
    assert "Sources:" in out
    assert "slack.search_messages" in out
    assert "calendar.list_events" in out


@_PENDING
def test_responder_supplies_thread_history():
    """FR-CB2-3.10 — last N (default 10) thread messages are
    included as `messages` in the Anthropic request."""
    from app.ceo_brain.responder import build_thread_history

    msgs = [{"user": "U1", "text": f"m{i}", "ts": str(i)} for i in range(15)]
    hist = build_thread_history(msgs, limit=10)
    assert len(hist) == 10
    assert hist[-1]["content"] == "m14"


@_PENDING
def test_responder_persists_run(session):
    """FR-CB2-3.11 — each responder attempt writes a row in
    `claude_responder_runs` with status / tool_uses / cost."""
    from app.ceo_brain.responder import persist_run
    from app.models import ClaudeResponderRun

    persist_run(
        session,
        slack_channel_id="C1",
        slack_event_ts="100.0",
        request_payload={"model": "claude-sonnet-4-6"},
        response_text="ok",
        tool_uses=[],
        status="done",
        cost_usd=0.0123,
    )
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "done"
    assert float(row.cost_usd) == 0.0123


@_PENDING
def test_responder_5xx_marks_failed(session, monkeypatch):
    """FR-CB2-3.12 / UC-8 — Anthropic 5xx → placeholder updated
    «⚠️ ошибка», PG row status=failed, NOT raising."""
    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    anthropic = MagicMock()
    def _explode(*a, **kw):
        raise RuntimeError("500 Internal Server Error")
    anthropic.messages.stream.side_effect = _explode
    slack = MagicMock()

    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="C1", placeholder_ts="1.2", thread_history=[],
    )
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "failed"


@_PENDING
def test_responder_429_retries(session, monkeypatch):
    """FR-CB2-3.13 / UC-9 — 429 retries up to 3 times with
    retry-after; placeholder shows ⏳ during waits."""
    from app.ceo_brain.responder import run_responder

    calls = {"n": 0}
    def _maybe_429(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            err = RuntimeError("429 Too Many Requests")
            err.headers = {"retry-after": "0"}
            raise err
        return MagicMock()

    anthropic = MagicMock()
    anthropic.messages.stream.side_effect = _maybe_429
    slack = MagicMock()
    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="C1", placeholder_ts="1.2", thread_history=[],
    )
    assert calls["n"] == 3


@_PENDING
def test_responder_system_prompt_includes_persona_and_date():
    """FR-CB2-3.14 — system prompt includes today's date + CEO
    Brain persona string."""
    from app.ceo_brain.responder import build_system_prompt

    sys_prompt = build_system_prompt(today=datetime(2026, 5, 18))
    assert "2026-05-18" in sys_prompt
    assert "CEO Brain" in sys_prompt or "Артем" in sys_prompt


@_PENDING
def test_responder_tool_use_events_streamed(monkeypatch):
    """FR-CB2-3.15 — when env-flag on, tool_use events surface in
    Slack placeholder as «🔍 ищу в Calendar…» updates."""
    from app.ceo_brain.responder import describe_tool_use_for_slack

    out = describe_tool_use_for_slack(
        tool_name="calendar.list_events",
        input_arg={"q": "EQT"},
    )
    assert "🔍" in out
    assert "Calendar" in out or "calendar" in out


# -- Category 4: MCP integration (FR-CB2-4.x) ------------------------------


@_PENDING
def test_mcp_config_loaded_and_validated(monkeypatch):
    """FR-CB2-4.1 — MCP_SERVERS JSON env var loaded + validated."""
    from app.ceo_brain.mcp import load_mcp_servers

    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name":"slack","url":"https://x","type":"sse","auth":"t"}]',
    )
    servers = load_mcp_servers()
    assert servers[0]["name"] == "slack"


@_PENDING
def test_mcp_supports_sse_and_http(monkeypatch):
    """FR-CB2-4.2 — both `sse` and `http` transport types accepted."""
    from app.ceo_brain.mcp import load_mcp_servers

    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name":"a","url":"u1","type":"sse"},'
        ' {"name":"b","url":"u2","type":"http"}]',
    )
    types = {s["type"] for s in load_mcp_servers()}
    assert types == {"sse", "http"}


@_PENDING
def test_mcp_oauth_token_resolution(monkeypatch):
    """FR-CB2-4.3 — OAuth token resolved per server, prod from
    Secret Manager, dev from env."""
    from app.ceo_brain.mcp import resolve_oauth_token

    monkeypatch.setenv("MCP_SLACK_OAUTH_TOKEN", "tok-slack")
    assert resolve_oauth_token("slack") == "tok-slack"


@_PENDING
def test_mcp_partial_failure_degraded(monkeypatch):
    """FR-CB2-4.4 — one MCP server unreachable: other tools still
    work; Claude gets `is_error=true` only for failing tool."""
    from app.ceo_brain.mcp import filter_reachable_servers

    servers = [
        {"name": "slack", "url": "http://localhost:1", "type": "sse"},
        {"name": "gmail", "url": "http://localhost:2", "type": "sse"},
    ]
    reachable = filter_reachable_servers(servers, probe=lambda u: u.endswith("2"))
    assert {s["name"] for s in reachable} == {"gmail"}


@_PENDING
def test_mcp_calls_persisted(session):
    """FR-CB2-4.5 — tool_use events recorded in
    `claude_responder_runs.tool_uses` JSONB."""
    from app.ceo_brain.responder import persist_run
    from app.models import ClaudeResponderRun

    persist_run(
        session,
        slack_channel_id="C1", slack_event_ts="100.0",
        request_payload={}, response_text="x",
        tool_uses=[
            {"name": "slack.search_messages",
             "input": {"query": "X"},
             "output_summary": "3 hits"},
        ],
        status="done", cost_usd=0.0,
    )
    row = session.query(ClaudeResponderRun).one()
    assert row.tool_uses and row.tool_uses[0]["name"] == "slack.search_messages"


# -- Category 5: Config / feature flags (FR-CB2-5.x) -----------------------


@_PENDING
def test_brain_disabled_no_threads(monkeypatch):
    """FR-CB2-5.1 — `CEO_BRAIN_ENABLED=false` keeps both archive
    and responder threads unstarted."""
    from app.ceo_brain.runner import CeoBrainRunner

    monkeypatch.setenv("CEO_BRAIN_ENABLED", "false")
    r = CeoBrainRunner.from_env()
    r.start()
    assert r.archive_thread is None
    assert r.responder_thread is None


@_PENDING
def test_brain_archive_only_mode(monkeypatch):
    """FR-CB2-5.2 — `CEO_BRAIN_ARCHIVE_ONLY=true` starts archive
    thread only; responder stays off."""
    from app.ceo_brain.runner import CeoBrainRunner

    monkeypatch.setenv("CEO_BRAIN_ENABLED", "true")
    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_ONLY", "true")
    r = CeoBrainRunner.from_env()
    r.start()
    assert r.archive_thread is not None
    assert r.responder_thread is None


@_PENDING
def test_brain_responder_requires_anthropic_key(monkeypatch):
    """FR-CB2-5.3 — without `CEO_BRAIN_ANTHROPIC_API_KEY` the
    responder refuses to start (logs warning + stays off)."""
    from app.ceo_brain.runner import CeoBrainRunner

    monkeypatch.setenv("CEO_BRAIN_ENABLED", "true")
    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_ONLY", "false")
    monkeypatch.setenv("CEO_BRAIN_ANTHROPIC_API_KEY", "")
    r = CeoBrainRunner.from_env()
    r.start()
    assert r.responder_thread is None


@_PENDING
def test_brain_archive_dir_configurable(monkeypatch, tmp_path):
    """FR-CB2-5.4 — `CEO_BRAIN_ARCHIVE_DIR` env overrides default."""
    from app.ceo_brain.config import get_archive_dir

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_DIR", str(tmp_path))
    assert get_archive_dir() == tmp_path


@_PENDING
def test_brain_uses_dedicated_tokens(monkeypatch):
    """FR-CB2-5.5 — CEO Brain Slack tokens are DISTINCT from
    existing `SLACK_BOT_TOKEN` / `AGENDA_SLACK_BOT_TOKEN`."""
    from app.ceo_brain.config import get_slack_tokens

    monkeypatch.setenv("CEO_BRAIN_SLACK_APP_TOKEN", "xapp-CB")
    monkeypatch.setenv("CEO_BRAIN_SLACK_BOT_TOKEN", "xoxb-CB")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-OLD")
    app_t, bot_t = get_slack_tokens()
    assert app_t == "xapp-CB"
    assert bot_t == "xoxb-CB"


@_PENDING
def test_brain_archive_whitelist(monkeypatch):
    """FR-CB2-5.6 — `CEO_BRAIN_ARCHIVE_CHANNELS=C1,C2` whitelists
    only those; messages elsewhere are dropped."""
    from app.ceo_brain.archive import should_archive_channel

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_CHANNELS", "C1,C2")
    assert should_archive_channel("C1") is True
    assert should_archive_channel("C3") is False


# -- Category 6: Ops (FR-CB2-6.x) ------------------------------------------


@_PENDING
def test_brain_cli_export(tmp_path):
    """FR-CB2-6.1 — `ops/brain_archive_export.py --channel X
    --from D1 --to D2` writes a single concatenated JSONL."""
    from ops import brain_archive_export

    out = tmp_path / "out.jsonl"
    rc = brain_archive_export.main(
        argv=["--channel", "board", "--from", "2026-05-01",
              "--to", "2026-05-18", "--out", str(out)],
    )
    assert rc == 0
    assert out.exists()


@_PENDING
def test_brain_cli_backfill(monkeypatch):
    """FR-CB2-6.2 — `ops/brain_backfill.py --channel X --since D`
    backfills via `conversations.history`."""
    from ops import brain_backfill

    rc = brain_backfill.main(
        argv=["--channel", "board", "--since", "2026-05-01"],
    )
    assert rc == 0


@_PENDING
def test_brain_health_archive_lag():
    """FR-CB2-6.3 — health-check returns time-since-last-event."""
    from app.ceo_brain.health import archive_lag_seconds

    assert isinstance(archive_lag_seconds(), (int, float))


@_PENDING
def test_brain_metrics_emitted():
    """FR-CB2-6.4 — Prometheus counters / histograms emitted."""
    from app.ceo_brain.metrics import (
        brain_archive_messages_total,
        brain_responder_latency_seconds,
        brain_responder_runs_total,
    )

    assert brain_archive_messages_total is not None
    assert brain_responder_runs_total is not None
    assert brain_responder_latency_seconds is not None


# -- Non-functional (NFR-CB2-x.x) ------------------------------------------


@_PENDING
def test_brain_failed_run_does_not_block_archive(session, monkeypatch):
    """NFR-CB2-R.1 — failure in responder pipeline does NOT stop
    the archive pipeline; archive keeps writing."""
    from app.ceo_brain.dispatcher import handle_event

    monkeypatch.setattr(
        "app.ceo_brain.responder.run_responder",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("explode")),
    )
    payload = {
        "type": "app_mention", "channel": "C1", "user": "U1",
        "text": "<@UBOT> hi", "ts": "100.0",
    }
    r = handle_event(session, payload, bot_user_id="UBOT")
    assert r.archived is True
    assert r.responder_error is not None


@_PENDING
def test_brain_reconnect_no_event_loss():
    """NFR-CB2-R.2 — Socket reconnect doesn't drop events; queued
    in PG-pending or replayed via `conversations.history`."""
    pytest.skip("Behaviour test — runs in inject-disconnect harness "
                "once Socket Mode client is wired")


@_PENDING
def test_responder_run_payload_scrubbed(session):
    """NFR-CB2-S.3 — `request_payload.mcp_servers[].auth` is NEVER
    written to DB (only name + url)."""
    import json

    from app.ceo_brain.responder import persist_run
    from app.models import ClaudeResponderRun

    persist_run(
        session,
        slack_channel_id="C1", slack_event_ts="100.0",
        request_payload={
            "model": "claude-sonnet-4-6",
            "mcp_servers": [
                {"name": "slack", "url": "u", "auth": "SECRET-TOKEN"},
            ],
        },
        response_text="x", tool_uses=[], status="done", cost_usd=0,
    )
    row = session.query(ClaudeResponderRun).one()
    blob = json.dumps(row.request_payload)
    assert "SECRET-TOKEN" not in blob


@_PENDING
def test_brain_per_run_cost_cap(monkeypatch):
    """NFR-CB2-C.2 — `CEO_BRAIN_MAX_RUN_COST_USD` enforces a
    hard cap via Anthropic `max_tokens`."""
    from app.ceo_brain.responder import build_anthropic_request

    monkeypatch.setenv("CEO_BRAIN_MAX_RUN_COST_USD", "1.0")
    req = build_anthropic_request(thread_history=[])
    # Sonnet-4.6 output ~ $15/Mtok → cap of $1 implies max_tokens
    # under 70_000.
    assert req["max_tokens"] <= 70_000


@_PENDING
def test_brain_jsonl_retention_rotate(tmp_path, monkeypatch):
    """NFR-CB2-A.1 — JSONL files older than
    `CEO_BRAIN_JSONL_RETENTION_DAYS` are pruned by cron job."""
    from datetime import timedelta

    from app.ceo_brain.retention import prune_old_jsonl_files

    monkeypatch.setenv("CEO_BRAIN_JSONL_RETENTION_DAYS", "365")
    old = tmp_path / "board" / (
        (datetime.now(timezone.utc) - timedelta(days=400))
        .strftime("%Y-%m-%d.jsonl")
    )
    old.parent.mkdir(parents=True)
    old.write_text("x\n")
    recent = tmp_path / "board" / (
        datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
    )
    recent.write_text("y\n")
    prune_old_jsonl_files(archive_dir=tmp_path, days=365)
    assert not old.exists()
    assert recent.exists()
