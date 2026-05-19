"""FR-CB2-200 — CEO Brain Bot: Slack archive + Claude responder w/ MCP.

Spec: ``SPEC_CEO_BRAIN_BOT_v0.1.md``.

Categories 1, 2, 4, 5 + part of NFR are GREEN as of Sprint 1
(archive + config + MCP loader). Category 3 (responder pipeline)
and Category 6 (ops CLI / metrics) remain `xfail(strict=True)` —
they land in Sprints 2-5.

Test-IDs map 1:1 to FR/NFR IDs in the spec — see §12 traceability
matrix.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

_PENDING = pytest.mark.xfail(
    strict=True,
    reason="CEO Brain Bot — Sprint 2+ impl pending",
)


@pytest.fixture(autouse=True)
def _clear_ceo_brain_caches():
    """Each test starts with empty LRU caches so monkeypatched
    fetcher calls aren't masked by leftover entries."""
    try:
        from app.ceo_brain.cache import (
            resolve_channel_name,
            resolve_user_display_name,
        )
        resolve_user_display_name.cache_clear()
        resolve_channel_name.cache_clear()
    except ImportError:
        pass
    yield


# -- Category 1: Slack Events ingestion (FR-CB2-1.x) ------------------------


def test_brain_socket_connect_ok(monkeypatch):
    """FR-CB2-1.1 — Socket Mode connection opens with the dedicated
    bot/app tokens."""
    from app.ceo_brain.socket_client import open_socket_connection

    monkeypatch.setenv("CEO_BRAIN_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("CEO_BRAIN_SLACK_BOT_TOKEN", "xoxb-test")

    seen: dict = {}
    def factory(*, app_token, bot_token):
        seen["app"] = app_token
        seen["bot"] = bot_token
        return MagicMock(name="socket-client")

    handle = open_socket_connection(client_factory=factory)
    assert handle is not None
    assert seen == {"app": "xapp-test", "bot": "xoxb-test"}


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


def test_brain_socket_reconnect_with_backoff():
    """FR-CB2-1.3 — disconnect triggers exponential backoff
    2s / 4s / 8s / 16s before each reconnect attempt."""
    from app.ceo_brain.socket_client import compute_reconnect_delays

    assert compute_reconnect_delays(max_attempts=4) == [2, 4, 8, 16]


def test_brain_signature_verification_rejects_invalid():
    """FR-CB2-1.4 — HTTP Events variant rejects bad HMAC signature."""
    from app.ceo_brain.http_events import verify_signature

    assert verify_signature(
        body=b"{}",
        timestamp="1700000000",
        signature="v0=deadbeef",
        signing_secret="wrong",
    ) is False


def test_brain_signature_verification_accepts_valid():
    """FR-CB2-1.4 (positive) — well-formed HMAC over v0:ts:body
    matches."""
    import hashlib
    import hmac

    from app.ceo_brain.http_events import verify_signature

    body = b'{"hello":"world"}'
    ts = "1700000000"
    secret = "shh"
    sig = "v0=" + hmac.new(
        secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256,
    ).hexdigest()
    assert verify_signature(
        body=body, timestamp=ts, signature=sig, signing_secret=secret,
    ) is True


def test_brain_dedup_repeat_event(session, tmp_path, monkeypatch):
    """FR-CB2-1.5 — replaying the same event_id is a no-op."""
    from app.ceo_brain.dispatcher import handle_event

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_DIR", str(tmp_path))
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


def test_brain_skips_self_messages(session, tmp_path, monkeypatch):
    """FR-CB2-1.6 / NFR-CB2-R.3 — bot ignores its own messages."""
    from app.ceo_brain.dispatcher import handle_event

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_DIR", str(tmp_path))
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
    assert r.archived is False


def test_history_poller_singleton_no_duplicates_on_reconnect(
    monkeypatch, tmp_path,
):
    """FR-CB2-1.7 — repeated calls to the poller's singleton starter
    must NOT spawn additional daemon threads. Socket-Mode reconnect
    loop calls `_attach_handlers` on every reconnect; if the poller
    weren't singleton, pollers would accumulate and dispatch the
    same Slack event N times → duplicate bot replies (operator-
    confirmed bug, 2026-05-19)."""
    from app.ceo_brain import slack_handler

    # Reset any singleton state from earlier tests.
    slack_handler._reset_history_poller_singleton()

    spawned: list = []

    class _FakePoller:
        def __init__(self, **kwargs):
            spawned.append(kwargs)
        def start(self):
            return MagicMock()

    monkeypatch.setattr(slack_handler, "SlackHistoryPoller", _FakePoller)
    monkeypatch.setattr(
        slack_handler, "discover_operator_dm_channels",
        lambda s: ["D123"],
    )

    fake_settings = MagicMock()
    fake_settings.ceo_brain_archive_dir = str(tmp_path)

    for _ in range(5):  # simulate 5 reconnects
        slack_handler.start_history_poller_singleton(
            slack_client=MagicMock(),
            bot_user_id="UBOT",
            responder=lambda p: None,
            archive_dir=tmp_path,
            settings=fake_settings,
        )
    assert len(spawned) == 1, (
        f"expected exactly one poller, got {len(spawned)}"
    )


def test_dispatcher_responder_in_process_dedup(session, tmp_path, monkeypatch):
    """FR-CB2-1.8 — even when two paths (Socket-Mode push +
    history-poller) race past the archive UNIQUE dedup, the
    responder must fire exactly ONCE per (channel, ts) within the
    dedup TTL window."""
    from app.ceo_brain.dispatcher import (
        _reset_responder_dedup_for_tests,
        handle_event,
    )

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_DIR", str(tmp_path))
    _reset_responder_dedup_for_tests()

    fired: list = []

    def _responder(p):
        fired.append(p.get("ts"))

    payload = {
        "type": "message",
        "channel": "D1",
        "channel_type": "im",
        "user": "U1",
        "text": "hi",
        "ts": "1779100100.000111",
    }

    # First call — should write archive AND fire responder.
    r1 = handle_event(session, payload, responder=_responder)
    assert r1.responder_triggered is True
    assert fired == ["1779100100.000111"]

    # Second call (same payload) — archive dedup kicks in, no
    # responder fire.
    r2 = handle_event(session, payload, responder=_responder)
    assert r2.duplicate is True
    assert fired == ["1779100100.000111"]

    # Now simulate the race: a SEPARATE archive row didn't get
    # written (e.g. transient error) so archive dedup misses, but
    # in-process dedup should still block the second responder.
    payload2 = dict(payload, ts="1779100200.000222")
    # Pre-record the ts in the in-process dedup set as if a parallel
    # path already fired it; archive write hasn't happened yet.
    from app.ceo_brain.dispatcher import _mark_responder_dispatched
    _mark_responder_dispatched("D1", "1779100200.000222")
    r3 = handle_event(session, payload2, responder=_responder)
    # archive write still happens — it's the responder that must
    # NOT fire twice.
    assert "1779100200.000222" not in fired


# -- Category 2: Slack archive (FR-CB2-2.x) --------------------------------


def test_archive_jsonl_append(tmp_path):
    """FR-CB2-2.1 — JSONL append-only at
    `<archive_dir>/<channel>/YYYY-MM-DD.jsonl`."""
    from app.ceo_brain.archive import jsonl_sink

    target = jsonl_sink.write(
        archive_dir=tmp_path,
        channel_id="C1",
        channel_name="board",
        ts="1779100000.000001",
        payload={"user": "U1", "text": "hello"},
    )
    assert target.exists()
    assert target.parent.name == "board"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert target.name == f"{today}.jsonl"
    line = target.read_text().strip()
    assert json.loads(line)["text"] == "hello"


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
    assert row.raw_payload == {"foo": "bar"}


def test_archive_unique_channel_ts(session):
    """FR-CB2-2.3 / UC-3 — UNIQUE(channel_id, ts) so retries don't
    create duplicates."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(
        session, channel_id="C1", ts="100.0", text="a", raw_payload={},
    )
    pg_sink.write(
        session, channel_id="C1", ts="100.0", text="a", raw_payload={},
    )
    assert session.query(SlackMessageArchive).count() == 1


def test_archive_edit_overwrites_pg_keeps_jsonl_history(session, tmp_path):
    """FR-CB2-2.4 / UC-5 — `message.changed` updates text + bumps
    edit_count; original ts row stays singular in PG."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(
        session, channel_id="C1", ts="100.0", text="old", raw_payload={},
    )
    pg_sink.apply_edit(
        session, channel_id="C1", ts="100.0", text="new",
        raw_payload={"subtype": "message_changed"},
    )
    row = session.query(SlackMessageArchive).one()
    assert row.text == "new"
    assert row.edit_count == 1


def test_archive_soft_delete(session):
    """FR-CB2-2.5 — `message.deleted` sets `deleted_at` (soft
    delete), never DROPS the row."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(
        session, channel_id="C1", ts="100.0", text="x", raw_payload={},
    )
    pg_sink.apply_delete(session, channel_id="C1", ts="100.0")
    row = session.query(SlackMessageArchive).one()
    assert row.deleted_at is not None
    assert row.text == "x"


def test_archive_pg_fail_pending_replay(tmp_path):
    """FR-CB2-2.6 / UC-4 — when PG is unreachable JSONL still
    appended + pending-row written for cron replay."""
    from app.ceo_brain.archive import PENDING_FILENAME, write_archive

    res = write_archive(
        archive_dir=tmp_path,
        pg_session=None,  # simulate PG fail
        channel_id="C1",
        channel_name="board",
        ts="100.0",
        text="x",
        raw_payload={"a": 1},
    )
    pending = tmp_path / PENDING_FILENAME
    assert pending.exists()
    assert "100.0" in pending.read_text()
    assert res["pending"] is True
    assert res["jsonl_path"].exists()


def test_archive_supports_all_channel_types(session):
    """FR-CB2-2.7 — public (`C…`), private (`G…`), DM (`D…`),
    group DM (`G…`/`G…` MPIM) all archive cleanly."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    for cid in ("C123public", "G123private", "D123dm", "G123mpim"):
        pg_sink.write(
            session, channel_id=cid, ts=f"100.{cid}",
            text="x", raw_payload={},
        )
    assert session.query(SlackMessageArchive).count() == 4


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


def test_archive_files_metadata_only(session):
    """FR-CB2-2.9 — file uploads keep only metadata (id, name,
    size, mime), NOT the binary content. Slack `files` lists are
    passed through as-is — operator can choose whether to strip
    download URLs at the dispatcher layer when needed; this test
    just guards that we never inline binary payloads ourselves."""
    from app.ceo_brain.archive import pg_sink
    from app.models import SlackMessageArchive

    pg_sink.write(
        session, channel_id="C1", ts="100.0", text="here is a file",
        raw_payload={
            "files": [
                {"id": "F1", "name": "deck.pdf", "size": 12345,
                 "mimetype": "application/pdf"},
            ]
        },
    )
    row = session.query(SlackMessageArchive).one()
    files = row.raw_payload.get("files") or []
    assert files and "id" in files[0]
    # We never store binary content (only the slack-side metadata)
    assert "content_b64" not in files[0]


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
    assert resolve_user_display_name("U1") == "Artem Sokolov"
    assert calls["n"] == 1


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


def test_archive_file_permissions(tmp_path):
    """FR-CB2-2.12 / NFR-CB2-S.2 — JSONL has chmod 0640."""
    import os
    import stat

    from app.ceo_brain.archive import jsonl_sink

    target = jsonl_sink.write(
        archive_dir=tmp_path, channel_id="C1", channel_name="board",
        ts="100.0", payload={"text": "x"},
    )
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o640


# -- Category 3: Claude responder (FR-CB2-3.x) -----------------------------


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


def test_responder_triggered_by_dm():
    """FR-CB2-3.2 / UC-10 — `message.im` triggers responder."""
    from app.ceo_brain.responder import should_respond

    assert should_respond(
        event_type="message", channel_id="D123dm",
        channel_type="im", text="статус по EQT", bot_user_id="UBOT",
    ) is True


def test_responder_triggered_by_dm_thread_reply(session, tmp_path, monkeypatch):
    """FR-CB2-3.2 — thread-reply внутри DM ОБЯЗАН триггерить
    responder. Slack отправляет `message.im` event с
    `thread_ts` set; channel_type остаётся "im". Operator
    наблюдал баг 2026-05-19 — top-level DM отвечал, а
    thread-reply молчал."""
    from app.ceo_brain.dispatcher import handle_event

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_DIR", str(tmp_path))
    captured: dict = {}

    def _responder(payload):
        captured["called"] = True
        captured["payload"] = payload

    payload = {
        "type": "message",
        "channel": "D0ASY5QF6UX",
        "channel_type": "im",
        "user": "U_OP",
        "text": "тест 20",
        "ts": "1779170900.000100",
        "thread_ts": "1779170756.546959",
        "event_id": "EvDM_threadreply",
    }

    r = handle_event(
        session, payload,
        bot_user_id="UBOT",
        responder=_responder,
        archive_dir=tmp_path,
    )
    assert r.responder_triggered is True
    assert captured.get("called") is True, (
        "Thread-reply в DM должен запускать responder pipeline "
        "(FR-CB2-3.2)"
    )


def test_responder_silent_on_channel_message_without_mention():
    """FR-CB2-3.3 / UC-11 — silent in channels without @mention."""
    from app.ceo_brain.responder import should_respond

    assert should_respond(
        event_type="message", channel_id="C1",
        channel_type="channel", text="just chatting",
        bot_user_id="UBOT",
    ) is False


def test_responder_placeholder_under_1s():
    """FR-CB2-3.4 — placeholder posted under 1 sec."""
    import time

    from app.ceo_brain.responder import post_placeholder

    slack = MagicMock()
    slack.chat_postMessage.return_value = {"ok": True, "ts": "1.2"}
    t0 = time.time()
    ts = post_placeholder(slack=slack, channel="C1", thread_ts="100.0")
    assert ts == "1.2"
    assert time.time() - t0 < 1.0


def test_responder_calls_anthropic_with_default_model():
    """FR-CB2-3.5 — uses `claude-sonnet-4-6`."""
    from app.ceo_brain.responder import build_anthropic_request

    req = build_anthropic_request(
        thread_history=[{"role": "user", "content": "hi"}],
    )
    assert req["model"] == "claude-sonnet-4-6"


def test_responder_passes_mcp_servers(monkeypatch):
    """FR-CB2-3.6 — `mcp_servers` propagated to Anthropic call."""
    from app.ceo_brain.responder import build_anthropic_request

    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name":"slack","url":"https://x","authorization_token":"t"}]',
    )
    req = build_anthropic_request(thread_history=[])
    assert any(s["name"] == "slack" for s in req.get("mcp_servers") or [])


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
def test_responder_streams_updates():
    """FR-CB2-3.8 — chat_update called multiple times."""
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


def test_responder_sources_block_includes_tool_uses():
    """FR-CB2-3.9 / UC-7 — final message contains `Sources:`."""
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


def test_responder_supplies_thread_history():
    """FR-CB2-3.10 — last N (default 10) thread messages."""
    from app.ceo_brain.responder import build_thread_history

    msgs = [{"user": "U1", "text": f"m{i}", "ts": str(i)} for i in range(15)]
    hist = build_thread_history(msgs, limit=10)
    assert len(hist) == 10
    assert hist[-1]["content"] == "m14"


def test_responder_persists_run(session):
    """FR-CB2-3.11 — each responder attempt writes a row."""
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


def test_responder_5xx_marks_failed(session):
    """FR-CB2-3.12 / UC-8 — Anthropic 5xx → status=failed."""
    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    anthropic = MagicMock()
    def _explode(*a, **kw):
        raise RuntimeError("500 Internal Server Error")
    # FR-CB2-3.22 — main turn uses messages.create (no MCP path).
    anthropic.messages.create.side_effect = _explode
    slack = MagicMock()

    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="C1", placeholder_ts="1.2", thread_history=[],
    )
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "failed"


def test_responder_429_retries(session):
    """FR-CB2-3.13 / UC-9 — 429 retries up to 3 times."""
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
    # FR-CB2-3.22 — main turn uses messages.create (no MCP path).
    anthropic.messages.create.side_effect = _maybe_429
    slack = MagicMock()
    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="C1", placeholder_ts="1.2", thread_history=[],
    )
    assert calls["n"] == 3


def test_responder_system_prompt_includes_persona_and_date():
    """FR-CB2-3.14 — system prompt includes today's date."""
    from app.ceo_brain.responder import build_system_prompt

    sys_prompt = build_system_prompt(today=datetime(2026, 5, 18))
    assert "2026-05-18" in sys_prompt
    assert "CEO Brain" in sys_prompt or "Артем" in sys_prompt


def test_slack_handler_filters_bot_messages_from_thread_history():
    """FR-CB2-3.20 — operator-observed 2026-05-19: after
    `post_placeholder("🤔 думаю...")`, the responder fetched the
    whole thread and added EVERY message (including its own
    placeholder) to history as `role=user`. The last "user
    message" became "🤔 думаю..." and the model honestly replied
    «получил только эмодзи». Fix: skip messages authored by the
    bot when assembling thread context."""
    from app.ceo_brain.slack_handler import (
        _build_thread_history_from_replies,
    )

    bot_user_id = "UBOTSELF"
    replies = [
        {"user": "U_OP", "text": "что сегодня обсудили с Йоханом?"},
        # Bot placeholder — must be filtered out.
        {"user": bot_user_id, "text": "🤔 думаю…"},
        # Earlier bot answer — must be filtered out.
        {"user": bot_user_id, "text": "ранее: фандрайзинг идёт ок"},
        # Another operator message — must be kept.
        {"user": "U_OP", "text": "уточни про Jochen"},
        # Generic bot-authored entry (no `user`, just `bot_id`).
        {"bot_id": "B1", "text": "bot relayed reply"},
        # Empty text — also skipped.
        {"user": "U_OP", "text": ""},
    ]
    out = _build_thread_history_from_replies(
        replies, bot_user_id=bot_user_id,
    )
    contents = [m["content"] for m in out]
    assert contents == [
        "что сегодня обсудили с Йоханом?",
        "уточни про Jochen",
    ]
    assert all(m["role"] == "user" for m in out)


def test_smart_mcp_routing_picks_calendar_for_meeting_questions():
    """FR-CB2-3.25 — for a question about meetings/transcripts the
    classifier should pick `n8n_calendar` (and maybe `n8n_drive` for
    Telegram context). Other MCPs are not needed → handshake faster
    & more reliable."""
    from types import SimpleNamespace

    from app.ceo_brain.responder import select_mcps_for_question

    anthropic = MagicMock()
    anthropic.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(
            type="text",
            text='{"mcps": ["n8n_calendar", "n8n_drive"]}',
        )],
    )

    selected = select_mcps_for_question(
        question="что обсудили с Йоханом на встрече сегодня?",
        all_servers=[
            {"name": "n8n_main", "url": "u1"},
            {"name": "n8n_calendar", "url": "u2"},
            {"name": "n8n_gmail", "url": "u3"},
            {"name": "n8n_drive", "url": "u4"},
            {"name": "n8n_rocketreach", "url": "u5"},
            {"name": "n8n_hubspot", "url": "u6"},
        ],
        anthropic_client=anthropic,
    )
    names = [s["name"] for s in selected]
    assert "n8n_calendar" in names
    assert "n8n_drive" in names
    # Irrelevant MCPs filtered out.
    assert "n8n_hubspot" not in names
    assert "n8n_rocketreach" not in names
    # Used haiku (cheaper / faster) for classification.
    call_kwargs = anthropic.messages.create.call_args.kwargs
    assert "haiku" in call_kwargs.get("model", "").lower()


def test_smart_mcp_routing_fallback_on_classifier_failure():
    """FR-CB2-3.25 — if the classifier raises OR returns malformed
    JSON, fall back to the FULL list (no degradation)."""
    from app.ceo_brain.responder import select_mcps_for_question

    all_servers = [
        {"name": "n8n_calendar", "url": "u1"},
        {"name": "n8n_main", "url": "u2"},
    ]

    # Case 1 — classifier raises.
    anthropic_err = MagicMock()
    anthropic_err.messages.create.side_effect = RuntimeError("boom")
    out_err = select_mcps_for_question(
        question="?", all_servers=all_servers,
        anthropic_client=anthropic_err,
    )
    assert out_err == all_servers

    # Case 2 — classifier returns nonsense.
    from types import SimpleNamespace
    anthropic_bad = MagicMock()
    anthropic_bad.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="not json")],
    )
    out_bad = select_mcps_for_question(
        question="?", all_servers=all_servers,
        anthropic_client=anthropic_bad,
    )
    assert out_bad == all_servers


def test_responder_progressive_mcp_degradation_on_retry(session):
    """FR-CB2-3.24 — when MCP handshake keeps failing, each retry
    attempt (starting from the 3rd) drops one MCP server from the
    tail of the list. Smaller subset → fewer parallel handshakes →
    higher chance the request succeeds. The last 2 MCPs always
    stay (minimum viable set)."""
    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    calls: list = []

    def _fail_n_times(*a, **kw):
        # Capture mcp_servers count per attempt for assertion.
        n = len(kw.get("mcp_servers") or [])
        calls.append(n)
        if len(calls) < 4:  # first 3 attempts fail
            raise RuntimeError(
                "Error code: 400 - Connection error while "
                "communicating with MCP server."
            )
        # 4th attempt succeeds.
        from types import SimpleNamespace
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="OK")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100, output_tokens=10,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    anthropic = MagicMock()
    anthropic.beta.messages.create.side_effect = _fail_n_times
    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}

    import os
    prev = os.environ.get("MCP_SERVERS")
    os.environ["MCP_SERVERS"] = (
        '['
        '{"name":"a","url":"https://x.invalid/a"},'
        '{"name":"b","url":"https://x.invalid/b"},'
        '{"name":"c","url":"https://x.invalid/c"},'
        '{"name":"d","url":"https://x.invalid/d"},'
        '{"name":"e","url":"https://x.invalid/e"},'
        '{"name":"f","url":"https://x.invalid/f"}'
        ']'
    )
    try:
        run_responder(
            slack=slack, anthropic_client=anthropic, db_session=session,
            channel="D1", placeholder_ts="1.2",
            thread_history=[{"role": "user", "content": "вопрос"}],
            sleep=lambda s: None,
        )
    finally:
        if prev is None:
            os.environ.pop("MCP_SERVERS", None)
        else:
            os.environ["MCP_SERVERS"] = prev

    # 4 attempts total. Expected MCP counts:
    #  attempt 1: 6 (full set)
    #  attempt 2: 6 (first retry, same set)
    #  attempt 3: 5 (drop one)
    #  attempt 4: 4 (drop another) — succeeds
    assert calls == [6, 6, 5, 4], (
        f"expected progressive degradation, got {calls}"
    )
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "done"


def test_responder_retries_on_mcp_handshake_connection_error(session):
    """FR-CB2-3.23 — Anthropic's parallel MCP handshake periodically
    fails with `BadRequestError: Connection error while communicating
    with MCP server`. Each individual server works (verified
    one-by-one), but one in 6+ stalls on a given handshake. The
    error is transient — responder must retry up to 3 times with
    backoff before giving up."""
    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    calls = {"n": 0}

    def _maybe_mcp_handshake_fail(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(
                "Error code: 400 - {'type':'error','error':{"
                "'type':'invalid_request_error','message':"
                "'Connection error while communicating with MCP "
                "server. The server may be unavailable or "
                "unresponsive.'}}"
            )
        # Third call succeeds with a clean text response.
        from types import SimpleNamespace
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="OK")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100, output_tokens=10,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    anthropic = MagicMock()
    anthropic.messages.create.side_effect = _maybe_mcp_handshake_fail
    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}

    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="D1", placeholder_ts="1.2",
        thread_history=[{"role": "user", "content": "вопрос"}],
        sleep=lambda s: None,  # speed up retries in test
    )

    assert calls["n"] == 3, (
        "expected 2 transient failures + 1 success = 3 attempts"
    )
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "done"
    assert "OK" in (row.response_text or "")


def test_run_responder_uses_create_for_main_turn(session):
    """FR-CB2-3.22 — main turn goes through
    `beta.messages.create` (non-stream) when MCP servers are
    configured. Streaming + MCP + parallel tool_use truncates the
    response before tool_results arrive (operator-observed
    2026-05-19), so non-streaming is the reliable path."""
    from types import SimpleNamespace

    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    # Full main turn — model called a tool, got result, wrote
    # synthesis. With create() we get all blocks in one shot.
    answer_text = SimpleNamespace(
        type="text", text="Сегодня обсудили Series A: $120M собрано.",
    )
    final_msg = SimpleNamespace(
        content=[answer_text],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=500, output_tokens=200,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    anthropic = MagicMock()
    anthropic.beta.messages.create.return_value = final_msg
    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}

    import os
    prev = os.environ.get("MCP_SERVERS")
    os.environ["MCP_SERVERS"] = (
        '[{"name":"n8n_calendar","url":"https://example.invalid/x"}]'
    )
    try:
        run_responder(
            slack=slack, anthropic_client=anthropic, db_session=session,
            channel="D1", placeholder_ts="1.2",
            thread_history=[
                {"role": "user", "content": "вопрос про фандрайзинг"},
            ],
        )
    finally:
        if prev is None:
            os.environ.pop("MCP_SERVERS", None)
        else:
            os.environ["MCP_SERVERS"] = prev

    # Main turn used beta.messages.create (non-stream).
    assert anthropic.beta.messages.create.called
    call_kwargs = anthropic.beta.messages.create.call_args.kwargs
    assert "mcp_servers" in call_kwargs
    assert "betas" in call_kwargs

    # Stream NOT used for main turn.
    assert anthropic.beta.messages.stream.call_count == 0

    # Result persisted with the answer text.
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "done"
    assert "Series A" in (row.response_text or "")


def test_responder_system_prompt_mandates_transcript_fetch_after_search():
    """FR-CB2-3.21 — model must always call get_zoom_transcript /
    get_meeting after a search_* tool. Operator-observed 2026-05-19:
    bot called 3 search-tools, never followed up with a transcript
    pull, recovery got an empty DATA block, response was «нет
    данных». Search results alone are titles, not content."""
    from app.ceo_brain.responder import build_system_prompt

    sys_prompt = build_system_prompt()
    # The prompt must contain a chain-rule: «после search_* всегда
    # зови get_zoom_transcript/get_meeting». Check for the strongest
    # form: explicit "после ... search ... get_zoom_transcript" or
    # equivalent English wording.
    lower = sys_prompt.lower()
    assert "search" in lower
    assert "get_zoom_transcript" in lower or "get_meeting" in lower
    # The mandate phrasing must explicitly connect search →
    # transcript, not just mention them separately.
    assert ("после search" in lower) or ("after search" in lower), (
        "prompt must explicitly chain search → transcript_fetch"
    )


def test_responder_system_prompt_includes_search_strategy_rules():
    """FR-CB2-3.19 — system prompt must teach the model two
    operator-observed search strategies (2026-05-19):

      1. Search queries derive from the TOPIC of the question
         (e.g. «fundraising», «due diligence»), not just the
         counterparty name — picking only the name surfaces all
         meetings with that person and risks landing on the wrong
         one.
      2. A transcript shorter than 2000 chars is effectively empty
         (just participant list, no dialogue). On encountering one,
         the model must try the next candidate or re-search rather
         than giving up.
    """
    from app.ceo_brain.responder import build_system_prompt

    sys_prompt = build_system_prompt()
    # Topic-keyword rule.
    assert "тем" in sys_prompt.lower(), (
        "system prompt should instruct using topic keywords"
    )
    # Short-transcript-retry rule.
    assert "2000" in sys_prompt or "пуст" in sys_prompt.lower(), (
        "system prompt should mention short/empty transcript retry"
    )


def test_responder_tool_use_events_streamed():
    """FR-CB2-3.15 — tool_use events surface in placeholder."""
    from app.ceo_brain.responder import describe_tool_use_for_slack

    out = describe_tool_use_for_slack(
        tool_name="calendar.list_events",
        input_arg={"q": "EQT"},
    )
    assert "🔍" in out
    assert "Calendar" in out or "calendar" in out


# -- FR-CB2-3.16 — Local Slack tools ----------------------------------------


def test_slack_tools_schemas_shape():
    """FR-CB2-3.16 — local Slack tool schemas are exposed in the
    Anthropic-`tools` wire shape (name + description + input_schema
    with `type: "object"`)."""
    from app.ceo_brain.slack_tools import SLACK_TOOL_SCHEMAS

    assert isinstance(SLACK_TOOL_SCHEMAS, list)
    assert SLACK_TOOL_SCHEMAS, "must expose at least one Slack tool"
    names = {t["name"] for t in SLACK_TOOL_SCHEMAS}
    # Essentials the operator asked for: read history, post, search.
    assert "slack_search" in names
    assert "slack_post_message" in names
    assert "slack_get_channel_history" in names
    for t in SLACK_TOOL_SCHEMAS:
        assert isinstance(t.get("name"), str) and t["name"]
        assert isinstance(t.get("description"), str)
        schema = t.get("input_schema") or {}
        assert schema.get("type") == "object"
        assert isinstance(schema.get("properties"), dict)


def test_slack_tool_search_disabled_without_user_token():
    """FR-CB2-3.16 — `slack_search` needs a `xoxp-` user token.
    Without one, the executor returns a clear error JSON instead of
    crashing (so Claude can fall back to other tools)."""
    from app.ceo_brain.slack_tools import build_executors

    bot = MagicMock(name="bot-client")
    execs = build_executors(bot_client=bot, user_client=None)
    out = execs["slack_search"]({"query": "anything"})
    payload = json.loads(out)
    assert "error" in payload
    assert "search" in payload["error"].lower()


def test_slack_tool_post_message_uses_bot_client():
    """FR-CB2-3.16 — `slack_post_message` routes through bot client
    (bot token can post; user token isn't required)."""
    from app.ceo_brain.slack_tools import build_executors

    bot = MagicMock()
    bot.chat_postMessage.return_value = {
        "ok": True, "ts": "1.1", "channel": "C1",
    }
    execs = build_executors(bot_client=bot, user_client=None)
    out = execs["slack_post_message"](
        {"channel": "C1", "text": "hi", "thread_ts": "0.0"}
    )
    assert bot.chat_postMessage.called
    kwargs = bot.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["text"] == "hi"
    assert kwargs["thread_ts"] == "0.0"
    payload = json.loads(out)
    assert payload.get("ok") is True


def test_responder_includes_local_slack_tools():
    """FR-CB2-3.16 — when `slack_bot_client` is supplied,
    `build_anthropic_request` carries the local Slack tool schemas
    alongside any MCP servers."""
    from app.ceo_brain.responder import build_anthropic_request
    from app.ceo_brain.slack_tools import SLACK_TOOL_SCHEMAS

    req = build_anthropic_request(
        thread_history=[{"role": "user", "content": "hi"}],
        tools=list(SLACK_TOOL_SCHEMAS),
    )
    assert isinstance(req.get("tools"), list)
    tool_names = {t["name"] for t in req["tools"]}
    assert "slack_search" in tool_names
    assert "slack_post_message" in tool_names


def test_responder_local_tool_use_loop(session):
    """FR-CB2-3.16 — responder runs the multi-turn tool-use loop:
    1) first stream emits a `tool_use` block for `slack_search`,
    2) executor fires against `slack_bot_client`,
    3) responder feeds the result back and re-streams to `end_turn`.
    """
    from types import SimpleNamespace

    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    # Turn 1 (main, via create()) — tool_use for slack_search.
    tool_use_block = SimpleNamespace(
        type="tool_use",
        name="slack_search",
        input={"query": "EQT"},
        id="toolu_xyz",
    )
    final_msg_1 = SimpleNamespace(
        content=[tool_use_block],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    # Turn 2 (recovery loop iteration after local tool execution,
    # via create()) — plain text, end_turn.
    text_block = SimpleNamespace(type="text", text="Ничего не нашёл.")
    final_msg_2 = SimpleNamespace(
        content=[text_block],
        usage=SimpleNamespace(
            input_tokens=20, output_tokens=15,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    anthropic = MagicMock()
    # FR-CB2-3.22 — main turns now use messages.create (no MCP).
    anthropic.messages.create.side_effect = [final_msg_1, final_msg_2]
    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}

    # Bot client gets the slack_search call (we route it through
    # user_client when set).
    bot_client = MagicMock()
    user_client = MagicMock()
    user_client.search_messages.return_value = {
        "ok": True,
        "messages": {"matches": []},
    }

    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="C1", placeholder_ts="1.2", thread_history=[
            {"role": "user", "content": "Что про EQT?"}
        ],
        slack_bot_client=bot_client,
        slack_user_client=user_client,
    )

    # create() invoked twice (main turn + post-tool-execution turn).
    assert anthropic.messages.create.call_count == 2
    # The executor ran against the user client (search.messages).
    assert user_client.search_messages.called
    # Persisted run carries the tool_use trace.
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "done"
    assert any(
        (tu or {}).get("name") == "slack_search"
        for tu in (row.tool_uses or [])
    )


def test_final_render_retries_on_rate_limit():
    """FR-CB2-3.18 — when the final chat_update returns
    `ratelimited`, retry with Retry-After backoff up to 3 times."""
    from app.ceo_brain.responder import _final_render_to_slack

    slack = MagicMock()
    slack.chat_update.side_effect = [
        {"ok": False, "error": "ratelimited"},
        {"ok": True, "ts": "1.1"},
    ]
    sleeps: list = []
    _final_render_to_slack(
        slack=slack, channel="D1", placeholder_ts="1.0",
        thread_ts="0.9", text="hello", sleep=sleeps.append,
    )
    # Two attempts; second succeeded → no fallback message posted.
    assert slack.chat_update.call_count == 2
    assert not slack.chat_postMessage.called
    assert sleeps, "expected at least one backoff sleep"


def test_final_render_falls_back_to_new_message():
    """FR-CB2-3.18 — when all 3 chat_update retries fail with
    ratelimited, post a NEW message in the same thread via
    chat_postMessage instead of leaving the user with a stale
    placeholder."""
    from app.ceo_brain.responder import _final_render_to_slack

    slack = MagicMock()
    slack.chat_update.return_value = {"ok": False, "error": "ratelimited"}
    slack.chat_postMessage.return_value = {"ok": True, "ts": "9.9"}

    _final_render_to_slack(
        slack=slack, channel="D1", placeholder_ts="1.0",
        thread_ts="0.9", text="the real answer",
        sleep=lambda s: None,
    )
    assert slack.chat_update.call_count == 3
    assert slack.chat_postMessage.called
    kwargs = slack.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "D1"
    assert kwargs["text"] == "the real answer"
    assert kwargs["thread_ts"] == "0.9"


def test_responder_synthesis_recovery_when_text_empty(session):
    """FR-CB2-3.17 — when first stream ends with tool_uses but NO
    synthesizing text (observed Sonnet quirk), responder fires a
    follow-up stream asking for a summary, and uses THAT as the
    final answer."""
    from types import SimpleNamespace

    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    # Stream 1 — fires an MCP tool_use, then ends without any text.
    mcp_tool_use_block = SimpleNamespace(
        type="mcp_tool_use",
        name="search_meetings",
        server_name="n8n_calendar",
        input={"query": "today"},
        id="mcptoolu_1",
    )
    final_msg_1 = SimpleNamespace(
        content=[mcp_tool_use_block],  # no text block!
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    events_1 = [
        SimpleNamespace(
            type="content_block_start",
            content_block=mcp_tool_use_block,
        ),
    ]
    stream_1 = MagicMock()
    stream_1.__iter__ = lambda self: iter(events_1)
    stream_1.get_final_message.return_value = final_msg_1
    cm_1 = MagicMock()
    cm_1.__enter__.return_value = stream_1
    cm_1.__exit__.return_value = False

    # Stream 2 (recovery) — produces a clean text answer.
    text_block = SimpleNamespace(
        type="text", text="Сегодня одна встреча: Йохан в 14:00."
    )
    final_msg_2 = SimpleNamespace(
        content=[text_block],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=200, output_tokens=30,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    events_2 = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="text_delta",
                text="Сегодня одна встреча: Йохан в 14:00.",
            ),
        ),
    ]
    stream_2 = MagicMock()
    stream_2.__iter__ = lambda self: iter(events_2)
    stream_2.get_final_message.return_value = final_msg_2
    cm_2 = MagicMock()
    cm_2.__enter__.return_value = stream_2
    cm_2.__exit__.return_value = False

    anthropic = MagicMock()
    # FR-CB2-3.22 — main via create() (no MCP env), recovery via stream().
    anthropic.messages.create.return_value = final_msg_1
    anthropic.messages.stream.return_value = cm_2
    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}

    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="D1", placeholder_ts="1.2",
        thread_history=[
            {"role": "user", "content": "какие встречи сегодня?"}
        ],
    )

    # Main via create(), recovery via stream() — one each.
    assert anthropic.messages.create.call_count == 1
    assert anthropic.messages.stream.call_count == 1
    # The placeholder got a final chat_update with the recovery text.
    final_calls = [
        c for c in slack.chat_update.call_args_list
        if "Йохан" in (c.kwargs.get("text") or "")
    ]
    assert final_calls, "expected recovery text to reach Slack"
    # Persisted as done with the recovery text.
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "done"
    assert "Йохан" in (row.response_text or "")


def test_responder_synthesis_recovery_when_last_block_is_tool_use(session):
    """FR-CB2-3.17 — observed Sonnet quirk 2026-05-19: model writes
    interleaved planning text + tool_uses, but the LAST block of
    the turn is a tool_use (no synthesizing text after it). The
    `sdk_text` is non-empty (intermediate "I'll check..." planning)
    so the original empty-text recovery never fired and the operator
    saw mid-reasoning text + Sources with no actual answer.

    Fix: recovery must also fire when the last block of the turn
    is a tool_use / mcp_tool_use — i.e. no text block came after
    the last tool call.
    """
    from types import SimpleNamespace

    from app.ceo_brain.responder import run_responder
    from app.models import ClaudeResponderRun

    # Stream 1 — interleaved: intermediate text + tool_use, then
    # another text + tool_use. ENDS on a tool_use block, no final
    # synthesis text after it.
    text_block_1 = SimpleNamespace(
        type="text",
        text="Проверю транскрипт Fundraising daily параллельно.",
    )
    mcp_tool_use_1 = SimpleNamespace(
        type="mcp_tool_use",
        name="search_zoom_meetings",
        server_name="n8n_calendar",
        input={"query": "Jochen"},
        id="mcptoolu_1",
    )
    text_block_2 = SimpleNamespace(
        type="text",
        text="Теперь возьму транскрипт Fundraising daily.",
    )
    mcp_tool_use_2 = SimpleNamespace(
        type="mcp_tool_use",
        name="get_zoom_transcript",
        server_name="n8n_calendar",
        input={"id": "abc"},
        id="mcptoolu_2",
    )
    final_msg_1 = SimpleNamespace(
        content=[
            text_block_1, mcp_tool_use_1,
            text_block_2, mcp_tool_use_2,  # last block is tool_use
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=120, output_tokens=80,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    events_1 = [
        SimpleNamespace(
            type="content_block_start", content_block=text_block_1,
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="text_delta",
                text=text_block_1.text,
            ),
        ),
        SimpleNamespace(
            type="content_block_start", content_block=mcp_tool_use_1,
        ),
        SimpleNamespace(
            type="content_block_start", content_block=text_block_2,
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="text_delta",
                text=text_block_2.text,
            ),
        ),
        SimpleNamespace(
            type="content_block_start", content_block=mcp_tool_use_2,
        ),
    ]
    stream_1 = MagicMock()
    stream_1.__iter__ = lambda self: iter(events_1)
    stream_1.get_final_message.return_value = final_msg_1
    cm_1 = MagicMock()
    cm_1.__enter__.return_value = stream_1
    cm_1.__exit__.return_value = False

    # Recovery stream — produces the final synthesis.
    answer_block = SimpleNamespace(
        type="text",
        text=(
            "Сегодня на Fundraising daily обсудили: 1) statusы по EQT — "
            "Йохан должен напомнить Сергею про term sheet."
        ),
    )
    final_msg_2 = SimpleNamespace(
        content=[answer_block],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=200, output_tokens=60,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    events_2 = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(
                type="text_delta", text=answer_block.text,
            ),
        ),
    ]
    stream_2 = MagicMock()
    stream_2.__iter__ = lambda self: iter(events_2)
    stream_2.get_final_message.return_value = final_msg_2
    cm_2 = MagicMock()
    cm_2.__enter__.return_value = stream_2
    cm_2.__exit__.return_value = False

    anthropic = MagicMock()
    # FR-CB2-3.22 — main via create() (no MCP env), recovery via stream().
    anthropic.messages.create.return_value = final_msg_1
    anthropic.messages.stream.return_value = cm_2
    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}

    run_responder(
        slack=slack, anthropic_client=anthropic, db_session=session,
        channel="D1", placeholder_ts="1.2",
        thread_history=[
            {"role": "user",
             "content": "Что обсудили с Йоханом на встрече?"},
        ],
    )

    # Main via create(), recovery via stream() — one each.
    assert anthropic.messages.create.call_count == 1
    assert anthropic.messages.stream.call_count == 1
    row = session.query(ClaudeResponderRun).one()
    assert row.status == "done"
    # Final stored text is the recovery synthesis, not the
    # intermediate planning text.
    assert "term sheet" in (row.response_text or "")


def test_responder_synthesis_recovery_strips_tools_to_force_text(session):
    """FR-CB2-3.17 — recovery request MUST drop `mcp_servers`,
    `tools`, and `betas` so the model can't loop back into another
    round of tool calls instead of writing the answer. Without this,
    Sonnet re-uses the available MCP toolbox and the recovery
    returns yet more planning text (operator-observed 2026-05-19)."""
    from types import SimpleNamespace

    from app.ceo_brain.responder import run_responder

    mcp_tool_use_block = SimpleNamespace(
        type="mcp_tool_use",
        name="search_zoom_meetings",
        server_name="n8n_calendar",
        input={"query": "Jochen"},
        id="mcptoolu_1",
    )
    final_msg_1 = SimpleNamespace(
        content=[mcp_tool_use_block],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=80, output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    events_1 = [
        SimpleNamespace(
            type="content_block_start",
            content_block=mcp_tool_use_block,
        ),
    ]
    stream_1 = MagicMock()
    stream_1.__iter__ = lambda self: iter(events_1)
    stream_1.get_final_message.return_value = final_msg_1
    cm_1 = MagicMock()
    cm_1.__enter__.return_value = stream_1
    cm_1.__exit__.return_value = False

    text_block = SimpleNamespace(type="text", text="Готово.")
    final_msg_2 = SimpleNamespace(
        content=[text_block],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    stream_2 = MagicMock()
    stream_2.__iter__ = lambda self: iter([
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Готово."),
        ),
    ])
    stream_2.get_final_message.return_value = final_msg_2
    cm_2 = MagicMock()
    cm_2.__enter__.return_value = stream_2
    cm_2.__exit__.return_value = False

    anthropic = MagicMock()
    slack = MagicMock()
    slack.chat_update.return_value = {"ok": True}

    # Configure MCP_SERVERS env so the main turn routes through
    # the beta namespace, recovery falls back to plain
    # `messages.stream` (no beta needed since we flatten the
    # conversation into a single user message).
    import os
    prev = os.environ.get("MCP_SERVERS")
    os.environ["MCP_SERVERS"] = (
        '[{"name":"n8n_calendar","url":"https://example.invalid/x"}]'
    )
    # FR-CB2-3.22 — main turn now via beta.messages.create.
    anthropic.beta.messages.create.return_value = final_msg_1
    anthropic.messages.stream.return_value = cm_2
    try:
        run_responder(
            slack=slack, anthropic_client=anthropic, db_session=session,
            channel="D1", placeholder_ts="1.2",
            thread_history=[{"role": "user", "content": "вопрос"}],
        )
    finally:
        if prev is None:
            os.environ.pop("MCP_SERVERS", None)
        else:
            os.environ["MCP_SERVERS"] = prev

    # Main call went through beta.messages.CREATE (mcp_servers path).
    assert anthropic.beta.messages.create.call_count == 1
    main_kwargs = anthropic.beta.messages.create.call_args.kwargs
    assert "mcp_servers" in main_kwargs
    assert "betas" in main_kwargs

    # Recovery flattens the tool conversation into a single plain
    # user message and goes through plain `messages.stream` — NO MCP,
    # NO tools, NO beta header.
    assert anthropic.messages.stream.call_count == 1
    recovery_kwargs = anthropic.messages.stream.call_args.kwargs
    assert "mcp_servers" not in recovery_kwargs
    assert "tools" not in recovery_kwargs
    assert "betas" not in recovery_kwargs
    # The recovery user message contains the harvested DATA block.
    msgs = recovery_kwargs.get("messages") or []
    assert any(
        "<DATA>" in str(m.get("content") or "") for m in msgs
    ), "recovery prompt should embed harvested tool data"


# -- Category 4: MCP integration (FR-CB2-4.x) ------------------------------


def test_mcp_config_loaded_and_validated(monkeypatch):
    """FR-CB2-4.1 — MCP_SERVERS env var loaded + validated."""
    from app.ceo_brain.mcp import load_mcp_servers

    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name":"slack","url":"https://x","authorization_token":"t"}]',
    )
    servers = load_mcp_servers()
    assert servers[0]["name"] == "slack"
    assert servers[0]["url"] == "https://x"
    # Normalised to Anthropic Messages API shape (`type: "url"`)
    assert servers[0]["type"] == "url"
    assert servers[0]["authorization_token"] == "t"


def test_mcp_accepts_legacy_auth_field(monkeypatch):
    """FR-CB2-4.2 — back-compat: `auth` keyword in env JSON
    normalises to `authorization_token` for Anthropic API."""
    from app.ceo_brain.mcp import load_mcp_servers

    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name":"a","url":"u1","auth":"legacy-tok"}]',
    )
    out = load_mcp_servers()
    assert out[0]["authorization_token"] == "legacy-tok"


def test_mcp_oauth_token_resolution(monkeypatch):
    """FR-CB2-4.3 — OAuth token resolved per server."""
    from app.ceo_brain.mcp import resolve_oauth_token

    monkeypatch.setenv("MCP_SLACK_OAUTH_TOKEN", "tok-slack")
    assert resolve_oauth_token("slack") == "tok-slack"


def test_mcp_oauth_token_falls_back_to_json_auth(monkeypatch):
    """FR-CB2-4.3 — when no per-server env var, the
    `authorization_token` field from MCP_SERVERS JSON is used."""
    from app.ceo_brain.mcp import resolve_oauth_token

    monkeypatch.delenv("MCP_GMAIL_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv(
        "MCP_SERVERS",
        '[{"name":"gmail","url":"u","authorization_token":"json-tok"}]',
    )
    assert resolve_oauth_token("gmail") == "json-tok"


def test_mcp_partial_failure_degraded():
    """FR-CB2-4.4 — one MCP server unreachable: others still in."""
    from app.ceo_brain.mcp import filter_reachable_servers

    servers = [
        {"name": "slack", "url": "http://localhost:1", "type": "url"},
        {"name": "gmail", "url": "http://localhost:2", "type": "url"},
    ]
    reachable = filter_reachable_servers(
        servers, probe=lambda u: u.endswith("2"),
    )
    assert {s["name"] for s in reachable} == {"gmail"}


def test_mcp_calls_persisted(session):
    """FR-CB2-4.5 — tool_use events recorded in PG."""
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


def test_brain_disabled_no_threads(monkeypatch):
    """FR-CB2-5.1 — `CEO_BRAIN_ENABLED=false` keeps threads off."""
    from app.config import get_settings
    from app.ceo_brain.runner import CeoBrainRunner

    monkeypatch.setenv("CEO_BRAIN_ENABLED", "false")
    get_settings.cache_clear()
    r = CeoBrainRunner.from_env()
    r.start()
    assert r.archive_thread is None
    assert r.responder_thread is None


def test_brain_archive_only_mode(monkeypatch):
    """FR-CB2-5.2 — `CEO_BRAIN_ARCHIVE_ONLY=true` starts archive
    thread only."""
    from app.config import get_settings
    from app.ceo_brain.runner import CeoBrainRunner

    monkeypatch.setenv("CEO_BRAIN_ENABLED", "true")
    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_ONLY", "true")
    monkeypatch.setenv("CEO_BRAIN_ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    r = CeoBrainRunner.from_env()
    r.start()
    assert r.archive_thread is not None
    assert r.responder_thread is None
    r.stop()


def test_brain_responder_requires_anthropic_key(monkeypatch):
    """FR-CB2-5.3 — without `CEO_BRAIN_ANTHROPIC_API_KEY` the
    responder stays off."""
    from app.config import get_settings
    from app.ceo_brain.runner import CeoBrainRunner

    monkeypatch.setenv("CEO_BRAIN_ENABLED", "true")
    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_ONLY", "false")
    monkeypatch.setenv("CEO_BRAIN_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    r = CeoBrainRunner.from_env()
    r.start()
    assert r.responder_thread is None
    r.stop()


def test_brain_archive_dir_configurable(monkeypatch, tmp_path):
    """FR-CB2-5.4 — `CEO_BRAIN_ARCHIVE_DIR` env overrides default."""
    from app.config import get_settings
    from app.ceo_brain.config import get_archive_dir

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_DIR", str(tmp_path))
    get_settings.cache_clear()
    assert get_archive_dir() == tmp_path


def test_brain_uses_dedicated_tokens(monkeypatch):
    """FR-CB2-5.5 — CEO Brain tokens prefer dedicated env vars
    when set, so a separate Slack app can be wired up if the
    operator chooses."""
    from app.config import get_settings
    from app.ceo_brain.config import get_slack_tokens

    monkeypatch.setenv("CEO_BRAIN_SLACK_APP_TOKEN", "xapp-CB")
    monkeypatch.setenv("CEO_BRAIN_SLACK_BOT_TOKEN", "xoxb-CB")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-OLD")
    get_settings.cache_clear()
    app_t, bot_t = get_slack_tokens()
    assert app_t == "xapp-CB"
    assert bot_t == "xoxb-CB"


def test_brain_falls_back_to_workspace_tokens(monkeypatch):
    """FR-CB2-5.5b — operator-pinned 2026-05-18: «мне не надо
    новый app создавать, мне в текущем надо». When CEO Brain
    tokens are unset, fall back to the top-level
    `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` so the existing
    workspace bot can serve as the responder identity."""
    from app.config import get_settings
    from app.ceo_brain.config import get_slack_tokens

    monkeypatch.delenv("CEO_BRAIN_SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("CEO_BRAIN_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-WS")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-WS")
    get_settings.cache_clear()
    app_t, bot_t = get_slack_tokens()
    assert app_t == "xapp-WS"
    assert bot_t == "xoxb-WS"


def test_brain_archive_whitelist(monkeypatch):
    """FR-CB2-5.6 — `CEO_BRAIN_ARCHIVE_CHANNELS=C1,C2` whitelist."""
    from app.config import get_settings
    from app.ceo_brain.archive import should_archive_channel

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_CHANNELS", "C1,C2")
    get_settings.cache_clear()
    assert should_archive_channel("C1") is True
    assert should_archive_channel("C3") is False


def test_brain_archive_whitelist_empty_means_all(monkeypatch):
    """FR-CB2-5.6 — empty whitelist archives every channel."""
    from app.config import get_settings
    from app.ceo_brain.archive import should_archive_channel

    monkeypatch.delenv("CEO_BRAIN_ARCHIVE_CHANNELS", raising=False)
    get_settings.cache_clear()
    assert should_archive_channel("C-anywhere") is True


# -- Category 6: Ops (FR-CB2-6.x) — Sprint 5 -------------------------------


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
def test_brain_cli_backfill():
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


def test_brain_failed_run_does_not_block_archive(session, tmp_path, monkeypatch):
    """NFR-CB2-R.1 — failure in responder pipeline does NOT stop
    the archive pipeline; archive keeps writing."""
    from app.ceo_brain.dispatcher import handle_event

    monkeypatch.setenv("CEO_BRAIN_ARCHIVE_DIR", str(tmp_path))

    def boom(payload):
        raise RuntimeError("explode")

    payload = {
        "type": "app_mention", "channel": "C1", "user": "U1",
        "text": "<@UBOT> hi", "ts": "100.0", "event_id": "Ev-boom",
    }
    r = handle_event(
        session, payload, bot_user_id="UBOT", responder=boom,
    )
    assert r.archived is True
    assert r.responder_error is not None
    assert "explode" in r.responder_error


@_PENDING
def test_brain_reconnect_no_event_loss():
    """NFR-CB2-R.2 — Socket reconnect doesn't drop events."""
    pytest.skip("Behaviour test — inject-disconnect harness needed")


def test_responder_run_payload_scrubbed(session):
    """NFR-CB2-S.3 — `request_payload.mcp_servers[].auth` never
    written to DB."""
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


def test_brain_per_run_cost_cap(monkeypatch):
    """NFR-CB2-C.2 — `CEO_BRAIN_MAX_RUN_COST_USD` enforces cap."""
    from app.ceo_brain.responder import build_anthropic_request

    monkeypatch.setenv("CEO_BRAIN_MAX_RUN_COST_USD", "1.0")
    req = build_anthropic_request(thread_history=[])
    assert req["max_tokens"] <= 70_000


@_PENDING
def test_brain_jsonl_retention_rotate(tmp_path, monkeypatch):
    """NFR-CB2-A.1 — JSONL files older than retention pruned."""
    from datetime import timedelta

    from app.ceo_brain.retention import prune_old_jsonl_files

    monkeypatch.setenv("CEO_BRAIN_JSONL_RETENTION_DAYS", "365")
    old_day = (datetime.now(timezone.utc) - timedelta(days=400)).strftime(
        "%Y-%m-%d.jsonl"
    )
    old = tmp_path / "board" / old_day
    old.parent.mkdir(parents=True)
    old.write_text("x\n")
    recent = tmp_path / "board" / (
        datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
    )
    recent.write_text("y\n")
    prune_old_jsonl_files(archive_dir=tmp_path, days=365)
    assert not old.exists()
    assert recent.exists()
