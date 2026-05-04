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
    _sniff_audio_extension,
    _strip_markdown_emphasis,
    _strip_uid_suffixes,
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

    def complete_text(self, *, system_prompt, user_prompt,
                      model=None, temperature=0.2,
                      reasoning_effort=None, response_format=None):
        self.complete_calls.append(system_prompt[:40])
        # FR-CR-05-129 — task extract / verifier now use JSON-
        # mode complete_text instead of call_tool. Detect via
        # system prompt keywords and return a JSON string.
        import json as _json
        if "SECOND-PASS verifier" in (system_prompt or ""):
            return _json.dumps({"tasks": []})
        if (
            "extract ACTIONABLE TASKS" in (system_prompt or "")
            or "ACTIONABLE TASKS" in (system_prompt or "")
            or "Extract action items" in (system_prompt or "")
        ):
            return _json.dumps({"tasks": list(self.tasks)})
        if "DETAILED" in system_prompt or "ДЕТАЛЬНЫЙ" in system_prompt:
            return self.detailed
        return self.short

    def call_tool(self, *, system_prompt, tool_name, **kw):
        # Back-compat: kept for any other call sites.
        self.tool_calls.append(tool_name)
        if "SECOND-PASS verifier" in (system_prompt or ""):
            return {"tasks": []}
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
        def fake_transcribe(*, audio_bytes, mimetype, filename, openai_api_key, model="whisper-1", prompt=None):
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
            # FR-CR-05-127 — title becomes an HTML hyperlink to
            # the Google Doc; the old «📄 Подробный отчёт: <url>»
            # trailer line is gone. Sender uses parse_mode=HTML
            # so the `<a href>` block renders as a clickable
            # title in Telegram. Built deterministically (NOT by
            # the LLM) so it can't be truncated mid-link or
            # hallucinated.
            assert "<a href=" in (row.short_summary or "")
            assert (row.google_doc_url or "") in (row.short_summary or "")
            assert "📄 Подробный отчёт:" not in (row.short_summary or "")
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


def test_pipeline_leaves_owner_null_when_llm_declined_to_assign(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-134 — operator-pinned: when the LLM returns
    ``owner=null`` (Rule 6 anti-admin-default kicked in
    correctly), the pipeline MUST NOT silently route the task
    to the admin user. Operator regression: «прислать email для
    отправки deck» landed on Андрей (AI Lead) because Python
    overrode Rule 6 with a hard fallback to admin_uid. Now the
    task surfaces as owner=null and the operator assigns it
    manually from the card."""
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
            assert task.owner_user_id is None
            assert task.owner_display_name in (None, "")
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


def test_sniff_audio_extension_detects_real_container(tmp_path):
    """FR-CR-05-117 — Zoom never includes the file extension in
    `download_url`, and the actual M4A audio it serves used to
    be saved as `.mp4` because the URL didn't say «m4a». That
    tripped Whisper («Invalid file format») and the chunker
    («Exactly one MP3 audio stream is required»). Sniffer reads
    the first 16 bytes and returns the real container, which the
    Zoom pipeline uses to pick a .m4a / .mp3 / etc. extension."""
    cases = {
        # M4A (Zoom's default audio-only export).
        "m4a": b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 4,
        # Real video MP4.
        "mp4": b"\x00\x00\x00\x20" + b"ftyp" + b"mp42" + b"\x00" * 4,
        # MP3 with ID3v2 header (Fireflies path).
        "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 6,
        # WAV.
        "wav": b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 0,
        # Ogg.
        "ogg": b"OggS\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        # FLAC.
        "flac": b"fLaC\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    }
    for ext, magic in cases.items():
        f = tmp_path / f"sample.bin"
        f.write_bytes(magic)
        assert _sniff_audio_extension(str(f)) == ext, ext

    # Mystery garbage → None (caller falls back to a default).
    f = tmp_path / "junk.bin"
    f.write_bytes(b"not-a-known-magic-prefix-123456")
    assert _sniff_audio_extension(str(f)) is None

    # Missing file → None, no exception.
    assert _sniff_audio_extension(str(tmp_path / "nope")) is None


def test_split_audio_chunker_preserves_input_container(monkeypatch, tmp_path):
    """FR-CR-05-117 — chunker used to force `.mp3` output via
    `-c copy`, which fails when the source is M4A/AAC. After
    the fix, output extension equals input extension so
    «-c copy» is always a valid combo."""
    from app.fireflies import pipeline as fp

    # Stub ffmpeg + ffprobe so the test doesn't need the binary.
    monkeypatch.setattr(fp.shutil, "which", lambda _name: "/usr/bin/" + _name)
    monkeypatch.setattr(fp, "_ffprobe_duration_seconds", lambda _p: 60.0)

    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        # Materialise the expected output file so the chunker's
        # post-condition («ffmpeg produced no output») passes.
        out_path = cmd[-1]
        with open(out_path, "wb") as f:
            f.write(b"x" * 100)

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr(fp.subprocess, "run", fake_run)

    src = tmp_path / "meeting.m4a"
    src.write_bytes(b"x" * (30 * 1024 * 1024))

    chunks = fp._split_audio_into_chunks(str(src), max_bytes=10 * 1024 * 1024)
    assert chunks, "chunker returned nothing"
    for ch in chunks:
        assert ch.endswith(".m4a"), ch  # NOT .mp3
    # Verify the ffmpeg call carried `-c copy` and an .m4a output.
    assert any("-c" in cmd and cmd[-1].endswith(".m4a") for cmd in captured)


def test_split_for_telegram_keeps_overview_block_in_first_message():
    """FR-CR-05-126 — operator pinned: «Header + Участники +
    Суть + To-Do» MUST land in ONE message. Optional «🔗
    Контрагенты» / «📄 Подробный отчёт» trailers can spill to
    a second message. Pre-fix the greedy packer split between
    Суть and To-Do because To-Do was very long; operator saw
    the overview broken across two DMs."""
    from app.fireflies.pipeline import _split_for_telegram

    body = (
        "01/05 - Fundraising sync\n\n"
        "Участники: Артем, Алина, Дима\n\n"
        "Суть: " + ("очень подробное описание встречи. " * 50) + "\n\n"
        "To-Do:\n" + "\n".join(
            f"{i}) Длинная задача с ответственным." for i in range(1, 30)
        ) + "\n\n"
        "🔗 Контрагенты: ADNOC, Bosch, Tether\n\n"
        "📄 Подробный отчёт: https://docs.google.com/document/d/X/edit"
    )
    chunks = _split_for_telegram(body, limit=3800)
    # First chunk must contain BOTH «Суть» and «To-Do» — the
    # overview block.
    assert len(chunks) >= 1
    assert "Суть:" in chunks[0]
    assert "To-Do:" in chunks[0]
    # Trailers go to the second chunk.
    if len(chunks) > 1:
        rest = "\n\n".join(chunks[1:])
        assert "🔗 Контрагенты" in rest or "📄 Подробный отчёт" in rest

    # Short body that fits in one chunk → single message.
    short_body = (
        "01/05 - Quick sync\n\nУчастники: А\n\nСуть: short.\n\n"
        "To-Do:\n1) Done.\n\n"
        "📄 Подробный отчёт: https://docs.google.com/document/d/X/edit"
    )
    assert len(_split_for_telegram(short_body, limit=3800)) == 1


def test_split_for_telegram_chunks_at_paragraph_boundaries():
    """FR-CR-05-119 — operator regression: 25-task To-Do block
    pushed `short_summary` to 10 KB, Telegram returned
    «Bad Request: message is too long» and dropped the message
    entirely. Splitter chunks at `\\n\\n` boundaries so each
    Telegram DM stays under the 4096-char per-message cap and
    each chunk starts on a fresh section («Их сторона», «Суть»,
    «To-Do»)."""
    from app.fireflies.pipeline import _split_for_telegram

    # Empty / falsy → [].
    assert _split_for_telegram("") == []
    assert _split_for_telegram(None) == []  # type: ignore[arg-type]

    # Short input → single chunk.
    short = "ADNOC — 30.04.2026\n\nИх сторона: …\n\nСуть: blah."
    assert _split_for_telegram(short, limit=3800) == [short]

    # Long input split at paragraph boundaries.
    para = "Это длинный параграф с разными словами " * 60  # ~2000 chars
    body = "Header\n\n" + para + "\n\n" + para + "\n\n" + para
    chunks = _split_for_telegram(body, limit=3800)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 3800

    # Single paragraph longer than the limit gets hard-split at
    # word boundary.
    huge = "x" * 5000
    chunks_huge = _split_for_telegram(huge, limit=1000)
    for c in chunks_huge:
        assert len(c) <= 1000


def test_build_full_tasks_section_for_doc_renders_verbose_with_meta(session):
    """FR-CR-05-119 follow-up — Google Doc gets the FULL task
    list (verbatim multi-sentence descriptions + owner + due +
    priority). Distinct from the short-summary helper which
    one-sentence-compresses. Pipeline order is detailed →
    tasks → doc → short so by doc-export the Task rows exist."""
    from datetime import date, time as _time

    from app.fireflies.pipeline import (
        _build_full_tasks_section_for_doc,
    )
    from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus

    # Empty case → "" so the doc body stays clean.
    assert _build_full_tasks_section_for_doc(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="trans-empty",
    ) == ""

    session.add(
        Task(
            title="Подготовить письмо",
            description=(
                "Алина подготовит письмо инвесторам с приложенным "
                "контрактом и базовой суммой. В тексте отметить "
                "NDA и проверить список рассылки."
            ),
            priority=TaskPriority.high,
            status=TaskStatus.todo,
            owner_display_name="Алина",
            due_date=date(2026, 5, 15),
            due_time=_time(18, 0),
            source_kind=TaskSourceKind.fireflies,
            source_conversation_id="trans-doc",
        )
    )
    session.add(
        Task(
            title="Скоординировать тайминг",
            description="Ирина скоординирует тайминг рассылки.",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
            owner_display_name="Ирина Шипилова",
            due_date=date(2026, 5, 16),
            source_kind=TaskSourceKind.fireflies,
            source_conversation_id="trans-doc",
        )
    )
    session.flush()

    out = _build_full_tasks_section_for_doc(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="trans-doc",
    )
    # Section header pinned.
    assert "📌 ЗАДАЧИ" in out
    # Full multi-sentence description preserved (NOT compressed
    # — that's the short-summary helper's job).
    assert "В тексте отметить NDA" in out
    # Owner / due / priority rendered as meta line.
    assert "Ответственный: Алина" in out
    assert "Срок: 15.05.2026 18:00" in out
    assert "Приоритет: high" in out
    # Default-medium priority NOT printed (less noise).
    assert "Приоритет: medium" not in out
    # Default-no-time due-date renders date-only.
    assert "Срок: 16.05.2026" in out


def test_short_summary_prompt_uses_new_dd_mm_header_format():
    """FR-CR-05-120 — operator updated header to «DD/MM -
    <Topic>» (was «<Topic> — DD.MM.YYYY | NN мин») and
    flattened participants into a single «Участники:» line
    (was a two-side «Их сторона / Наша сторона» split). New
    canonical example pinned in the prompt."""
    from app.fireflies.prompts import SHORT_SUMMARY_SYSTEM

    blob = SHORT_SUMMARY_SYSTEM
    assert "DD/MM" in blob
    assert "30/04 - ADNOC" in blob
    assert "Участники: Fabrizio Siraguzano" in blob
    # Old format gone from the example.
    assert "30.04.2026 | 57 мин" not in blob
    assert "Их сторона: Fabrizio" not in blob


def test_task_extraction_prompt_pins_topic_action_description_format():
    """FR-CR-05-120 — operator pinned: each Task description
    follows «<тема> - <конкретное действие>» format so the
    short summary's To-Do can use it verbatim. Worked examples
    pinned (Schaeffler / Draper / QIA / варанты) so a future
    prompt rewrite can't accidentally drop the format."""
    from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM

    blob = TASK_EXTRACTION_SYSTEM
    # FR-CR-05-128 — format pinned with stronger language: subject
    # comes FIRST, verb after the dash. The literal «<тема-или-фонд>»
    # placeholder is in the prompt now.
    assert "<тема-или-фонд>" in blob
    assert "<глагол-действие" in blob
    assert "Schaeffler" in blob or "Шафлера" in blob
    assert "Draper Associates" in blob
    assert "Интро к катарскому шейху" in blob
    assert "Варанты" in blob or "Варанты для инвесторов" in blob
    # FR-CR-05-128 — anti-examples pin the subject-first contract.
    assert (
        "starts with verb" in blob.lower()
        or "starts with verb" in blob
    )


def test_task_extraction_prompt_pins_thinking_guidance():
    """FR-CR-05-120 — operator switched task extraction to a
    reasoning model and asked for «extract ALL tasks, don't
    miss any». Prompt has the THINK CAREFULLY block with the
    «8-25 tasks per 30-min meeting» heuristic and the «walk
    the known_employees table item by item» owner-selection
    guidance."""
    from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM

    blob = TASK_EXTRACTION_SYSTEM
    assert "THINK CAREFULLY" in blob
    # FR-CR-05-129 — operator-pinned: «нет никакого таргета по
    # количеству, ты извлекаешь задачи из длинного саммери:
    # надо длинное саммери чтобы включало максимум информации,
    # это по сути транскрипт структурированный». Pin the
    # «MAXIMUM DETAIL / NO TARGET COUNT / Granularity beats
    # brevity» framing.
    assert "MAXIMUM DETAIL" in blob or "максимум информации" in blob
    assert "Granularity" in blob or "granularity" in blob
    assert "walk the `known_employees`" in blob or (
        "walk the known_employees" in blob
    )


def test_task_verification_prompt_pins_second_pass_contract():
    """FR-CR-05-121 — verifier prompt forbids duplicating
    already-extracted tasks, allows empty `{"tasks": []}` as a
    valid response, reuses the FR-CR-05-120 description format
    + rule 7 (named-assignee), and pins the «SECOND-PASS
    verifier» framing the FakeLLM keys off in tests."""
    from app.fireflies.prompts import TASK_VERIFICATION_SYSTEM

    blob = TASK_VERIFICATION_SYSTEM
    assert "SECOND-PASS verifier" in blob
    # Empty-list-is-fine framing pinned.
    assert '"tasks": []' in blob or '`{"tasks": []}`' in blob
    # No-duplicates rule pinned.
    assert "DO NOT duplicate" in blob or "do not duplicate" in blob.lower()
    # Same description format the first pass uses.
    assert "<тема> - <конкретное действие" in blob
    # Same rules 6 (anti-admin-default) and 7 (named-assignee)
    # carried over.
    assert "NEVER pick the admin" in blob
    assert "NAMED ASSIGNEE OVERRIDES" in blob


def test_owner_assignment_full_team_context_consistent_across_all_paths():
    """FR-CR-05-123 — owner assignment in TG, Fireflies, and
    Zoom paths must all feed the LLM the same full team_members
    context (role + notes columns from the team-sheet pull).
    Different code paths, same data shape, same routing rules.

    TG path: separate `OWNER_SYSTEM_PROMPT` (FR-CR-04-04).
    Fireflies / Zoom: combined into `TASK_EXTRACTION_SYSTEM`
    (one LLM call extracts + routes per FR-CR-05-117/120).

    This test pins:
      - `as_known_employees(session)` returns rows with role +
        notes for both `prefer_telegram=True` (meeting paths)
        and `prefer_telegram=False` (TG path default).
      - All three prompts pass the rendered employee table with
        the role + notes columns visible to the LLM.
      - Rule 7 (named-assignee) and rule 6 (anti-admin-default)
        are pinned in BOTH the TG owner prompt AND the meeting
        task-extraction prompt."""
    from app.fireflies.pipeline import _render_known_employees_table
    from app.fireflies.prompts import (
        TASK_EXTRACTION_SYSTEM,
        TASK_VERIFICATION_SYSTEM,
    )
    from app.intent.owner_prompt import (
        OWNER_SYSTEM_PROMPT,
        build_owner_user_prompt,
    )

    # Fixture employee rows mirror what `as_known_employees`
    # produces (role + notes are always populated when the
    # operator filled them in the sheet).
    employees = [
        {
            "slack_user_id": "412243973",
            "display_name": "msfrecklie",
            "real_name": "Алина Колпакова",
            "role": "CSO / CMO",
            "notes": "Strategy, Marketing, PR, Fundraising narrative",
        },
        {
            "slack_user_id": "700469400",
            "display_name": "IrinaMorato",
            "real_name": "Ирина Шипилова",
            "role": "Ассистент CEO",
            "notes": "Ведёт оперативку, follow-ups, ассистент Артёма",
        },
    ]

    # 1. Fireflies / Zoom — combined extraction prompt sees the
    # rendered table with role + notes columns.
    table_meeting = _render_known_employees_table(employees)
    assert "| role" in table_meeting and "| notes" in table_meeting
    assert "CSO / CMO" in table_meeting
    assert "Ведёт оперативку" in table_meeting
    assert "Стратегия" in table_meeting or "Strategy" in table_meeting

    # 2. TG path — separate OWNER_SYSTEM_PROMPT call sees the
    # same role + notes shape.
    tg_user_prompt = build_owner_user_prompt(
        source_text="Алина, подготовь презу.",
        context_messages=[],
        author_user_id="97239970",
        known_employees=employees,
    )
    assert "| role" in tg_user_prompt and "| notes" in tg_user_prompt
    assert "CSO / CMO" in tg_user_prompt
    assert "Ведёт оперативку" in tg_user_prompt

    # 3. Rule 7 (named-assignee) pinned in both surfaces.
    assert "NAMED ASSIGNEE OVERRIDES" in TASK_EXTRACTION_SYSTEM
    assert "NAMED ASSIGNEE OVERRIDES" in TASK_VERIFICATION_SYSTEM
    # TG owner prompt has its own named-assignee guidance
    # (FR-CR-04-04: «if the source text or context names a
    # specific assignee, prefer that»).
    tg_lower = OWNER_SYSTEM_PROMPT.lower()
    assert (
        "name" in tg_lower and (
            "assignee" in tg_lower or "explicit" in tg_lower
        )
    )

    # 4. Rule 6 (anti-admin-default / null > admin) pinned for
    # Fireflies/Zoom path.
    assert "STRICTLY BETTER" in TASK_EXTRACTION_SYSTEM


def test_as_known_employees_returns_role_and_notes(session):
    """FR-CR-05-123 — `as_known_employees(session)` must
    surface both `role` and `notes` columns from the team_members
    table, regardless of which channel preference the caller
    asks for. Without this the LLM owner-routing prompt loses
    the context the operator typed into the team sheet."""
    from app.models import TeamMember
    from app.services.team_members import as_known_employees

    session.add(
        TeamMember(
            real_name="Тестовый",
            telegram_user_id=999,
            telegram_username="testovii",
            slack_user_id="UTEST123",
            role="Аналитик",
            notes="Подготовка справок по людям и фондам",
            active=True,
        )
    )
    session.flush()

    # prefer_telegram=True path (meeting pipelines).
    rows_tg = as_known_employees(session, prefer_telegram=True)
    target = next(
        (r for r in rows_tg if r.get("real_name") == "Тестовый"), None
    )
    assert target is not None, "missing test employee"
    assert target["role"] == "Аналитик"
    assert target["notes"] == "Подготовка справок по людям и фондам"
    # On prefer_telegram=True, the slack_user_id field carries
    # whatever id was preferred for that channel — usually the
    # TG numeric id.
    assert target["slack_user_id"] == "999"

    # prefer_telegram=False path (Slack / TG default for ingest).
    rows_slack = as_known_employees(session, prefer_telegram=False)
    target = next(
        (r for r in rows_slack if r.get("real_name") == "Тестовый"), None
    )
    assert target is not None
    assert target["role"] == "Аналитик"
    assert target["notes"] == "Подготовка справок по людям и фондам"
    assert target["slack_user_id"] == "UTEST123"


def test_first_sentence_compresses_multi_sentence_description():
    """FR-CR-05-119 follow-up — short TG summary's «To-Do» line
    needs ONE sentence per task even when the full description
    on the Task row is multi-sentence. The full text still lives
    on the per-task DM card; this just compresses for the
    summary so 25 tasks fit in a few Telegram messages instead
    of 25 KB."""
    from app.fireflies.pipeline import _first_sentence

    # Empty / falsy → "".
    assert _first_sentence("") == ""
    assert _first_sentence(None) == ""  # type: ignore[arg-type]

    # Single sentence → returned as is.
    one = "Алина подготовит письмо инвесторам."
    assert _first_sentence(one) == one

    # Multi-sentence → keep only the first.
    multi = (
        "Алина подготовит письмо инвесторам с приложенным контрактом. "
        "В письме отметить также NDA и базовую сумму. "
        "Список рассылки уточнить с Ирой."
    )
    assert _first_sentence(multi) == (
        "Алина подготовит письмо инвесторам с приложенным контрактом."
    )

    # «И. Иванов»-style abbreviation isn't taken as the boundary
    # (the period is at index < 30 so we look further).
    short_initials = (
        "По договорённости с И. Ивановым подготовить апдейт инвесторам "
        "на следующей неделе."
    )
    assert _first_sentence(short_initials).startswith(
        "По договорённости с"
    )

    # No period at all → capped at limit with ellipsis on word
    # boundary.
    long_no_period = (
        "очень длинная строка без точек " * 20
    ).strip()
    out = _first_sentence(long_no_period, limit=120)
    assert len(out) <= 120
    assert out.endswith("…")


def test_build_todo_section_renders_tasks_verbatim_with_owner(session):
    """FR-CR-05-120: To-Do block items use the task description
    VERBATIM (the LLM is told to write in «<topic> - <action>»
    format already, so we trust the row content). Hard-cap at
    350 chars guards against runaway emits. Owner in parens
    only when set — empty owner drops the parens entirely.
    Soft-deleted tasks excluded; no tasks → empty string."""
    from app.fireflies.pipeline import _build_todo_section
    from app.models import Task, TaskPriority, TaskSourceKind, TaskStatus

    # Empty case → "" (caller drops the section).
    assert _build_todo_section(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="trans-empty",
    ) == ""

    # Multi-sentence description → first sentence only.
    session.add(
        Task(
            title="Подготовить письмо",
            description=(
                "Алина подготовит письмо инвесторам с приложенным "
                "контрактом и базовой суммой. В тексте отметить "
                "NDA и проверить список рассылки."
            ),
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
            owner_display_name="Алина",
            source_kind=TaskSourceKind.fireflies,
            source_conversation_id="trans-ok",
        )
    )
    session.add(
        Task(
            title="Скоординировать тайминг",
            description="Ирина скоординирует тайминг рассылки по сегментам.",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
            owner_display_name="Ирина Шипилова",
            source_kind=TaskSourceKind.fireflies,
            source_conversation_id="trans-ok",
        )
    )
    session.flush()

    out = _build_todo_section(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="trans-ok",
    )
    # FR-CR-05-128 — items separated by blank lines so the
    # splitter chunks BETWEEN tasks, not mid-text. Find each
    # numbered item anywhere in the output.
    assert "To-Do:" in out
    assert "1) Алина подготовит письмо инвесторам" in out
    assert "(Алина)" in out
    assert "2) Ирина скоординирует тайминг рассылки" in out
    assert "(Ирина Шипилова)" in out

    # FR-CR-05-120 — empty owner drops the parens (no
    # «(не назначен)» noise).
    session.add(
        Task(
            title="Орфан",
            description="Orphan task - сделать что-то без назначения.",
            priority=TaskPriority.medium,
            status=TaskStatus.todo,
            owner_display_name=None,
            source_kind=TaskSourceKind.fireflies,
            source_conversation_id="trans-orphan",
        )
    )
    session.flush()
    orphan_out = _build_todo_section(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="trans-orphan",
    )
    assert "1) Orphan task - сделать что-то без назначения." in orphan_out
    assert "(не назначен)" not in orphan_out

    # Soft-deleted tasks are excluded.
    other = Task(
        title="Cancelled",
        description="not visible",
        priority=TaskPriority.medium,
        status=TaskStatus.todo,
        owner_display_name="Кто-то",
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="trans-ok",
        deleted_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    )
    session.add(other)
    session.flush()
    out2 = _build_todo_section(
        session,
        source_kind=TaskSourceKind.fireflies,
        source_conversation_id="trans-ok",
    )
    assert "Cancelled" not in out2


def test_strip_llm_todo_block_removes_emitted_section():
    """FR-CR-05-119 — even with the prompt forbidding it, the
    LLM occasionally still emits a To-Do / Следующие шаги
    section. The pipeline strips it before appending the
    deterministic one so the operator never sees both."""
    from app.fireflies.pipeline import _strip_llm_todo_block

    body = (
        "Header\n\n"
        "Их сторона: X\n"
        "Наша сторона: Y\n\n"
        "Суть: blah blah.\n\n"
        "To-Do:\n"
        "1) old item\n"
        "2) another old item\n"
    )
    cleaned = _strip_llm_todo_block(body)
    assert "To-Do" not in cleaned
    assert "old item" not in cleaned
    assert "Суть: blah blah." in cleaned

    # Russian variant.
    body_ru = (
        "Суть: тестовая встреча.\n\n"
        "Следующие шаги:\n"
        "• сделать X\n"
        "• сделать Y\n"
    )
    cleaned_ru = _strip_llm_todo_block(body_ru)
    assert "Следующие шаги" not in cleaned_ru
    assert "сделать X" not in cleaned_ru


def test_short_summary_prompt_forbids_llm_emitting_todo():
    """FR-CR-05-119 — prompt rule pinned. To-Do is appended by
    the pipeline from the actual Task rows, not by the LLM."""
    from app.fireflies.prompts import SHORT_SUMMARY_SYSTEM

    blob = SHORT_SUMMARY_SYSTEM
    assert "LLM SHOULD NOT EMIT" in blob or "do NOT generate a" in blob
    # «Stop after Суть» framing pinned.
    assert "Stop after" in blob or "stop after" in blob.lower() or (
        "ends at" in blob.lower() or "must end at" in blob.lower()
    )


def test_detailed_summary_prompt_drops_next_steps_section():
    """FR-CR-05-119 — `СЛЕДУЮЩИЕ ШАГИ` no longer in the doc
    structure template; tasks live in their own Task rows + the
    short summary's To-Do block, the doc carries meeting
    context only. The string CAN appear in the explanatory
    «do NOT emit» instruction below the template — we just want
    it gone from the bulleted template the LLM is told to fill."""
    from app.fireflies.prompts import DETAILED_SUMMARY_SYSTEM

    blob = DETAILED_SUMMARY_SYSTEM
    if "Structure:" in blob and "FR-CR-05-119" in blob:
        # Slice the actual template (between the «Structure:»
        # heading and the FR-CR-05-119 instruction line).
        template = blob[
            blob.find("Structure:"): blob.find("FR-CR-05-119")
        ]
        assert "СЛЕДУЮЩИЕ ШАГИ" not in template
        assert "📌" not in template  # the bullet headed the section
    # Plus the prompt has the explicit FR-CR-05-119 instruction
    # forbidding the LLM from emitting the section.
    assert "FR-CR-05-119" in blob
    assert "do NOT emit" in blob


def test_task_extraction_prompt_named_assignee_overrides_admin_default():
    """FR-CR-05-119 rule 7 — when transcript explicitly names a
    person who should do the task («Алине поручено …», «Дима,
    нужно протестировать …»), the LLM MUST pick that name's row
    from `known_employees`. NEVER falls back to admin / a
    different teammate. Pre-fix: «Алине поручено» landed on
    Андрей (admin / AI Lead); «Диме нужно протестировать»
    landed on Viktor."""
    from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM

    blob = TASK_EXTRACTION_SYSTEM
    lower = blob.lower()
    # Rule 7 framing pinned.
    assert "named assignee" in lower or "named assignee" in lower or (
        "named-assignee" in lower or "named assignee overrides" in lower
    )
    # Worked regression examples pinned.
    assert "Алине" in blob or "алине" in lower
    assert "Дима" in blob or "дима" in lower
    # Anti-substitution: don't pick a different teammate.
    assert "NEVER substitute" in blob or "do not substitute" in lower or (
        "never substitute" in lower
    )


def test_strip_markdown_emphasis_removes_paired_markers():
    """FR-CR-05-117 — Google Docs renders the detailed summary
    as plain text, so `**bold**` etc. show up as literal
    asterisks. The pipeline strips paired markdown emphasis
    markers before saving the body to the row / Doc."""
    # **bold** / __bold__
    assert (
        _strip_markdown_emphasis("Это **важно** сегодня.")
        == "Это важно сегодня."
    )
    assert (
        _strip_markdown_emphasis("__Решение__ принято.")
        == "Решение принято."
    )
    # *italic* / _italic_
    assert (
        _strip_markdown_emphasis("Слово *курсивом* в строке.")
        == "Слово курсивом в строке."
    )
    assert (
        _strip_markdown_emphasis("Слово _курсивом_ в строке.")
        == "Слово курсивом в строке."
    )
    # Inline `code`.
    assert _strip_markdown_emphasis("Запусти `make`.") == "Запусти make."
    # Multiline bold preserved across newlines.
    assert (
        _strip_markdown_emphasis("**Линия 1\nЛиния 2**")
        == "Линия 1\nЛиния 2"
    )
    # Literal asterisk in math NOT eaten («3 * 5»).
    assert _strip_markdown_emphasis("Формула: 3 * 5 = 15.") == (
        "Формула: 3 * 5 = 15."
    )
    # Empty / None safe.
    assert _strip_markdown_emphasis("") == ""
    assert _strip_markdown_emphasis(None) is None  # type: ignore[arg-type]


def test_detailed_summary_prompt_forbids_markdown():
    """FR-CR-05-117 — prompt rule pinned. Operator pastes the
    summary into Google Docs which renders `**bold**` as literal
    asterisks."""
    from app.fireflies.prompts import DETAILED_SUMMARY_SYSTEM

    blob = DETAILED_SUMMARY_SYSTEM
    assert "PLAIN TEXT ONLY" in blob or "plain text only" in blob.lower()
    assert "NO MARKDOWN" in blob or "markdown" in blob.lower()
    assert "**" in blob  # the literal forbidden marker is named


def test_strip_uid_suffixes_removes_employee_uids_only():
    """FR-CR-05-117 — operator regression: the LLM occasionally
    copied slack_user_id values from the known_employees table
    into the task description as «Валентина (462156243) и Irina
    Shipilova (700469400)». The post-processor strips those
    suffixes when the parenthesised token matches a real uid,
    while leaving legitimate parentheses untouched."""
    valid_ids = {"462156243", "700469400", "U02XPPN2BTC"}

    # Numeric TG uids stripped.
    out = _strip_uid_suffixes(
        "Участники — Валентина (462156243) и Irina Shipilova "
        "(700469400). Нужно уточнить срок.",
        valid_ids,
    )
    assert "(462156243)" not in out
    assert "(700469400)" not in out
    assert "Валентина" in out
    assert "Irina Shipilova" in out

    # Slack-style uid stripped too.
    out = _strip_uid_suffixes("Спросить Андрея (U02XPPN2BTC).", valid_ids)
    assert "(U02XPPN2BTC)" not in out
    assert "Андрея" in out

    # Legitimate parentheses preserved.
    preserved = "Закрыть раунд в Q2 (2025) на $300k (вторая часть)."
    assert _strip_uid_suffixes(preserved, valid_ids) == preserved

    # Empty / no employees → no-op.
    assert _strip_uid_suffixes("", valid_ids) == ""
    assert _strip_uid_suffixes("Текст (123).", set()) == "Текст (123)."


def test_task_extraction_prompt_forbids_uid_in_description():
    """FR-CR-05-117 — prompt rule pinned. The LLM must not copy
    slack_user_id values into the description prose."""
    from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM

    blob = TASK_EXTRACTION_SYSTEM
    assert "NEVER copy slack_user_id" in blob
    # Worked example pinned in the prompt to make the rule
    # concrete (operator's regression).
    assert "462156243" in blob


def test_short_summary_prompt_pins_operator_format():
    """FR-CR-05-120 — operator updated the canonical layout:

        DD/MM - <Topic>

        Участники: Имя1, Имя2, Имя3

        Суть: <2-4 sentences>

    The pipeline appends «To-Do:» from extracted Task rows; the
    LLM stops at «Суть». Pre-FR-CR-05-120 used the longer
    «<Тема> — DD.MM.YYYY | NN мин» header and a two-line
    «Их сторона / Наша сторона» split; this test pins the new
    flat single-line format.
    """
    from app.fireflies.prompts import SHORT_SUMMARY_SYSTEM

    blob = SHORT_SUMMARY_SYSTEM
    # New header shape pinned.
    assert "DD/MM" in blob
    assert "30/04 - ADNOC" in blob
    # Single-line participants line pinned.
    assert "Участники:" in blob
    # Old two-sided split removed from the pinned example.
    assert "Их сторона: Fabrizio" not in blob
    assert "Наша сторона:" not in blob
    # «Суть» kept; «To-Do» is OUT of the LLM's job.
    assert "Суть" in blob
    # Anti-regression: ban on auto-stamps still pinned.
    assert "auto-stamp" in blob.lower() or "Apr 30" in blob


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


def test_task_extraction_prompt_pins_disambiguate_first_names_via_participants():
    """FR-CR-05-139 — operator regression: «Дима Дроздов» vs
    «Дмитрий Седов» both match a transcript «Дима, …». The
    LLM was assigning fundraising tasks to Седов even when only
    Дроздов was on the call — because Седов's role looked like a
    better fit. Rule 8 inverts that: «participants beat role-
    match» — assign to who was actually present."""
    from app.fireflies.prompts import (
        TASK_EXTRACTION_SYSTEM, TASK_VERIFICATION_SYSTEM,
    )

    for prompt in (TASK_EXTRACTION_SYSTEM, TASK_VERIFICATION_SYSTEM):
        assert "PARTICIPANTS BEAT" in prompt
        assert "meeting_participants" in prompt
        assert "FR-CR-05-139" in prompt
    # Worked example pinning the operator's specific regression.
    assert "Дима Дроздов" in TASK_EXTRACTION_SYSTEM
    assert "Дмитрий Седов" in TASK_EXTRACTION_SYSTEM
    # The rule must NOT say «pick by role even if absent» — the
    # operator regression came from precisely that behaviour.
    assert (
        "DO NOT pick an absent teammate just because their"
        in TASK_EXTRACTION_SYSTEM
    )


def test_task_extraction_prompt_forbids_admin_default_owner():
    """FR-CR-05-117 rule 6 — «прислать строку с таймингами» and
    «уточнить сроки поездки» landed on Андрей Кузьминых (admin /
    AI Lead) because the LLM defaulted to admin when no obvious
    match existed. Rule 6 forbids that: null is STRICTLY BETTER
    than picking the admin / AI Lead. This test pins the
    operator-mandated language so a future prompt rewrite can't
    accidentally drop it."""
    from app.fireflies.prompts import TASK_EXTRACTION_SYSTEM

    blob = TASK_EXTRACTION_SYSTEM
    # «Null is strictly better» language pinned.
    lower = blob.lower()
    assert "null is strictly better" in lower or "strictly better than" in lower
    # «admin» row called out as context-only.
    assert "admin" in lower
    # The rule explicitly mentions the AI Lead anti-default.
    assert "ai lead" in lower or "lead ai" in lower
    # «Don't pick admin unless transcript names them» framing.
    assert "explicitly addresses" in lower or "explicitly names" in lower or (
        "addresses them by name" in lower
    )


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
        # Task cards carry both a priority bullet (🟡 etc.) AND a
        # 👤 owner-line — the FR-CR-05-119 short-summary To-Do
        # section also contains the task descriptions but lacks
        # the 👤 / 📅 card marker, so we filter strictly.
        admin_cards = [
            m for m in sender.sent
            if m["chat_id"] == 777
            and "👤" in m["text"]
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


# ============================================================
# FR-CR-05-127 — title-as-hyperlink + Whisper biasing
# ============================================================


def test_wrap_short_summary_with_doc_link_wraps_first_line_html():
    """FR-CR-05-127 — operator pinned: «'📄 Подробный отчёт:' -
    не выводи, просто гиперссылкой к названию». The first line
    of the body (the «DD/MM - <Topic>» header) is wrapped in
    `<a href="<doc_url>">…</a>`; the rest of the body is HTML-
    escaped so Telegram's `parse_mode=HTML` accepts the message
    even when free-text contains `&` / `<` / `>` (owner names,
    deal numbers etc.). The old «📄 Подробный отчёт: <url>»
    trailer line is NOT appended anymore — the doc-link is on
    the title only."""
    from app.fireflies.pipeline import _wrap_short_summary_with_doc_link

    body = (
        "01/05 - ADNOC\n\n"
        "Участники: Артем, Алина\n\n"
        "Суть: партнёрство по робототехнике.\n\n"
        "To-Do:\n1) Подписать NDA. (Артем)"
    )
    url = "https://docs.google.com/document/d/abc/edit"
    out = _wrap_short_summary_with_doc_link(body, url)
    # First line wrapped exactly once at the top.
    assert out.startswith(f'<a href="{url}">01/05 - ADNOC</a>')
    # No legacy trailer.
    assert "📄 Подробный отчёт" not in out
    # Doc URL is present (in the `href`).
    assert url in out
    # Body content survives (Cyrillic / emoji unaffected by escape).
    assert "Участники: Артем, Алина" in out
    assert "Суть: партнёрство по робототехнике." in out
    # Free-text `&` would be escaped — sanity check the helper is
    # using `html.escape` on the rest of the body.
    body_amp = "01/05 - X\n\nNotes: A & B"
    out_amp = _wrap_short_summary_with_doc_link(body_amp, url)
    assert "A &amp; B" in out_amp

    # Empty / no URL → unchanged (don't HTML-escape when we're
    # not building a link).
    assert _wrap_short_summary_with_doc_link("", url) == ""
    assert _wrap_short_summary_with_doc_link(body, "") == body


def test_short_summary_prompt_no_longer_mentions_old_doc_trailer():
    """FR-CR-05-127 — prompt previously instructed: «No emojis
    except optional `📄 Подробный отчёт: <url>` line appended at
    the very end». Now the trailer is gone (caller wraps the
    header in `<a href>` instead). The prompt must NOT instruct
    the LLM to emit any «📄 Подробный отчёт» / URL trailer of
    its own — that would leak through into the body."""
    from app.fireflies.prompts import SHORT_SUMMARY_SYSTEM

    # The old trailer-line instruction is gone.
    assert "Подробный отчёт: <url>" not in SHORT_SUMMARY_SYSTEM
    # The new contract is documented: caller wraps header in
    # `<a href>`. Pin the FR + key word so a future regression
    # is loud.
    assert "FR-CR-05-127" in SHORT_SUMMARY_SYSTEM
    assert "<a href>" in SHORT_SUMMARY_SYSTEM


def test_build_whisper_bias_prompt_packs_team_and_counterparties(session):
    """FR-CR-05-127 — operator pinned: «Whisper транскрипция
    максимально подробная, не пропускает 'tether'» — bias the
    Whisper call with the operator's canonical name registries
    so brand names don't mutate in transcription. Pack order:
    meeting_title → participants → team_members.real_name →
    counterparties.name."""
    from app.models import Counterparty, TeamMember
    from app.services.transcription import build_whisper_bias_prompt

    session.add_all([
        TeamMember(real_name="Артем Кузьминых",
                   telegram_user_id=111, active=True),
        TeamMember(real_name="Алина",
                   telegram_user_id=222, active=True),
        # Inactive — must be excluded.
        TeamMember(real_name="ExEmployee",
                   telegram_user_id=333, active=False),
    ])
    session.add_all([
        Counterparty(name="Tether",
                     name_normalised="tether"),
        Counterparty(name="Schaeffler",
                     name_normalised="schaeffler"),
        Counterparty(name="ADNOC",
                     name_normalised="adnoc"),
    ])
    session.flush()

    prompt = build_whisper_bias_prompt(
        session,
        meeting_title="Fundraising sync",
        participants=["Артем", "Sean"],
    )
    assert prompt is not None
    # All categories present.
    assert "Fundraising sync" in prompt
    assert "Артем" in prompt  # participant (also team — dedup)
    assert "Sean" in prompt
    assert "Алина" in prompt
    assert "Tether" in prompt
    assert "Schaeffler" in prompt
    assert "ADNOC" in prompt
    # Inactive employees are NOT included.
    assert "ExEmployee" not in prompt
    # Comma-separated packing (not newline / sentence form) so
    # we fit more proper nouns into Whisper's 224-token cap.
    assert ", " in prompt


def test_build_whisper_bias_prompt_returns_none_when_empty(session):
    """No team rows + no counterparties + no meeting metadata →
    None. Caller passes nothing to Whisper rather than an empty
    string."""
    from app.services.transcription import build_whisper_bias_prompt

    assert build_whisper_bias_prompt(session) is None


def test_build_whisper_bias_prompt_caps_at_max_chars(session):
    """Cap at `max_chars` so we don't exceed Whisper's 224-token
    `prompt` limit. Excess names are dropped — the order
    (title → participants → team → counterparties) determines
    priority."""
    from app.models import Counterparty
    from app.services.transcription import build_whisper_bias_prompt

    # 50 long-name counterparties pushes well past any cap.
    for i in range(50):
        session.add(Counterparty(
            name=f"VeryLongCompanyName{i:02d}",
            name_normalised=f"verylongcompanyname{i:02d}",
        ))
    session.flush()

    prompt = build_whisper_bias_prompt(
        session,
        meeting_title="Critical meeting topic that comes first",
        max_chars=200,
    )
    assert prompt is not None
    assert len(prompt) <= 200
    # Highest-priority piece (title) survived.
    assert "Critical meeting topic" in prompt


def test_transcribe_bytes_passes_prompt_to_openai(monkeypatch):
    """FR-CR-05-127 — `prompt` parameter is forwarded to OpenAI's
    Whisper endpoint. Operator regression: «tether» missing from
    transcript because Whisper had no context about brand names;
    fix is to pass them in the `prompt` arg."""
    from app.services import transcription as tr_mod

    captured: dict = {}

    class _FakeResp:
        text = "Tether это плохо распознаётся без подсказки."

    class _FakeAudio:
        class transcriptions:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return _FakeResp()

    class _FakeOpenAI:
        def __init__(self, *_, **__):
            self.audio = _FakeAudio()

    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)

    result = tr_mod.transcribe_bytes(
        audio_bytes=b"FAKE",
        mimetype="audio/mpeg",
        filename="x.mp3",
        openai_api_key="sk-x",
        model="whisper-1",
        prompt="Tether, Schaeffler, ADNOC",
    )
    assert result == "Tether это плохо распознаётся без подсказки."
    assert captured.get("prompt") == "Tether, Schaeffler, ADNOC"

    # No prompt → no `prompt` in kwargs (back-compat: Slack
    # voice path has no biasing).
    captured.clear()
    tr_mod.transcribe_bytes(
        audio_bytes=b"FAKE",
        mimetype="audio/mpeg",
        filename="x.mp3",
        openai_api_key="sk-x",
        prompt=None,
    )
    assert "prompt" not in captured


def test_fireflies_pipeline_passes_whisper_bias_prompt(
    patched_session_scope, SessionFactory, monkeypatch, tmp_path
):
    """FR-CR-05-127 — end-to-end: Fireflies pipeline pulls
    counterparty + team names from the session and forwards them
    to Whisper as the `prompt` argument. Pin the integration so
    a future refactor can't silently drop the biasing."""
    from app.config import get_settings
    from app.fireflies.pipeline import FirefliesPipeline
    from app.models import Counterparty, TeamMember

    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    captured_prompts: list[str | None] = []

    def fake_transcribe(*, audio_bytes, mimetype, filename,
                        openai_api_key, model="whisper-1",
                        prompt=None):
        captured_prompts.append(prompt)
        return "Это тестовый транскрипт встречи."

    monkeypatch.setattr(
        "app.services.transcription.transcribe_bytes", fake_transcribe
    )
    try:
        settings = _settings_with_audio_dir()
        client = _FakeFirefliesClient(transcripts=[_fake_transcript()])
        llm = _FakeLLM(tasks=[])
        pipeline = FirefliesPipeline(
            settings=settings,
            client=client,
            llm_backend=llm,
            docs_factory=lambda: _FakeDocs(),
            sender=_FakeSender(),
        )
        with SessionFactory() as s:
            s.add(TeamMember(
                real_name="Артем Кузьминых", telegram_user_id=777,
                active=True,
            ))
            s.add(Counterparty(
                name="Tether",
                name_normalised="tether",
            ))
            s.flush()
            t = client.list_transcripts(limit=5)[0]
            pipeline.process_one(s, t)
            s.commit()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]

    assert captured_prompts, "transcribe_bytes was never called"
    final_prompt = captured_prompts[0]
    assert final_prompt is not None
    # Both name registries surfaced into the Whisper bias prompt.
    assert "Артем" in final_prompt
    assert "Tether" in final_prompt


# ============================================================
# FR-CR-05-128 — one-message overview guarantee + compact To-Do
# ============================================================


def test_short_summary_compact_todo_when_overview_overflows(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-128 follow-up — operator pinned «не надо всё
    вмещать в одно сообщение, если не вмещается, то след
    сообщение». Verbose To-Do is preserved; the splitter
    chunks into multiple Telegram DMs when overview exceeds
    4096 chars. Each chunk fits ≤4096; verbose description
    text survives across the chunks; all 22 tasks land
    somewhere in the delivered chunks."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "app.services.transcription.transcribe_bytes",
        lambda **kw: "x",
    )
    try:
        settings = _settings_with_audio_dir()
        client = _FakeFirefliesClient(transcripts=[_fake_transcript("trans-many")])

        # 22 tasks each with 250-char description — same shape
        # as the operator's fundraising sync that triggered the
        # split.
        rich_desc = (
            "очень подробное описание задачи с большим количеством "
            "контекста и деталей чтобы превысить лимит и проверить "
            "работу компактного fallback. " * 2
        )
        # FR-CR-05-128 — distinct topics + distinct titles so
        # the dedupe step doesn't collapse them.
        topics = [
            "Felix Capital", "Insight Partners", "Tether",
            "TPP", "NVIDIA", "Primavera", "Schaeffler",
            "Goldman Sachs", "QIA", "ADIA", "Bowerdart",
            "Sequoia", "Sentinel", "Trinity", "Lunate",
            "Capricorn", "Endeavor", "Mubadala", "Atinum",
            "MGX Fund", "Battery Ventures", "Gates Frontier",
        ]
        tasks = [
            {
                "title": f"Задача про {topics[i]}",
                "description": f"{topics[i]} - {rich_desc}",
                "owner": "777",
                "priority": "medium",
            }
            for i in range(22)
        ]
        llm = _FakeLLM(tasks=tasks)
        sender = _FakeSender()
        pipeline = FirefliesPipeline(
            settings=settings, client=client, llm_backend=llm,
            docs_factory=lambda: _FakeDocs(), sender=sender,
        )
        with SessionFactory() as s:
            s.add(TeamMember(real_name="Admin", telegram_user_id=777, active=True))
            s.flush()
            t = client.list_transcripts(limit=1)[0]
            pipeline.process_one(s, t)
            s.commit()

        with SessionFactory() as s:
            from app.models import MeetingRecording

            row = (
                s.query(MeetingRecording)
                .filter(MeetingRecording.fireflies_id == "trans-many")
                .one()
            )
            body = row.short_summary or ""

        # Verbose To-Do preserved — full descriptions land in
        # the body (chunked across multiple DMs if needed).
        assert rich_desc.strip()[:80] in body, (
            "verbose description was unexpectedly compacted"
        )
        # All 22 task descriptions present.
        for i in range(22):
            assert topics[i] in body, (
                f"missing topic {topics[i]!r} from verbose To-Do"
            )
        # Each delivered chunk to admin fits Telegram's 4096-cap.
        admin_msgs = [m for m in sender.sent if m["chat_id"] == 777]
        for m in admin_msgs:
            assert len(m["text"]) <= 4096, (
                f"chunk exceeds Telegram limit: {len(m['text'])} chars"
            )
        # Verbose tasks span multiple chunks (operator-accepted
        # split rather than compacting).
        tasks_in_chunks = sum(
            1 for m in admin_msgs
            if "Тема" in m["text"] or "Задача" in m["text"]
        )
        assert tasks_in_chunks >= 1, "no chunks carry the To-Do content"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_trace_event_writes_jsonl_per_recording(monkeypatch, tmp_path):
    """FR-CR-05-128 — operator-pinned: «мне под каждый вызов
    надо в трейсы складывать с датой и временем». Every key
    pipeline event appends one JSONL line to
    `<MEETING_TRACE_DIR>/<source>-<recording-id>.jsonl` with a
    UTC ISO timestamp + event name + structured fields, so the
    operator can `cat traces/zoom-XYZ.jsonl | jq` after a run
    to walk through every step / LLM call / Telegram send."""
    import importlib
    import json

    monkeypatch.setenv("MEETING_TRACE_DIR", str(tmp_path))
    # Force the trace-log module to re-resolve the dir on the
    # next call (its first-use guard caches the result).
    import app.services.trace_log as trace_log_mod
    importlib.reload(trace_log_mod)

    trace_log_mod.trace_event(
        source="zoom", recording_id="zm-test-001",
        event="step_started", step="transcribe", model="whisper-1",
    )
    trace_log_mod.trace_event(
        source="zoom", recording_id="zm-test-001",
        event="task_extraction_llm_returned",
        raw_count=3, raw_titles=["a", "b", "c"],
    )
    # Different recording → different file.
    trace_log_mod.trace_event(
        source="fireflies", recording_id="ff-other",
        event="step_done", step="download", duration_ms=500,
    )

    file_a = tmp_path / "zoom-zm-test-001.jsonl"
    assert file_a.exists()
    lines = [json.loads(l) for l in file_a.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["event"] == "step_started"
    assert lines[0]["fields"]["step"] == "transcribe"
    assert lines[1]["event"] == "task_extraction_llm_returned"
    assert lines[1]["fields"]["raw_count"] == 3
    # Timestamps are ISO-format UTC.
    from datetime import datetime
    for line in lines:
        ts = datetime.fromisoformat(line["ts"])
        assert ts.tzinfo is not None

    file_b = tmp_path / "fireflies-ff-other.jsonl"
    assert file_b.exists()
    lines_b = [json.loads(l) for l in file_b.read_text().splitlines()]
    assert lines_b[0]["event"] == "step_done"
    assert lines_b[0]["fields"]["duration_ms"] == 500


def test_trace_event_safe_against_zoom_id_slashes(monkeypatch, tmp_path):
    """Zoom UUIDs are base64 with `/` and `=` — sanitiser
    replaces them so we don't create unwanted subdirs."""
    import importlib
    monkeypatch.setenv("MEETING_TRACE_DIR", str(tmp_path))
    import app.services.trace_log as trace_log_mod
    importlib.reload(trace_log_mod)

    trace_log_mod.trace_event(
        source="zoom", recording_id="HdyK6m9iQtKabZ/FpT6bN1Q==",
        event="step_started", step="download",
    )
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert "/" not in files[0].name
    assert "=" not in files[0].name


def test_short_summary_keeps_verbose_todo_when_fits(
    patched_session_scope, SessionFactory, monkeypatch
):
    """FR-CR-05-128 — when the verbose body fits, keep verbose
    To-Do (FR-CR-05-120 contract: descriptions VERBATIM in
    «<topic> - <action with details>» format). Compact fallback
    is only for overflow."""
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_IDS", "777")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "app.services.transcription.transcribe_bytes",
        lambda **kw: "x",
    )
    try:
        settings = _settings_with_audio_dir()
        client = _FakeFirefliesClient(transcripts=[_fake_transcript("trans-few")])
        # 3 short tasks — fits comfortably under 4000 chars.
        tasks = [
            {"title": "T1", "description": "Тема1 - первое действие.",
             "owner": "777", "priority": "medium"},
            {"title": "T2", "description": "Тема2 - второе действие.",
             "owner": "777", "priority": "medium"},
            {"title": "T3", "description": "Тема3 - третье действие.",
             "owner": "777", "priority": "medium"},
        ]
        pipeline = FirefliesPipeline(
            settings=settings, client=client, llm_backend=_FakeLLM(tasks=tasks),
            docs_factory=lambda: _FakeDocs(), sender=_FakeSender(),
        )
        with SessionFactory() as s:
            s.add(TeamMember(real_name="Admin", telegram_user_id=777, active=True))
            s.flush()
            t = client.list_transcripts(limit=1)[0]
            pipeline.process_one(s, t)
            s.commit()

        with SessionFactory() as s:
            from app.models import MeetingRecording

            row = (
                s.query(MeetingRecording)
                .filter(MeetingRecording.fireflies_id == "trans-few")
                .one()
            )
            body = row.short_summary or ""

        # Verbose descriptions present (we're under the limit).
        assert "Тема1 - первое действие." in body
        assert "Тема2 - второе действие." in body
        assert "Тема3 - третье действие." in body
        assert len(body) <= 4096
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
