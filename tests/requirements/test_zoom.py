"""FR-CR-05-116 — Zoom Cloud Recordings pipeline.

Mirror of the Fireflies pipeline tests at the integration
level. The shared helpers (`_split_audio_into_chunks`,
`_truncate`, etc.) are exercised by `test_fireflies.py`; here
we focus on the Zoom-specific pieces:

  - ZoomClient OAuth caches token and uses bearer auth.
  - ZoomClient.list_recordings normalises the REST payload
    into ZoomRecordingMeta with audio_url picking M4A first.
  - ZoomPipeline.process_one runs all 6 steps end-to-end on
    the new ZoomRecording table.
  - Tasks extracted from a Zoom recording carry
    source_kind=zoom (FR-CR-05-116 enum addition).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from tempfile import mkdtemp
from unittest.mock import patch

import pytest

from app.config import Settings
from app.models import Task, TaskSourceKind, ZoomRecording
from app.models.team import TeamMember
from app.zoom.client import ZoomClient, ZoomRecordingMeta
from app.zoom.pipeline import ZoomPipeline


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #


class _FakeRequestFunc:
    """Sequence-driven stub for ZoomClient._request_func.

    Each call returns the next dict from `responses` (or {} if
    exhausted). Records every call for assertion."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, url, headers, body, method="GET"):
        self.calls.append((url, headers, body, method))
        if self.responses:
            return self.responses.pop(0)
        return {}


class _FakeLLM:
    def __init__(self, *, summary_text="Подробный отчёт.", tasks=None):
        self.summary_text = summary_text
        self.tasks = tasks or []
        self.complete_text_calls = 0
        self.call_tool_calls = 0

    def complete_text(self, *, system_prompt, user_prompt, model=None,
                      temperature=0.2, reasoning_effort=None,
                      response_format=None):
        self.complete_text_calls += 1
        # FR-CR-05-129 — task extract / verifier now use JSON-
        # mode complete_text. Discriminate via system prompt.
        import json as _json
        if "SECOND-PASS verifier" in (system_prompt or ""):
            return _json.dumps({"tasks": []})
        if (
            "Extract action items" in (system_prompt or "")
            or "ACTIONABLE TASKS" in (system_prompt or "")
            or "extract ACTIONABLE TASKS" in (system_prompt or "")
        ):
            return _json.dumps({"tasks": list(self.tasks)})
        return self.summary_text

    def call_tool(self, *, system_prompt, user_prompt, tool_name,
                  tool_description, tool_parameters, model=None,
                  reasoning_effort=None):
        # Back-compat for any leftover call_tool sites.
        self.call_tool_calls += 1
        if "SECOND-PASS verifier" in (system_prompt or ""):
            return {"tasks": []}
        return {"tasks": list(self.tasks)}


class _FakeDocs:
    def __init__(self):
        self.export_calls = 0

    def export_summary(self, *, title, body, parent_folder_id=""):
        self.export_calls += 1
        return ("doc-zoom-1", "https://docs.google.com/document/d/doc-zoom-1/edit")


class _FakeSender:
    enabled = True  # FR-CR-05-118 — `post_initial_card` short-
    # circuits when `sender.enabled` is falsy, so the task-card
    # DMs would silently no-op without this flag.

    def __init__(self):
        self.sent = []

    def send_message(self, *, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return {"message_id": len(self.sent)}


def _settings_with_audio_dir():
    """Build a Settings object pointing audio_dir at a tmp dir
    (we never actually download or transcribe in these tests)."""
    audio_dir = mkdtemp()
    return Settings(
        OPENAI_API_KEY="sk-test",
        TELEGRAM_BOT_TOKEN="0:fake",
        ZOOM_ACCOUNT_ID="acc",
        ZOOM_CLIENT_ID="cid",
        ZOOM_CLIENT_SECRET="csecret",
        ZOOM_AUDIO_DIR=audio_dir,
    )


def _zoom_meta(zoom_id="zm-1"):
    return ZoomRecordingMeta(
        id=zoom_id,
        meeting_id="123456789",
        title="Weekly Sync",
        meeting_date=datetime(2026, 4, 30, 14, 30, tzinfo=timezone.utc),
        duration_seconds=1800,
        participants=["Andre", "Irina"],
        audio_url="https://cdn.zoom.us/audio.m4a",
        share_url="https://zoom.us/rec/share/abc",
        raw={},
    )


# --------------------------------------------------------------------------- #
# ZoomClient — OAuth + list_recordings
# --------------------------------------------------------------------------- #


def test_zoom_client_disabled_when_credentials_missing():
    """Empty creds ⇒ enabled=False, list_recordings returns []."""
    c = ZoomClient(account_id="", client_id="", client_secret="")
    assert c.enabled is False
    assert c.list_recordings() == []


def test_zoom_client_oauth_basic_auth_and_list_recordings():
    """FR-CR-05-116 — OAuth call uses Basic + grant_type=
    account_credentials. list_recordings normalises Zoom's
    `meetings[].recording_files` payload, prefers M4A audio."""
    fake = _FakeRequestFunc(
        responses=[
            # 1. OAuth token response
            {"access_token": "tok-1", "expires_in": 3600},
            # 2. /users/me/recordings response
            {
                "meetings": [
                    {
                        "uuid": "abc-123",
                        "id": 999888777,
                        "topic": "Quarterly Review",
                        "start_time": "2026-04-30T14:30:00Z",
                        "duration": 30,  # minutes
                        "recording_files": [
                            {
                                "file_type": "MP4",
                                "download_url": "https://x/video.mp4",
                            },
                            {
                                "file_type": "M4A",
                                "download_url": "https://x/audio.m4a",
                            },
                        ],
                        "share_url": "https://zoom.us/rec/share/q",
                    }
                ]
            },
        ]
    )
    c = ZoomClient(
        account_id="acc",
        client_id="cid",
        client_secret="csecret",
        request_func=fake,
    )
    metas = c.list_recordings(limit=5)
    assert len(metas) == 1
    m = metas[0]
    assert m.id == "abc-123"
    assert m.meeting_id == "999888777"
    assert m.title == "Quarterly Review"
    # M4A preferred over MP4.
    assert m.audio_url == "https://x/audio.m4a"
    assert m.duration_seconds == 30 * 60

    # OAuth call shape.
    oauth_call = fake.calls[0]
    assert oauth_call[0].endswith("/oauth/token")
    assert oauth_call[1]["Authorization"].startswith("Basic ")
    assert oauth_call[2]["grant_type"] == "account_credentials"
    assert oauth_call[2]["account_id"] == "acc"
    # Subsequent /recordings call uses Bearer.
    rec_call = fake.calls[1]
    assert rec_call[1]["Authorization"] == "Bearer tok-1"


def test_zoom_client_list_recordings_populates_host_email():
    """FR-CR-05-143 — `host_email` from the Zoom API payload is
    surfaced on `ZoomRecordingMeta` so the migrator can filter
    to Artem-only recordings."""
    fake = _FakeRequestFunc(
        responses=[
            {"access_token": "tok-1", "expires_in": 3600},
            {
                "meetings": [
                    {
                        "uuid": "abc-1",
                        "id": 1,
                        "topic": "Artem's call",
                        "host_email": "1@thehumanoid.ai",
                        "start_time": "2026-05-01T08:04:00Z",
                        "duration": 70,
                        "recording_files": [
                            {"file_type": "M4A",
                             "download_url": "https://x/a.m4a"},
                        ],
                    },
                    {
                        "uuid": "abc-2",
                        "id": 2,
                        "topic": "Viktor sync",
                        "host_email": "irina.shipilova@skl.vc",
                        "start_time": "2026-05-04T08:09:00Z",
                        "duration": 60,
                        "recording_files": [],
                    },
                ]
            },
        ]
    )
    c = ZoomClient(
        account_id="acc", client_id="cid", client_secret="csecret",
        request_func=fake,
    )
    metas = c.list_recordings(limit=10)
    assert len(metas) == 2
    by_uuid = {m.id: m for m in metas}
    assert by_uuid["abc-1"].host_email == "1@thehumanoid.ai"
    assert by_uuid["abc-2"].host_email == "irina.shipilova@skl.vc"


def test_zoom_client_list_recordings_keeps_host_match_no_participants_call():
    """FR-CR-05-143 — when `host_email == required_email`,
    the recording is kept WITHOUT an extra
    `/past_meetings/{uuid}/participants` call (cost saver)."""
    fake = _FakeRequestFunc(
        responses=[
            {"access_token": "tok-1", "expires_in": 3600},
            {
                "meetings": [
                    {"uuid": "host-match", "id": 1,
                     "topic": "Artem call",
                     "host_email": "1@thehumanoid.ai",
                     "recording_files": []},
                ],
            },
        ]
    )
    c = ZoomClient(
        account_id="acc", client_id="cid", client_secret="csecret",
        request_func=fake,
    )
    metas = c.list_recordings(
        limit=10, required_email="1@thehumanoid.ai",
    )
    assert [m.id for m in metas] == ["host-match"]
    # No extra `/past_meetings/.../participants` call was made.
    participant_calls = [
        c for c in fake.calls if "/past_meetings/" in c[0]
    ]
    assert participant_calls == []


def test_zoom_client_list_recordings_falls_back_to_participants_check():
    """FR-CR-05-143 — operator-pinned «мне надо проверять что
    там есть 1@thehumanoid.ai». When `host_email` is someone
    else, fetch `/past_meetings/{uuid}/participants` and keep
    the recording iff the required email appears there. So
    Artem's joined-someone-else's calls aren't dropped."""
    fake = _FakeRequestFunc(
        responses=[
            {"access_token": "tok-1", "expires_in": 3600},
            # listing
            {
                "meetings": [
                    # (a) host doesn't match, Artem IS in the
                    #     participant list → keep.
                    {"uuid": "viktor-call", "id": 1,
                     "topic": "Viktor <> Artem",
                     "host_email": "irina.shipilova@skl.vc",
                     "recording_files": []},
                    # (b) host doesn't match, Artem NOT in the
                    #     participant list → drop.
                    {"uuid": "elena-call", "id": 2,
                     "topic": "Elena solo",
                     "host_email": "elena.radionova@sokolov.ch",
                     "recording_files": []},
                    # (c) host matches → keep, no participants call.
                    {"uuid": "artem-call", "id": 3,
                     "topic": "Artem hosted",
                     "host_email": "1@thehumanoid.ai",
                     "recording_files": []},
                ],
            },
            # /past_meetings/viktor-call/participants
            {"participants": [
                {"user_email": "irina.shipilova@skl.vc"},
                {"user_email": "1@thehumanoid.ai"},  # Artem
            ]},
            # /past_meetings/elena-call/participants
            {"participants": [
                {"user_email": "elena.radionova@sokolov.ch"},
                {"user_email": "stranger@example.com"},
            ]},
        ]
    )
    c = ZoomClient(
        account_id="acc", client_id="cid", client_secret="csecret",
        request_func=fake,
    )
    metas = c.list_recordings(
        limit=10, required_email="1@thehumanoid.ai",
    )
    kept = sorted(m.id for m in metas)
    # viktor-call kept (Artem participant), artem-call kept
    # (host); elena-call dropped (Artem absent).
    assert kept == ["artem-call", "viktor-call"]
    # Exactly TWO participants calls were made (one per host
    # mismatch). The host-match (artem-call) skipped the call.
    participant_calls = [
        c for c in fake.calls if "/past_meetings/" in c[0]
    ]
    assert len(participant_calls) == 2


def test_zoom_client_list_recordings_empty_required_email_disables_filter():
    """Passing `required_email=None` (default) preserves legacy
    behaviour — accept every recording, no participants call."""
    fake = _FakeRequestFunc(
        responses=[
            {"access_token": "tok-1", "expires_in": 3600},
            {
                "meetings": [
                    {"uuid": "a", "id": 1, "topic": "X",
                     "host_email": "x@y.z", "recording_files": []},
                    {"uuid": "b", "id": 2, "topic": "Y",
                     "host_email": "p@q.r", "recording_files": []},
                ]
            },
        ]
    )
    c = ZoomClient(
        account_id="acc", client_id="cid", client_secret="csecret",
        request_func=fake,
    )
    assert len(c.list_recordings(limit=10)) == 2
    # No `/past_meetings/...` calls.
    assert all("/past_meetings/" not in c[0] for c in fake.calls)


def test_zoom_client_fetch_participant_emails_handles_failure():
    """FR-CR-05-143 — `_fetch_meeting_participant_emails`
    returns `set()` (empty) on any failure (auth, network,
    404). Listing skips the recording in that case (effectively
    a deny-by-default when the participants check itself fails).
    """
    fake = _FakeRequestFunc(
        responses=[
            {"access_token": "tok-1", "expires_in": 3600},
            # listing
            {
                "meetings": [
                    {"uuid": "weird-uuid==", "id": 1, "topic": "x",
                     "host_email": "stranger@example.com",
                     "recording_files": []},
                ],
            },
            # /past_meetings/... — empty payload (e.g. 404
            # turned into {}).
            {},
        ]
    )
    c = ZoomClient(
        account_id="acc", client_id="cid", client_secret="csecret",
        request_func=fake,
    )
    metas = c.list_recordings(
        limit=10, required_email="1@thehumanoid.ai",
    )
    assert metas == []  # participants empty → drop.


def test_zoom_client_token_cached_until_expiry():
    """Second list_recordings call within the TTL doesn't
    re-OAuth."""
    fake = _FakeRequestFunc(
        responses=[
            {"access_token": "tok-1", "expires_in": 3600},
            {"meetings": []},
            {"meetings": []},  # second call reuses token
        ]
    )
    c = ZoomClient(
        account_id="acc", client_id="cid", client_secret="csecret",
        request_func=fake,
    )
    c.list_recordings()
    c.list_recordings()
    # Only ONE oauth call (first /oauth/token in fake.calls).
    oauth_calls = [
        call for call in fake.calls if call[0].endswith("/oauth/token")
    ]
    assert len(oauth_calls) == 1


# --------------------------------------------------------------------------- #
# ZoomPipeline — full happy path
# --------------------------------------------------------------------------- #


def test_zoom_pipeline_runs_every_step_and_creates_zoom_source_tasks(
    patched_session_scope, SessionFactory, monkeypatch
):
    """End-to-end happy path: download → transcribe → detailed
    summary → Doc → short summary → tasks. All 6 step flags
    flip True; the produced Tasks carry source_kind=zoom."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        settings = _settings_with_audio_dir()
        # Simulate audio download via stubbed client.
        downloaded_paths: list[str] = []

        class _StubZoomClient:
            enabled = True

            def list_recordings(self, *, limit, **kw):
                return [_zoom_meta()]

            def download_audio(self, *, url, dest_path, max_bytes):
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(dest_path, "wb") as f:
                    f.write(b"FAKE-M4A")
                downloaded_paths.append(dest_path)
                return 8

        # Whisper stub.
        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes",
            lambda **kw: "Это тестовый транскрипт Zoom встречи.",
        )
        llm = _FakeLLM(
            tasks=[
                {
                    "title": "подготовить follow-up по Zoom",
                    "description": "Отправить заметки по итогам встречи.",
                    "owner": "777",
                    "priority": "high",
                }
            ]
        )
        sender = _FakeSender()
        pipeline = ZoomPipeline(
            settings=settings,
            client=_StubZoomClient(),
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=sender,
        )

        with SessionFactory() as s:
            s.add(
                TeamMember(
                    real_name="Admin", telegram_user_id=777,
                    telegram_username="admin", active=True,
                )
            )
            s.flush()
            report = pipeline.process_one(s, _zoom_meta())
            s.commit()

        assert report.tasks_created == 1
        # Step flags landed.
        with SessionFactory() as s:
            row = s.query(ZoomRecording).one()
            assert row.audio_downloaded is True
            assert row.transcribed is True
            assert row.detailed_summarised is True
            assert row.doc_exported is True
            assert row.tasks_extracted is True
            assert row.short_summary_sent is True
            assert row.google_doc_url == "https://docs.google.com/document/d/doc-zoom-1/edit"
            assert row.tasks_extracted_count == 1
            # FR-CR-05-127 — title becomes an HTML hyperlink to
            # the Google Doc; the old «📄 Подробный отчёт: <url>»
            # trailer line is gone.
            assert "<a href=" in (row.short_summary or "")
            assert (row.google_doc_url or "") in (row.short_summary or "")
            assert "📄 Подробный отчёт:" not in (row.short_summary or "")

            tasks = s.query(Task).all()
            assert len(tasks) == 1
            t = tasks[0]
            assert t.source_kind == TaskSourceKind.zoom
            assert t.source_permalink == "https://zoom.us/rec/share/abc"
            assert t.title.lower().startswith("подготовить follow-up")
            # FR-CR-05-118 — source_conversation_id MUST be the
            # zoom_id so the join `JOIN zoom_recordings z ON
            # z.zoom_id = t.source_conversation_id` finds the
            # originating meeting. Pre-fix this was NULL and
            # every join returned 0 rows.
            assert t.source_conversation_id == row.zoom_id
            assert t.source_message_ts == row.zoom_id
            # FR-CR-05-118 — Zoom-extracted tasks default to 18:00
            # deadline same as Fireflies (FR-CR-05-63).
            from datetime import time as _time
            assert t.due_time == _time(18, 0)

        # FR-CR-05-118 — Short summary DM (one) AND a per-task
        # DM card (one per extracted task) were sent. Pre-fix
        # only the short summary went out — operator never saw
        # individual task cards in TG.
        assert any(m["chat_id"] == 777 for m in sender.sent)
        # At least the summary + one task card → ≥2 messages
        # to the admin's DM.
        admin_dms = [m for m in sender.sent if m["chat_id"] == 777]
        assert len(admin_dms) >= 2, (
            "expected short summary + ≥1 task card DMs, "
            f"got {len(admin_dms)}"
        )
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_zoom_pipeline_idempotent_when_already_processed(
    patched_session_scope, SessionFactory, monkeypatch
):
    """A second `process_one` on the same Zoom UUID short-
    circuits: no new audio download, no new Whisper call,
    no new Tasks."""
    settings = _settings_with_audio_dir()
    monkeypatch.setattr(
        "app.services.transcription.transcribe_bytes",
        lambda **kw: "irrelevant",
    )

    download_count = {"n": 0}

    class _StubClient:
        enabled = True

        def list_recordings(self, *, limit, **kw):
            return [_zoom_meta("zm-rerun")]

        def download_audio(self, *, url, dest_path, max_bytes):
            download_count["n"] += 1
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(b"FAKE-M4A")
            return 8

    llm = _FakeLLM(tasks=[])
    pipeline = ZoomPipeline(
        settings=settings,
        client=_StubClient(),
        llm_backend=llm,
        docs_factory=lambda: _FakeDocs(),
        sender=None,
    )
    with SessionFactory() as s:
        pipeline.process_one(s, _zoom_meta("zm-rerun"))
        s.commit()
    assert download_count["n"] == 1

    # Re-run with a fresh-stubbed transcribe that would explode
    # if called.
    monkeypatch.setattr(
        "app.services.transcription.transcribe_bytes",
        lambda **kw: (_ for _ in ()).throw(AssertionError("re-transcribe attempted")),
    )
    with SessionFactory() as s:
        report = pipeline.process_one(s, _zoom_meta("zm-rerun"))
        s.commit()
    # Download wasn't repeated; no new transcribe call.
    assert download_count["n"] == 1
    # No new tasks.
    with SessionFactory() as s:
        assert s.query(Task).count() == 0


# --------------------------------------------------------------------------- #
# FR-CR-05-118 — listener-side periodic Zoom poll
# --------------------------------------------------------------------------- #


class _StubZoomClient:
    def __init__(self, metas):
        self._metas = metas
        self.calls = 0
        self.last_kwargs: dict = {}

    def list_recordings(self, *, limit, **kw):
        self.calls += 1
        self.last_kwargs = dict(kw)
        return list(self._metas)


class _StubZoomPipeline:
    def __init__(self, metas, *, raise_on=None):
        self._client = _StubZoomClient(metas)
        self.processed: list[str] = []
        self._raise_on = raise_on or set()

    def process_one(self, session, m):
        if m.id in self._raise_on:
            raise RuntimeError("boom")
        self.processed.append(m.id)
        from types import SimpleNamespace

        return SimpleNamespace(
            skipped_reason=None,
            tasks_created=2,
            errors=[],
        )


def _zoom_listener_for_poll():
    """Minimal `TelegramListener` instance suitable for invoking
    `_maybe_poll_zoom` in isolation. The ingest dependency is
    stubbed because it isn't exercised by the poll path."""
    from app.config import Settings
    from app.orchestrator import Orchestrator
    from app.telegram_ingest.service import TelegramIngestService
    from app.telegram_bot.listener import TelegramListener

    class _NoopClassifier:
        def classify(self, *a, **k):  # noqa: D401
            from app.intent.types import IntentClassification, IntentType

            return IntentClassification(intent=IntentType.unknown, confidence=0.0)

    ingest = TelegramIngestService(
        classifier=_NoopClassifier(),
        orchestrator=Orchestrator(Settings()),
    )
    return TelegramListener(token="", ingest=ingest)


def _zoom_poll_meta(zoom_id="zm-poll-1"):
    from datetime import datetime, timezone

    from app.zoom.client import ZoomRecordingMeta

    return ZoomRecordingMeta(
        id=zoom_id,
        meeting_id=None,
        title="Sync " + zoom_id,
        meeting_date=datetime.now(timezone.utc),
        duration_seconds=600,
        participants=[],
        audio_url="https://zoom.us/x.m4a",
        share_url="https://zoom.us/share/x",
    )


def test_verifier_pass_adds_missed_tasks_in_zoom_pipeline(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-121 — operator regression: first-pass extract
    misses 30-50% of tasks on long meetings. The verifier pass
    re-reads the transcript + already-extracted tasks and adds
    whatever was missed. End-to-end: pipeline starts with 1
    task from the first pass, verifier finds 1 more → DB holds
    2 tasks."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        settings = _settings_with_audio_dir()

        class _StubZoomClient:
            enabled = True

            def list_recordings(self, *, limit, **kw):
                return [_zoom_meta()]

            def download_audio(self, *, url, dest_path, max_bytes):
                with open(dest_path, "wb") as f:
                    f.write(b"x" * 1024)
                return 1024

        monkeypatch.setattr(
            "app.fireflies.pipeline._sniff_audio_extension",
            lambda *a, **kw: "m4a",
        )
        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes",
            lambda **kw: "Артем: Алина подготовит письмо. Дима пришлёт фоллоу-ап.",
        )

        class _VerifyingFakeLLM:
            def __init__(self):
                self.summary_text = "Подробный отчёт."
                self.complete_text_calls = 0
                self.call_tool_calls = 0

            def complete_text(self, *, system_prompt, user_prompt,
                              model=None, temperature=0.2,
                              reasoning_effort=None,
                              response_format=None):
                self.complete_text_calls += 1
                import json as _json
                # FR-CR-05-129 — task extract / verifier on JSON-mode.
                if "SECOND-PASS verifier" in (system_prompt or ""):
                    return _json.dumps({"tasks": [{
                        "title": "Прислать фоллоу-ап",
                        "description": "Дима - прислать фоллоу-ап.",
                        "owner": None, "priority": "medium",
                    }]})
                if (
                    "Extract action items" in (system_prompt or "")
                    or "ACTIONABLE TASKS" in (system_prompt or "")
                    or "extract ACTIONABLE TASKS" in (system_prompt or "")
                ):
                    return _json.dumps({"tasks": [{
                        "title": "Подготовить письмо",
                        "description": "Алина - подготовит письмо.",
                        "owner": None, "priority": "medium",
                    }]})
                return self.summary_text

            def call_tool(self, *, system_prompt, user_prompt, tool_name,
                          tool_description, tool_parameters, model=None,
                          reasoning_effort=None):
                self.call_tool_calls += 1
                if "SECOND-PASS verifier" in (system_prompt or ""):
                    return {"tasks": [{
                        "title": "Прислать фоллоу-ап",
                        "description": "Дима - прислать фоллоу-ап.",
                        "owner": None, "priority": "medium",
                    }]}
                return {"tasks": [{
                    "title": "Подготовить письмо",
                    "description": "Алина - подготовит письмо.",
                    "owner": None, "priority": "medium",
                }]}

        sender = _FakeSender()
        from app.zoom.pipeline import ZoomPipeline

        pipeline = ZoomPipeline(
            settings=settings,
            client=_StubZoomClient(),
            llm_backend=_VerifyingFakeLLM(),
            docs_factory=lambda: _FakeDocs(),
            sender=sender,
        )
        with SessionFactory() as s:
            from app.models import TeamMember
            s.add(
                TeamMember(
                    real_name="Admin", telegram_user_id=777,
                    telegram_username="admin", active=True,
                )
            )
            s.flush()
            report = pipeline.process_one(s, _zoom_meta())
            s.commit()

        assert report.tasks_created == 2
        with SessionFactory() as s:
            tasks = s.query(Task).filter(
                Task.source_kind == TaskSourceKind.zoom
            ).all()
            assert len(tasks) == 2
            titles = sorted(t.title for t in tasks)
            assert "Подготовить письмо" in titles
            assert "Прислать фоллоу-ап" in titles
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_zoom_pipeline_emits_trace_lines_for_every_step(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-122 — every pipeline step is bracketed by a
    `zoom_step_started` / `zoom_step_done` log line so the
    operator can walk through a single recording's run by
    `grep zoom_step_(started|done|failed)`. Each line carries
    the step label + zoom_id; `_done` lines also carry
    `duration_ms`. This test pins the trace shape end-to-end."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    captured: list[dict] = []

    # FR-CR-05-122 — the pipeline uses structlog (not stdlib
    # logging), so caplog won't see the events. Patch the
    # logger's `info` and `warning` methods to record events
    # for assertion.
    from app.fireflies import pipeline as _ff
    from app.zoom import pipeline as _zoom_mod

    def _capture(level):
        def _emit(event, **fields):
            captured.append({"level": level, "event": event, **fields})
        return _emit

    monkeypatch.setattr(_ff.log, "info", _capture("info"))
    monkeypatch.setattr(_ff.log, "warning", _capture("warning"))
    monkeypatch.setattr(_zoom_mod.log, "info", _capture("info"))
    monkeypatch.setattr(_zoom_mod.log, "warning", _capture("warning"))
    try:
        settings = _settings_with_audio_dir()

        class _StubZoomClient:
            enabled = True

            def list_recordings(self, *, limit, **kw):
                return [_zoom_meta()]

            def download_audio(self, *, url, dest_path, max_bytes):
                with open(dest_path, "wb") as f:
                    f.write(b"x" * 1024)
                return 1024

        monkeypatch.setattr(
            "app.fireflies.pipeline._sniff_audio_extension",
            lambda *a, **kw: "m4a",
        )
        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes",
            lambda **kw: "Артем сказал что Алина подготовит письмо.",
        )
        llm = _FakeLLM(
            summary_text="Подробный отчёт.",
            tasks=[{
                "title": "Подготовить письмо",
                "description": "Алина - подготовит письмо.",
                "owner": None, "priority": "medium",
            }],
        )
        sender = _FakeSender()
        from app.zoom.pipeline import ZoomPipeline

        pipeline = ZoomPipeline(
            settings=settings,
            client=_StubZoomClient(),
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=sender,
        )

        with SessionFactory() as s:
            from app.models import TeamMember
            s.add(
                TeamMember(
                    real_name="Admin", telegram_user_id=777,
                    telegram_username="admin", active=True,
                )
            )
            s.flush()
            pipeline.process_one(s, _zoom_meta())
            s.commit()

        events = [c["event"] for c in captured]
        assert "zoom_step_started" in events, (
            "no started lines: %s" % events[:20]
        )
        assert "zoom_step_done" in events, (
            "no done lines: %s" % events[:20]
        )
        # Per-step coverage: every step name appears in BOTH
        # started and done lines.
        started_steps = {
            c["step"] for c in captured if c["event"] == "zoom_step_started"
        }
        done_steps = {
            c["step"] for c in captured if c["event"] == "zoom_step_done"
        }
        for step in (
            "download", "transcribe", "detailed_summary",
            "extract_tasks", "verify_tasks",
            "doc_export", "short_summary", "post_task_cards",
        ):
            assert step in started_steps, f"no started for {step}"
            assert step in done_steps, f"no done for {step}"
        # `_done` carries duration_ms (int).
        for c in captured:
            if c["event"] == "zoom_step_done":
                assert isinstance(c.get("duration_ms"), int), c
                assert c.get("zoom_id") == "zm-1"
        # No failed lines on the happy path.
        assert not any(c["event"] == "zoom_step_failed" for c in captured)
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_zoom_short_summary_dm_arrives_before_per_task_cards(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-120 follow-up — operator pinned: meeting overview
    DM (Суть + To-Do in one message) MUST arrive before the
    per-task DM cards. Pre-fix the cards came first because
    `_step_extract_tasks` posted them inline; now `_step_post_
    task_cards` runs as the final pipeline step, after
    `_step_send_short_summary`."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        settings = _settings_with_audio_dir()

        class _StubZoomClient:
            enabled = True

            def list_recordings(self, *, limit, **kw):
                return [_zoom_meta()]

            def download_audio(self, *, url, dest_path, max_bytes):
                with open(dest_path, "wb") as f:
                    f.write(b"x" * 1024)
                return 1024

        monkeypatch.setattr(
            "app.fireflies.pipeline._sniff_audio_extension",
            lambda *a, **kw: "m4a",
        )
        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes",
            lambda **kw: "Артем сказал что Алина подготовит письмо.",
        )

        # FR-CR-05-134 — give tasks explicit owners that resolve
        # to a real team_member; previously relied on the now-
        # removed admin_fallback_null_owner. Zoom uses
        # `as_known_employees(prefer_telegram=True)` so the
        # primary id is the telegram_user_id stringified ("777"),
        # not the slack_user_id. Test purpose (summary BEFORE
        # cards ordering) is unchanged.
        llm = _FakeLLM(
            summary_text="Краткое описание.",
            tasks=[
                {"title": "Подготовить письмо",
                 "description": "Алина - подготовит письмо инвесторам.",
                 "owner": "777", "priority": "medium"},
                {"title": "Скоординировать тайминг",
                 "description": "Ирина - скоординировать тайминг.",
                 "owner": "777", "priority": "medium"},
            ],
        )
        sender = _FakeSender()
        from app.zoom.pipeline import ZoomPipeline

        pipeline = ZoomPipeline(
            settings=settings,
            client=_StubZoomClient(),
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=sender,
        )

        with SessionFactory() as s:
            from app.models import TeamMember
            s.add(
                TeamMember(
                    real_name="Admin", telegram_user_id=777,
                    telegram_username="admin",
                    active=True,
                )
            )
            s.flush()
            pipeline.process_one(s, _zoom_meta())
            s.commit()

        # Find the index of the short-summary DM (text contains
        # «Суть:» / «To-Do:») vs per-task DM cards (have 👤
        # owner-line marker per FR-CR-05-118 test).
        admin_dms = [m for m in sender.sent if m["chat_id"] == 777]
        assert admin_dms, "no admin DMs sent"

        first_summary_idx = next(
            (i for i, m in enumerate(admin_dms)
             if "Суть" in m["text"] or "To-Do" in m["text"]),
            None,
        )
        first_card_idx = next(
            (i for i, m in enumerate(admin_dms) if "👤" in m["text"]),
            None,
        )
        assert first_summary_idx is not None, "no short-summary DM"
        assert first_card_idx is not None, "no per-task cards"
        # FR-CR-05-120 follow-up: summary lands BEFORE the first
        # per-task card.
        assert first_summary_idx < first_card_idx, (
            f"summary at {first_summary_idx}, first card at "
            f"{first_card_idx}; expected summary < cards"
        )
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_zoom_listener_poll_off_by_default(patched_session_scope, SessionFactory):
    """`_maybe_poll_zoom` is a no-op until `wire_zoom(enabled=True)`
    is called. Mirror of `_maybe_poll_fireflies` gating."""
    listener = _zoom_listener_for_poll()
    pipe = _StubZoomPipeline(metas=[_zoom_poll_meta("zm-A")])
    # Wire but DISABLED.
    listener.wire_zoom(
        pipeline=pipe, enabled=False,
        poll_interval_seconds=60, poll_batch_size=10,
    )
    listener._maybe_poll_zoom()
    assert pipe._client.calls == 0
    assert pipe.processed == []


def test_zoom_listener_poll_pulls_when_enabled(
    patched_session_scope, SessionFactory
):
    listener = _zoom_listener_for_poll()
    pipe = _StubZoomPipeline(metas=[_zoom_poll_meta("zm-B")])
    listener.wire_zoom(
        pipeline=pipe, enabled=True,
        poll_interval_seconds=60, poll_batch_size=10,
    )
    # Bypass the «from-now» cutoff so the test meta isn't dropped
    # as ancient relative to listener startup.
    from datetime import datetime as _dt, timezone as _tz

    listener._zoom_started_at = _dt(1970, 1, 1, tzinfo=_tz.utc)
    listener._maybe_poll_zoom()
    assert pipe._client.calls == 1
    assert pipe.processed == ["zm-B"]


def test_zoom_listener_poll_throttled_within_interval(
    patched_session_scope, SessionFactory
):
    """Repeat ticks inside the throttle window must NOT re-pull
    the API. Same protection the Fireflies path has."""
    listener = _zoom_listener_for_poll()
    pipe = _StubZoomPipeline(metas=[_zoom_poll_meta("zm-C")])
    listener.wire_zoom(
        pipeline=pipe, enabled=True,
        poll_interval_seconds=60, poll_batch_size=10,
    )
    from datetime import datetime as _dt, timezone as _tz

    listener._zoom_started_at = _dt(1970, 1, 1, tzinfo=_tz.utc)
    listener._maybe_poll_zoom()
    listener._maybe_poll_zoom()  # immediate second call
    listener._maybe_poll_zoom()
    assert pipe._client.calls == 1


def test_zoom_listener_poll_swallows_pipeline_errors(
    patched_session_scope, SessionFactory
):
    """If `process_one` blows up on one recording the listener
    keeps going for the others — same resilience contract as
    the Fireflies path."""
    listener = _zoom_listener_for_poll()
    pipe = _StubZoomPipeline(
        metas=[_zoom_poll_meta("zm-ok-1"), _zoom_poll_meta("zm-bad"),
               _zoom_poll_meta("zm-ok-2")],
        raise_on={"zm-bad"},
    )
    listener.wire_zoom(
        pipeline=pipe, enabled=True,
        poll_interval_seconds=60, poll_batch_size=10,
    )
    from datetime import datetime as _dt, timezone as _tz

    listener._zoom_started_at = _dt(1970, 1, 1, tzinfo=_tz.utc)
    listener._maybe_poll_zoom()
    assert pipe.processed == ["zm-ok-1", "zm-ok-2"]


def test_zoom_listener_poll_skips_recordings_before_startup(
    patched_session_scope, SessionFactory
):
    """`_zoom_started_at` cutoff drops recordings finished before
    listener startup so a fresh deploy doesn't backfill stale
    meetings — symmetric with FR-CR-05-51."""
    from datetime import datetime as _dt, timezone as _tz

    listener = _zoom_listener_for_poll()
    old = _zoom_poll_meta("zm-old")
    old.meeting_date = _dt(2000, 1, 1, tzinfo=_tz.utc)
    fresh = _zoom_poll_meta("zm-fresh")
    pipe = _StubZoomPipeline(metas=[old, fresh])
    listener.wire_zoom(
        pipeline=pipe, enabled=True,
        poll_interval_seconds=60, poll_batch_size=10,
    )
    # Set the cutoff in the past so the fresh meeting (date =
    # «now» at construction time) is treated as new.
    listener._zoom_started_at = _dt(2020, 1, 1, tzinfo=_tz.utc)
    fresh.meeting_date = _dt(2030, 1, 1, tzinfo=_tz.utc)
    listener._maybe_poll_zoom()
    assert pipe.processed == ["zm-fresh"]


def test_settings_zoom_polling_defaults():
    """Settings ship with the operator-friendly defaults: realtime
    OFF (must be opted in), 60s interval, 10-item batch. The
    Fireflies counterpart now also defaults to 60s for parity."""
    from app.config import Settings

    s = Settings()
    assert s.zoom_realtime_enabled is False
    assert s.zoom_poll_interval_seconds == 60
    assert s.zoom_poll_batch_size == 10
    assert s.fireflies_poll_interval_seconds == 60
