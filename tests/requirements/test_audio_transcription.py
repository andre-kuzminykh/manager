"""Requirement coverage: FR-CR-04-8 (audio input via Whisper).

Slack audio attachments (voice notes, uploaded audio files) are
transcribed via OpenAI Whisper and fed into the normal intent pipeline
as the source text. Text captions, if present, are preserved and the
transcript is appended."""
from __future__ import annotations

from unittest.mock import patch

from app.services.transcription import (
    extract_audio_files,
    is_audio_file,
    merge_transcripts_into_text,
    transcribe_audio_files,
)


# --------------------------------------------------------------------------- #
# Unit: file filtering
# --------------------------------------------------------------------------- #


def test_is_audio_file_recognises_audio_mimetypes():
    assert is_audio_file({"mimetype": "audio/webm"})
    assert is_audio_file({"mimetype": "audio/mp4"})
    assert is_audio_file({"mimetype": "AUDIO/OGG"})
    assert not is_audio_file({"mimetype": "image/png"})
    assert not is_audio_file({"mimetype": "application/pdf"})
    assert not is_audio_file({})


def test_extract_audio_files_filters_only_audio():
    event = {
        "files": [
            {"mimetype": "image/png", "url_private": "u1"},
            {"mimetype": "audio/webm", "url_private": "u2"},
            {"mimetype": "audio/mp4", "url_private": "u3"},
            "not-a-dict",
        ]
    }
    audio = extract_audio_files(event)
    assert [f["url_private"] for f in audio] == ["u2", "u3"]


def test_extract_audio_files_none_returns_empty():
    assert extract_audio_files({}) == []
    assert extract_audio_files({"files": None}) == []


# --------------------------------------------------------------------------- #
# Unit: transcript merging into source text
# --------------------------------------------------------------------------- #


def test_merge_transcripts_preserves_caption_and_appends():
    assert merge_transcripts_into_text(
        "see attached", ["hello world"]
    ) == "see attached\nhello world"


def test_merge_transcripts_uses_transcript_when_no_caption():
    assert merge_transcripts_into_text("", ["hello"]) == "hello"
    assert merge_transcripts_into_text("   ", ["hello"]) == "hello"


def test_merge_transcripts_joins_multiple():
    assert merge_transcripts_into_text(
        "caption", ["first", "second"]
    ) == "caption\nfirst\nsecond"


def test_merge_transcripts_drops_empty_entries():
    assert merge_transcripts_into_text("cap", ["", "real", ""]) == "cap\nreal"


# --------------------------------------------------------------------------- #
# Unit: the two I/O helpers degrade to None on failure
# --------------------------------------------------------------------------- #


def test_transcribe_audio_files_skips_files_without_url():
    with patch(
        "app.services.transcription.download_slack_file", return_value=None
    ):
        out = transcribe_audio_files(
            [{"mimetype": "audio/webm"}],
            bot_token="xoxb",
            openai_api_key="sk-foo",
        )
    assert out == []


def test_transcribe_audio_files_returns_transcripts_in_order():
    files = [
        {"mimetype": "audio/webm", "url_private": "u1", "name": "a.webm"},
        {"mimetype": "audio/mp4", "url_private": "u2", "name": "b.m4a"},
    ]
    with patch(
        "app.services.transcription.download_slack_file", return_value=b"fake-bytes"
    ), patch(
        "app.services.transcription.transcribe_bytes",
        side_effect=["first transcript", "second transcript"],
    ):
        out = transcribe_audio_files(files, bot_token="x", openai_api_key="y")
    assert out == ["first transcript", "second transcript"]


# --------------------------------------------------------------------------- #
# download_slack_file — HTTP path
# --------------------------------------------------------------------------- #


def test_download_slack_file_returns_bytes_on_200():
    from app.services.transcription import download_slack_file
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.content = b"hello"
    resp.raise_for_status = MagicMock()
    with patch("app.services.transcription.httpx.get", return_value=resp) as m:
        out = download_slack_file(url="https://slack/f", bot_token="xoxb-1")
    assert out == b"hello"
    # Token header passed correctly.
    kwargs = m.call_args.kwargs
    assert kwargs["headers"] == {"Authorization": "Bearer xoxb-1"}
    assert kwargs["follow_redirects"] is True


def test_download_slack_file_returns_none_on_http_error():
    from app.services.transcription import download_slack_file

    with patch(
        "app.services.transcription.httpx.get",
        side_effect=RuntimeError("connection reset"),
    ):
        assert download_slack_file(url="https://slack/f", bot_token="x") is None


def test_download_slack_file_rejects_oversize():
    from app.services.transcription import download_slack_file
    from unittest.mock import MagicMock

    resp = MagicMock()
    # One byte over the 25 MB cap.
    resp.content = b"x" * (25 * 1024 * 1024 + 1)
    resp.raise_for_status = MagicMock()
    with patch("app.services.transcription.httpx.get", return_value=resp):
        assert download_slack_file(url="https://slack/f", bot_token="x") is None


# --------------------------------------------------------------------------- #
# transcribe_bytes — Whisper path
# --------------------------------------------------------------------------- #


def test_transcribe_bytes_happy_path():
    from app.services.transcription import transcribe_bytes
    from unittest.mock import MagicMock

    whisper_resp = MagicMock()
    whisper_resp.text = "hello world  "
    client = MagicMock()
    client.audio.transcriptions.create.return_value = whisper_resp
    with patch("openai.OpenAI", return_value=client):
        out = transcribe_bytes(
            audio_bytes=b"x", mimetype="audio/webm", filename="a.webm",
            openai_api_key="sk-test",
        )
    assert out == "hello world"


def test_transcribe_bytes_returns_none_without_key():
    from app.services.transcription import transcribe_bytes

    assert transcribe_bytes(
        audio_bytes=b"x", mimetype="audio/webm", filename="a.webm", openai_api_key=""
    ) is None


def test_transcribe_bytes_returns_none_on_empty_audio():
    from app.services.transcription import transcribe_bytes

    assert transcribe_bytes(
        audio_bytes=b"", mimetype="audio/webm", filename="a.webm", openai_api_key="sk"
    ) is None


def test_transcribe_bytes_returns_none_on_whisper_error():
    from app.services.transcription import transcribe_bytes

    client = MagicMock = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock
    client_inst = client()
    client_inst.audio.transcriptions.create.side_effect = RuntimeError("api down")
    with patch("openai.OpenAI", return_value=client_inst):
        out = transcribe_bytes(
            audio_bytes=b"x", mimetype="audio/webm", filename="a.webm",
            openai_api_key="sk",
        )
    assert out is None


def test_transcribe_bytes_returns_none_when_whisper_text_missing():
    from app.services.transcription import transcribe_bytes
    from unittest.mock import MagicMock

    whisper_resp = MagicMock(spec=[])  # no .text attribute
    client = MagicMock()
    client.audio.transcriptions.create.return_value = whisper_resp
    with patch("openai.OpenAI", return_value=client):
        out = transcribe_bytes(
            audio_bytes=b"x", mimetype="audio/webm", filename="a.webm",
            openai_api_key="sk",
        )
    assert out is None


def test_transcribe_bytes_accepts_dict_response_shape():
    from app.services.transcription import transcribe_bytes
    from unittest.mock import MagicMock

    # Some client variants return dict-like responses.
    client = MagicMock()
    client.audio.transcriptions.create.return_value = {"text": "hey"}
    with patch("openai.OpenAI", return_value=client):
        out = transcribe_bytes(
            audio_bytes=b"x", mimetype="audio/webm", filename="a.webm",
            openai_api_key="sk",
        )
    assert out == "hey"


# --------------------------------------------------------------------------- #
# Integration: handle_message wires audio → pipeline
# --------------------------------------------------------------------------- #


class _Sender:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    def post_message(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": f"{len(self.posts)}.0"}

    def update_message(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    def post_ephemeral(self, **kw):  # pragma: no cover
        return {"ok": True}


def test_passive_voice_only_message_is_transcribed_and_classified(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    """Voice-only Slack message (text is empty, one audio file) → the
    bot transcribes via Whisper, the pipeline runs on the transcript,
    a draft card appears in the thread."""
    from app.models import ActionDraft
    from app.slack_bot.handlers.events import handle_message

    sender = _Sender()
    with patch(
        "app.services.transcription.transcribe_audio_files",
        return_value=["надо подготовить заметки к 1 мая"],
    ):
        handle_message(
            event={
                "ts": "700.0",
                "user": "U-author",
                "text": "",
                "channel": "C1",
                "channel_type": "channel",
                "files": [
                    {
                        "id": "F1",
                        "mimetype": "audio/webm",
                        "url_private": "https://slack.com/files/F1",
                        "name": "voice.webm",
                    }
                ],
            },
            body={"event_id": "voice-1"},
            client=slack_client,
            context=bolt_context,
            services=services_task,
            sender=sender,
            ack=ack,
        )
    # A draft was produced from the transcript.
    with SessionFactory() as s:
        draft = s.query(ActionDraft).one()
        # The source text stored on the draft includes the transcript.
        # (ContextSnapshot carries the raw source_message; the draft
        # payload carries the LLM-extracted title which, with the stub
        # classifier, is "Prepare report".)
        assert draft.payload is not None
    # A draft card was posted in the source thread.
    assert any(p.get("thread_ts") == "700.0" for p in sender.posts)


def test_mention_with_voice_only_transcribes(
    patched_session_scope,
    services_task,
    ack,
    bolt_context,
    slack_client,
    SessionFactory,
):
    from app.models import Task
    from app.slack_bot.handlers.events import handle_app_mention

    sender = _Sender()
    with patch(
        "app.services.transcription.transcribe_audio_files",
        return_value=["надо сделать отчёт"],
    ):
        handle_app_mention(
            event={
                "ts": "800.0",
                "user": "U-author",
                "text": "<@UBOT>",
                "channel": "C1",
                "channel_type": "channel",
                "files": [
                    {
                        "mimetype": "audio/mp4",
                        "url_private": "https://slack.com/files/F2",
                        "name": "voice.m4a",
                    }
                ],
            },
            body={"event_id": "mention-voice-1"},
            client=slack_client,
            context=bolt_context,
            services=services_task,
            sender=sender,
            ack=ack,
        )
    # The task was auto-created via the mention path.
    with SessionFactory() as s:
        assert s.query(Task).count() == 1


# --------------------------------------------------------------------------- #
# FR-CR-05-146a — parallel Whisper chunks
# --------------------------------------------------------------------------- #


def test_transcribe_chunks_parallel_runs_concurrently_and_preserves_order(
    tmp_path,
):
    """FR-CR-05-146a — operator-pinned «Whisper-чанки параллельно».
    Each chunk goes to `transcribe_bytes` in a thread; all run
    concurrently. Order of results matches input order so the
    joined transcript stays chronological."""
    import threading
    import time
    from unittest.mock import patch

    from app.services.transcription import transcribe_chunks_parallel

    # Three chunk files.
    paths: list[str] = []
    for i in range(3):
        p = tmp_path / f"chunk{i}.m4a"
        p.write_bytes(b"FAKE-AUDIO-" + str(i).encode())
        paths.append(str(p))

    in_flight = 0
    max_in_flight = 0
    in_flight_lock = threading.Lock()

    def fake_transcribe(*, audio_bytes, filename, **_):
        nonlocal in_flight, max_in_flight
        with in_flight_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)  # let other threads pile in
        with in_flight_lock:
            in_flight -= 1
        # Return text uniquely identifying the chunk by its bytes.
        suffix = audio_bytes.decode().split("-")[-1]
        return f"transcript_for_chunk_{suffix}"

    with patch(
        "app.services.transcription.transcribe_bytes",
        side_effect=fake_transcribe,
    ):
        out = transcribe_chunks_parallel(
            paths,
            openai_api_key="sk-test",
            model="whisper-1",
            max_workers=3,
        )

    # All 3 chunks transcribed.
    assert len(out) == 3
    # Ordering preserved (chunk_0 first, chunk_2 last).
    assert out == [
        "transcript_for_chunk_0",
        "transcript_for_chunk_1",
        "transcript_for_chunk_2",
    ]
    # Concurrency observed — at least 2 in-flight at the peak.
    assert max_in_flight >= 2, (
        f"expected concurrent execution, max_in_flight={max_in_flight}"
    )


def test_transcribe_chunks_parallel_returns_none_on_no_api_key():
    from app.services.transcription import transcribe_chunks_parallel

    out = transcribe_chunks_parallel(
        ["/x/a.m4a"], openai_api_key="", model="whisper-1",
    )
    assert out == [None]


def test_transcribe_chunks_parallel_propagates_per_chunk_failure(tmp_path):
    """One chunk failing returns None for THAT slot; others
    still complete in order."""
    from unittest.mock import patch

    from app.services.transcription import transcribe_chunks_parallel

    paths: list[str] = []
    for i in range(3):
        p = tmp_path / f"chunk{i}.m4a"
        p.write_bytes(b"FAKE-" + str(i).encode())
        paths.append(str(p))

    def fake_transcribe(*, audio_bytes, **_):
        suffix = audio_bytes.decode().split("-")[-1]
        if suffix == "1":  # middle chunk fails
            return None
        return f"ok_{suffix}"

    with patch(
        "app.services.transcription.transcribe_bytes",
        side_effect=fake_transcribe,
    ):
        out = transcribe_chunks_parallel(
            paths, openai_api_key="sk-test", max_workers=3,
        )
    assert out == ["ok_0", None, "ok_2"]


# --------------------------------------------------------------------------- #
# FR-CR-05-146b — parallel counterparty resolve via batched LLM calls
# --------------------------------------------------------------------------- #


def test_resolve_mentions_to_directory_runs_batches_in_parallel():
    """FR-CR-05-146b — operator-pinned «match_counterparties —
    параллельно». 30 mentions, batch_size=10 → 3 LLM calls in
    parallel; results merged with ORIGINAL mention order
    preserved. Concurrent execution observed via lock-counter."""
    import threading
    import time

    from app.models import Counterparty
    from app.services.counterparty_match import (
        resolve_mentions_to_directory,
    )

    directory = [
        Counterparty(id=10 + i, name=f"Org{i}", name_normalised=f"org{i}")
        for i in range(5)
    ]
    mentions = [f"mention_{i:02d}" for i in range(30)]

    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    class _StubLLM:
        def complete_text(self, *, system_prompt, user_prompt, **kw):
            nonlocal in_flight, max_in_flight
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            # Map every input mention to directory id 10
            # (just enough so the function returns a non-empty
            # mapping for each).
            import json as _json
            import re

            ms = re.findall(r"\d+\.\s+(mention_\d+)", user_prompt)
            return _json.dumps({
                "matches": [
                    {"mention": m, "directory_id": 10} for m in ms
                ]
            })

    out = resolve_mentions_to_directory(
        mentions, directory,
        llm_backend=_StubLLM(),
        model="gpt-test",
        batch_size=10,
        max_workers=3,
    )
    # All 30 mentions resolved to id 10.
    assert len(out) == 30
    assert all(v == 10 for v in out.values())
    # 3 concurrent calls observed.
    assert max_in_flight >= 2


def test_resolve_mentions_to_directory_default_no_batching_one_call():
    """`batch_size=0` (default) → single LLM call, legacy
    behaviour. No threading."""
    from app.models import Counterparty
    from app.services.counterparty_match import (
        resolve_mentions_to_directory,
    )

    directory = [
        Counterparty(id=10, name="Org", name_normalised="org"),
    ]

    calls = 0

    class _StubLLM:
        def complete_text(self, **kw):
            nonlocal calls
            calls += 1
            import json as _json
            return _json.dumps({"matches": [
                {"mention": "m1", "directory_id": 10},
                {"mention": "m2", "directory_id": 10},
            ]})

    out = resolve_mentions_to_directory(
        ["m1", "m2"], directory,
        llm_backend=_StubLLM(), model="gpt-test",
    )
    assert calls == 1
    assert out == {"m1": 10, "m2": 10}
