"""Slack audio attachments (voice notes, uploaded audio files) are
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
