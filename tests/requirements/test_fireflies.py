"""FR-CR-05-39 — Fireflies meeting-recording pipeline.

Covers:
- API client: `list_transcripts` parses GraphQL response into
  `FirefliesTranscript` rows; participants / dates / unix-millis
  normalised; empty / unauthenticated responses degrade.
- Pipeline: each step writes its artefact + flips its progress
  flag; idempotent re-run on a completed recording is a no-op;
  task extraction lands `due_date=today` and
  `source_kind=fireflies`; admin-fallback fires when the LLM
  returns a hallucinated owner.
- Listener: `_maybe_poll_fireflies` is off by default,
  throttled, and swallows pipeline errors.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone

import pytest

from app.config import Settings
from app.fireflies.client import FirefliesClient, FirefliesTranscript
from app.fireflies.pipeline import (
    FirefliesPipeline,
    _looks_like_auto_stamp_title,
    _truncate,
)
from app.models import MeetingRecording, Task, TaskSourceKind, TeamMember


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


def test_client_disabled_when_token_empty():
    c = FirefliesClient(token="")
    assert c.enabled is False
    assert c.list_transcripts(limit=5) == []


def test_client_parses_graphql_transcripts_payload():
    """List transcripts call: payload → list of FirefliesTranscript."""
    captured: dict = {}

    def fake_request(url, headers, body):
        captured["url"] = url
        captured["headers"] = dict(headers)
        captured["body"] = dict(body)
        return {
            "data": {
                "transcripts": [
                    {
                        "id": "trans_1",
                        "title": "Weekly sync",
                        "date": 1730000000000,  # unix millis
                        "duration": 1800,
                        "audio_url": "https://files.fireflies.ai/abc.mp3",
                        "transcript_url": "https://app.fireflies.ai/view/abc",
                        "meeting_attendees": [
                            {"displayName": "Andre", "email": "andre@x.com"},
                            {"displayName": "Petya", "email": "petya@x.com"},
                        ],
                    }
                ]
            }
        }

    c = FirefliesClient(token="abc", request_func=fake_request)
    out = c.list_transcripts(limit=10)
    assert len(out) == 1
    t = out[0]
    assert t.id == "trans_1"
    assert t.title == "Weekly sync"
    assert t.duration_seconds == 1800
    assert t.audio_url == "https://files.fireflies.ai/abc.mp3"
    assert "Andre <andre@x.com>" in t.participants
    assert isinstance(t.meeting_date, datetime)
    # Bearer header is present.
    assert captured["headers"]["Authorization"] == "Bearer abc"


def test_client_handles_empty_response():
    def fake_request(url, headers, body):
        return {}

    c = FirefliesClient(token="x", request_func=fake_request)
    assert c.list_transcripts(limit=5) == []


# --------------------------------------------------------------------------- #
# Pipeline — fakes
# --------------------------------------------------------------------------- #


class _FakeLLM:
    """Stub backend covering the two methods the pipeline uses:
    `complete_text` for summaries, `call_tool` for task
    extraction."""

    def __init__(
        self,
        *,
        detailed: str = "Подробное саммари встречи (RU)",
        short: str = "🎙 Test\n📊 Кратко: ничего особенного",
        tasks: list[dict] | None = None,
    ) -> None:
        self.detailed = detailed
        self.short = short
        self.tasks = tasks or []
        self.complete_calls: list[str] = []
        self.tool_calls: list[str] = []

    def complete_text(self, *, system_prompt, user_prompt, model=None, temperature=0.2):
        self.complete_calls.append(system_prompt[:40])
        # Use first 30 chars of system prompt as discriminator.
        if "DETAILED" in system_prompt or "ДЕТАЛЬНЫЙ" in system_prompt or "DETAILED" in system_prompt.upper():
            return self.detailed
        return self.short

    def call_tool(self, *, system_prompt, tool_name, **kw):
        self.tool_calls.append(tool_name)
        return {"tasks": list(self.tasks)}


class _FakeFirefliesClient:
    """Stub `FirefliesClient` that returns canned transcripts and
    fakes the audio download by writing a sentinel byte string."""

    _UNSET = object()

    def __init__(
        self,
        *,
        transcripts=None,
        audio_bytes=b"FAKE-MP3",
        download_returns=_UNSET,  # FR-CR-05-115 — explicit None
                                  # = simulate cap-exceeded
        graphql_transcript_text="",
    ):
        self.enabled = True
        self._transcripts = transcripts or []
        self._audio_bytes = audio_bytes
        self._download_returns_explicit = (
            download_returns is not self._UNSET
        )
        self._download_returns = (
            None
            if download_returns is self._UNSET
            else download_returns
        )
        self._graphql_transcript_text = graphql_transcript_text
        self.download_calls = 0
        self.transcript_text_calls: list[str] = []

    def list_transcripts(self, *, limit, skip=0):
        return list(self._transcripts[:limit])

    def download_audio(self, *, url, dest_path, max_bytes=25 * 1024 * 1024):
        self.download_calls += 1
        # `download_returns=None` simulates cap-exceeded /
        # network failure when the test passes it explicitly.
        # Otherwise default (no explicit value) writes the
        # canned bytes and returns their length.
        if self._download_returns_explicit is False:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(self._audio_bytes)
            return len(self._audio_bytes)
        if self._download_returns is None:
            return None
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(self._audio_bytes)
        return self._download_returns

    def fetch_transcript_text(self, fireflies_id):
        """FR-CR-05-115 — GraphQL fallback used when audio
        is too big for Whisper."""
        self.transcript_text_calls.append(fireflies_id)
        return self._graphql_transcript_text


class _FakeDocs:
    def export_summary(self, *, title, body, parent_folder_id=""):
        return ("doc-id-123", "https://docs.google.com/document/d/doc-id-123/edit")


class _FakeSender:
    def __init__(self):
        self.enabled = True
        self.sent = []

    def send_message(self, *, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": 1}


def _settings_with_audio_dir() -> Settings:
    tmp = tempfile.mkdtemp(prefix="fireflies-test-")
    # Settings fields are aliased to their UPPER_SNAKE env-var names,
    # so kwargs must use the alias.
    return Settings(
        FIREFLIES_AUDIO_DIR=tmp,
        OPENAI_API_KEY="sk-test",
    )


def _fake_transcript(id_="trans-1", title="Weekly sync") -> FirefliesTranscript:
    return FirefliesTranscript(
        id=id_,
        title=title,
        meeting_date=datetime.now(timezone.utc),
        duration_seconds=1800,
        participants=["Andre <andre@x.com>"],
        audio_url="https://files.fireflies.ai/x.mp3",
        share_url="https://app.fireflies.ai/view/x",
        raw={},
    )


# --------------------------------------------------------------------------- #
# Pipeline — happy path
# --------------------------------------------------------------------------- #


def test_pipeline_process_one_runs_every_step(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Happy path: every step lands its artefact + flips its
    flag; the row's `processed_at` is set; tasks are created
    with `source_kind=fireflies` and `due_date=today`."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    try:
        # Bypass Whisper (no real OpenAI key) — replace
        # transcribe_bytes with a stub that returns canned text.
        def fake_transcribe(*, audio_bytes, mimetype, filename, openai_api_key, model="whisper-1"):
            return "Это тестовый транскрипт встречи."

        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes", fake_transcribe
        )

        settings = _settings_with_audio_dir()
        client = _FakeFirefliesClient(transcripts=[_fake_transcript()])
        llm = _FakeLLM(
            tasks=[
                {
                    "title": "написать письмо клиенту",
                    "description": "По итогам встречи нужно отправить.",
                    "owner": "777",
                    "priority": "high",
                }
            ]
        )
        sender = _FakeSender()
        pipeline = FirefliesPipeline(
            settings=settings,
            client=client,
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=sender,
        )

        with SessionFactory() as s:
            s.add(
                TeamMember(
                    real_name="Admin",
                    telegram_user_id=777,
                    telegram_username="admin",
                    active=True,
                )
            )
            s.flush()
            t = client.list_transcripts(limit=5)[0]
            report = pipeline.process_one(s, t)
            s.commit()

        assert report.tasks_created == 1
        assert report.google_doc_url == "https://docs.google.com/document/d/doc-id-123/edit"
        assert report.short_summary_recipients == 1
        assert report.transcript_chars > 0
        assert report.detailed_chars > 0
        assert report.short_chars > 0

        # Persistence — meeting_recordings row + Task row.
        with SessionFactory() as s:
            row = (
                s.query(MeetingRecording)
                .filter(MeetingRecording.fireflies_id == "trans-1")
                .first()
            )
            assert row is not None
            assert row.audio_downloaded is True
            assert row.transcribed is True
            assert row.detailed_summarised is True
            assert row.doc_exported is True
            assert row.short_summary_sent is True
            assert row.tasks_extracted is True
            assert row.processed_at is not None
            assert row.audio_path and os.path.exists(row.audio_path)

            tasks = s.query(Task).filter(
                Task.source_kind == TaskSourceKind.fireflies
            ).all()
            assert len(tasks) == 1
            assert tasks[0].due_date == date.today()
            assert tasks[0].owner_user_id == "777"
            assert tasks[0].source_conversation_id == "trans-1"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_pipeline_chunks_oversize_audio_and_concatenates_transcripts(
    patched_session_scope, SessionFactory, monkeypatch, tmp_path
):
    """FR-CR-05-115 — operator: «значит мне надо резать файл по
    24 мб, отдельно их прогонять в whisper, а потом склеивать,
    никаких фолбеков в транскрипт FF». When the downloaded
    audio exceeds 24 MB, pipeline splits via ffmpeg into
    ≤24 MB chunks, transcribes each, joins. No Fireflies-side
    transcript fallback."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        settings = _settings_with_audio_dir()
        # Simulate a 30 MB audio file.
        big_bytes = b"X" * (30 * 1024 * 1024)
        client = _FakeFirefliesClient(
            transcripts=[_fake_transcript("trans-big")],
            audio_bytes=big_bytes,
            download_returns=len(big_bytes),
        )
        # Stub out the chunker so we don't actually shell out
        # to ffmpeg. Two fake chunks.
        chunk_paths: list[str] = []

        def fake_split(path, *, max_bytes):
            for i in range(2):
                cp = f"{path}.chunk{i:02d}.mp3"
                with open(cp, "wb") as f:
                    f.write(b"chunk-" + str(i).encode())
                chunk_paths.append(cp)
            return list(chunk_paths)

        monkeypatch.setattr(
            "app.fireflies.pipeline._split_audio_into_chunks", fake_split
        )

        # Whisper stub: returns chunk-specific text so we can
        # assert concatenation order.
        call_log: list[str] = []

        def fake_transcribe(**kw):
            call_log.append(kw["filename"])
            if "chunk00" in kw["filename"]:
                return "часть 1: говорили о Beta"
            if "chunk01" in kw["filename"]:
                return "часть 2: договорились на четверг"
            return "(unexpected chunk)"

        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes", fake_transcribe
        )
        llm = _FakeLLM(tasks=[])
        pipeline = FirefliesPipeline(
            settings=settings,
            client=client,
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=_FakeSender(),
        )
        with SessionFactory() as s:
            pipeline.process_one(s, _fake_transcript("trans-big"))
            s.commit()

        with SessionFactory() as s:
            from app.models import MeetingRecording

            row = s.query(MeetingRecording).one()
            assert row.audio_downloaded is True
            assert row.transcribed is True
            # Both chunks landed in the joined transcript, in order.
            assert "часть 1: говорили о Beta" in (row.transcript_text or "")
            assert "часть 2: договорились на четверг" in (row.transcript_text or "")
            idx_a = row.transcript_text.index("часть 1")
            idx_b = row.transcript_text.index("часть 2")
            assert idx_a < idx_b
        # Both chunks went through Whisper, and Fireflies-side
        # transcript fallback was NOT called.
        assert len(call_log) == 2
        assert client.transcript_text_calls == []
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_pipeline_idempotent_when_already_processed(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Re-running on a fully-processed recording is a no-op:
    every step's flag is set, so `process_one` short-circuits
    with `skipped_reason='already_processed'` (FR-CR-05-53).

    Needs an `admin_user_ids()` recipient so the short-summary
    step actually flips its flag — without it
    `short_summary_sent` stays False and the early-return
    check (which now demands every flag, FR-CR-05-53) wouldn't
    fire."""
    def fake_transcribe(**kw):
        return "x"

    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "555")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes", fake_transcribe
        )
        settings = _settings_with_audio_dir()
        client = _FakeFirefliesClient(transcripts=[_fake_transcript("trans-2")])
        llm = _FakeLLM(tasks=[{"title": "t1"}])
        sender = _FakeSender()
        pipeline = FirefliesPipeline(
            settings=settings,
            client=client,
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=sender,
        )
        with SessionFactory() as s:
            t = client.list_transcripts(limit=1)[0]
            first = pipeline.process_one(s, t)
            s.commit()
        assert first.tasks_created == 1
        assert first.skipped_reason is None

        with SessionFactory() as s:
            t = client.list_transcripts(limit=1)[0]
            second = pipeline.process_one(s, t)
            s.commit()
        assert second.skipped_reason == "already_processed"
        # No new tasks.
        with SessionFactory() as s:
            assert (
                s.query(Task)
                .filter(Task.source_kind == TaskSourceKind.fireflies)
                .count()
                == 1
            )
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_pipeline_retries_failed_step_on_rerun(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-53 — when an upstream step (e.g. Google Docs
    export) fails on the first run, a re-run should NOT short-
    circuit on `processed_at`. The retry must reach the failed
    step and try again.

    Reproduces the production bug operator hit: Docs API was
    disabled, doc_exported stayed False, but processed_at +
    tasks_extracted made the recording look 'done' so the
    next migrate_fireflies skipped it instead of retrying."""
    def fake_transcribe(**kw):
        return "x"

    monkeypatch.setattr(
        "app.services.transcription.transcribe_bytes", fake_transcribe
    )

    class _FlakyDocs:
        def __init__(self):
            self.calls = 0

        def export_summary(self, *, title, body, parent_folder_id=""):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Docs API not enabled")
            return ("doc-id-321", "https://docs.google.com/document/d/doc-id-321/edit")

    docs = _FlakyDocs()
    settings = _settings_with_audio_dir()
    client = _FakeFirefliesClient(transcripts=[_fake_transcript("trans-flaky")])
    llm = _FakeLLM(tasks=[{"title": "t1"}])
    pipeline = FirefliesPipeline(
        settings=settings,
        client=client,
        llm_backend=llm,
        docs_factory=lambda: docs,
        sender=None,
    )
    with SessionFactory() as s:
        t = client.list_transcripts(limit=1)[0]
        first = pipeline.process_one(s, t)
        s.commit()
    # First run: doc_exported failed but everything else flipped.
    assert first.skipped_reason is None
    assert first.google_doc_url is None
    assert first.tasks_created == 1
    # Re-run: should NOT short-circuit, should retry the doc step.
    with SessionFactory() as s:
        t = client.list_transcripts(limit=1)[0]
        second = pipeline.process_one(s, t)
        s.commit()
    assert second.skipped_reason is None
    assert second.google_doc_url == "https://docs.google.com/document/d/doc-id-321/edit"
    assert docs.calls == 2  # second call succeeded


def test_pipeline_admin_fallback_for_unresolved_owner(
    patched_session_scope, SessionFactory, monkeypatch
):
    """Task extraction fallback chain mirrors FR-CR-05-09: when
    the LLM emits an `owner` that doesn't resolve in
    known_employees (or null), the admin uid wins."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "888")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        def fake_transcribe(**kw):
            return "x"

        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes", fake_transcribe
        )
        settings = _settings_with_audio_dir()
        client = _FakeFirefliesClient(transcripts=[_fake_transcript("trans-3")])
        # LLM returns null owner — should fall through to admin.
        llm = _FakeLLM(tasks=[{"title": "сделать", "owner": None}])
        pipeline = FirefliesPipeline(
            settings=settings,
            client=client,
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=None,
        )
        with SessionFactory() as s:
            s.add(
                TeamMember(
                    real_name="Admin", telegram_user_id=888,
                    active=True,
                )
            )
            s.flush()
            t = client.list_transcripts(limit=1)[0]
            pipeline.process_one(s, t)
            s.commit()
        with SessionFactory() as s:
            task = (
                s.query(Task)
                .filter(Task.source_kind == TaskSourceKind.fireflies)
                .first()
            )
            assert task is not None
            assert task.owner_user_id == "888"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_truncate_caps_at_limit():
    """Hard-cap on the short-summary length keeps Telegram happy
    even when the LLM blows past 2000 chars."""
    text = "слово " * 1000  # ~6000 chars
    cut = _truncate(text, limit=2000)
    assert len(cut) <= 2000
    assert cut.endswith("…")


def test_truncate_passthrough_when_short():
    text = "короткий"
    assert _truncate(text, limit=2000) == text


# --------------------------------------------------------------------------- #
# FR-CR-05-117 — short summary format + auto-stamp title derivation
# --------------------------------------------------------------------------- #


def test_looks_like_auto_stamp_title_detects_fireflies_defaults():
    """FR-CR-05-117 — Fireflies stamps untitled meetings as
    «Apr 30, 03:32 PM» / «May 5 at 5pm». The pipeline replaces
    those with an LLM-derived business topic before the LLM
    summary call. The detector must catch every variant the
    operator has reported."""
    assert _looks_like_auto_stamp_title("Apr 30, 03:32 PM") is True
    assert _looks_like_auto_stamp_title("May 5 at 5pm") is True
    assert _looks_like_auto_stamp_title("september 12, 11:00 AM") is True
    assert _looks_like_auto_stamp_title("Dec 1") is True
    assert _looks_like_auto_stamp_title("  Jan 3, 09:00 AM  ") is True
    # Empty / None counts too — no title is just as bad.
    assert _looks_like_auto_stamp_title("") is True
    assert _looks_like_auto_stamp_title(None) is True
    # Real business titles must NOT match.
    assert _looks_like_auto_stamp_title("ADNOC partnership call") is False
    assert _looks_like_auto_stamp_title("Раунд Humanoid") is False
    assert _looks_like_auto_stamp_title("Goldman Sachs intro") is False
    assert _looks_like_auto_stamp_title("Mayfield prep") is False


def test_short_summary_prompt_pins_operator_format():
    """FR-CR-05-117 — operator pinned the ADNOC layout as the
    canonical short-summary shape:

        <Тема> — DD.MM.YYYY | NN мин

        Их сторона: …
        Наша сторона: …

        Суть: <2-4 sentences>

        To-Do:
        1) …
        2) …

    The system prompt must reference every section so the LLM
    sticks to the format. This test is the regression guard for
    the «🎙 Apr 30, 03:32 PM / 0 мин / без раздела «Их сторона»»
    output we shipped before."""
    from app.fireflies.prompts import SHORT_SUMMARY_SYSTEM

    blob = SHORT_SUMMARY_SYSTEM
    # Header shape pinned.
    assert "DD.MM.YYYY" in blob
    assert "NN мин" in blob
    # Two-sided participant split.
    assert "Их сторона" in blob
    assert "Наша сторона" in blob
    # «Суть» + «To-Do» sections pinned.
    assert "Суть" in blob
    assert "To-Do" in blob
    # Worked ADNOC example pinned (canonical shape).
    assert "ADNOC" in blob
    # Anti-regression: explicit ban on Fireflies auto-stamps in
    # the output.
    assert "auto-stamp" in blob.lower() or "auto-timestamp" in blob.lower() or \
           "auto-stamp" in blob or "Apr 30" in blob


# --------------------------------------------------------------------------- #
# FR-CR-05-57/58/59 — owner routing + DM cards + doc sharing
# --------------------------------------------------------------------------- #


def test_task_extraction_prompt_pins_role_notes_and_assistant_routing():
    """FR-CR-05-57 — Fireflies task extraction prompt teaches
    the LLM to route tasks via role / notes / assistant rules,
    not just by spoken name. Without this the LLM picks the
    «AI Lead» row for routine prep work because the speaker
    (Артём) is mentioned, leaving the actual operator
    (Ирина — Артём's assistant per his notes) idle."""
    from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM

    blob = TASK_EXTRACTION_SYSTEM
    # Role / notes used as source of truth.
    assert "ROLE / NOTES" in blob or "role / notes" in blob.lower()
    # Assistant routing rule pinned.
    assert "ассистент" in blob.lower() or "assistant" in blob.lower()
    assert "только стратегические" in blob.lower() or "only strategic" in blob.lower()
    # AI-Lead anti-default is pinned (regression: 4-task batch
    # all landed on Lead AI).
    assert "AI Lead" in blob or "Lead AI" in blob
    # Worked example pinned.
    assert "Артём" in blob and "Ирина" in blob
    # Speaker ≠ assignee rule pinned.
    assert "speaker" in blob.lower() or "SPEAKER" in blob


def test_pipeline_posts_tg_card_per_extracted_task(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-58 — every Fireflies-created task gets a DM
    card posted to the admin (and owner if different) so the
    operator sees them in TG, not just in the Sheet."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        def fake_transcribe(**kw):
            return "talked through everything in the meeting"

        monkeypatch.setattr(
            "app.services.transcription.transcribe_bytes", fake_transcribe
        )
        settings = _settings_with_audio_dir()
        client = _FakeFirefliesClient(transcripts=[_fake_transcript("trans-cards")])
        llm = _FakeLLM(
            tasks=[
                {"title": "first task", "owner": "777"},
                {"title": "second task", "owner": "777"},
            ]
        )
        sender = _FakeSender()
        pipeline = FirefliesPipeline(
            settings=settings,
            client=client,
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=sender,
        )
        with SessionFactory() as s:
            s.add(
                TeamMember(
                    real_name="Admin",
                    telegram_user_id=777,
                    active=True,
                )
            )
            s.flush()
            t = client.list_transcripts(limit=1)[0]
            report = pipeline.process_one(s, t)
            s.commit()

        assert report.tasks_created == 2
        # Two DMs went to chat_id=777 with task-card-shaped text
        # (look for the body markers `📝` description / due
        # icons that build_task_card_text produces).
        admin_cards = [
            m for m in sender.sent
            if m["chat_id"] == 777
            and ("first task" in m["text"] or "second task" in m["text"])
        ]
        assert len(admin_cards) == 2
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_docs_export_share_anyone_with_link():
    """FR-CR-05-59 — `export_summary` shares the new doc as
    anyone-with-link writer by default so the Telegram link in
    the short summary is openable by every teammate without a
    per-person share dance."""
    from app.sync.docs import DocsExportService

    class _FakeFiles:
        def __init__(self):
            self.create_calls: list[dict] = []
            self.permission_calls: list[dict] = []

        def create(self, **kw):
            self.create_calls.append(kw)
            class _Exec:
                def execute(self_):
                    return {"id": "doc-xyz"}
            return _Exec()

    class _FakePermissions:
        def __init__(self, files):
            self._files = files

        def create(self, **kw):
            self._files.permission_calls.append(kw)
            class _Exec:
                def execute(self_):
                    return {"id": "perm-1"}
            return _Exec()

    files = _FakeFiles()
    perms = _FakePermissions(files)

    class _FakeDriveBuild:
        def files(self):
            return files

        def permissions(self):
            return perms

    class _FakeDocsBuild:
        def documents(self):
            class _D:
                def batchUpdate(self_, **kw):
                    class _E:
                        def execute(self__):
                            return {}
                    return _E()
            return _D()

    svc = DocsExportService.__new__(DocsExportService)
    svc._docs = _FakeDocsBuild()
    svc._drive = _FakeDriveBuild()

    doc_id, url = svc.export_summary(
        title="x",
        body="some text",
        parent_folder_id="folder-123",
    )
    assert doc_id == "doc-xyz"
    assert url == "https://docs.google.com/document/d/doc-xyz/edit"
    # Doc was created inside the Shared Drive folder.
    assert files.create_calls[0]["body"]["parents"] == ["folder-123"]
    assert files.create_calls[0]["supportsAllDrives"] is True
    # Permission was created: anyone, writer.
    assert files.permission_calls
    perm_body = files.permission_calls[0]["body"]
    assert perm_body == {"type": "anyone", "role": "writer"}
    assert files.permission_calls[0]["supportsAllDrives"] is True


def test_docs_export_skip_share_when_role_none():
    """`share_role=None` skips the permissions.create call."""
    from app.sync.docs import DocsExportService

    class _FakeFiles:
        def __init__(self):
            self.create_calls: list[dict] = []

        def create(self, **kw):
            self.create_calls.append(kw)
            class _E:
                def execute(self_):
                    return {"id": "doc-y"}
            return _E()

    class _FakeDrive:
        def __init__(self, f):
            self._f = f

        def files(self):
            return self._f

        def permissions(self):
            raise RuntimeError("should not be called")

    files = _FakeFiles()

    class _FakeDocsBuild:
        def documents(self):
            class _D:
                def batchUpdate(self_, **kw):
                    class _E:
                        def execute(self__):
                            return {}
                    return _E()
            return _D()

    svc = DocsExportService.__new__(DocsExportService)
    svc._docs = _FakeDocsBuild()
    svc._drive = _FakeDrive(files)
    doc_id, _ = svc.export_summary(
        title="x", body="y", parent_folder_id="f",
        share_role=None,
    )
    assert doc_id == "doc-y"
