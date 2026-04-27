"""Requirement coverage: full message capture (every Slack event with
raw payload + audio transcripts).

Two stores:

  slack_events_archive — every event the bot received, BEFORE filters.
  slack_messages       — "real" messages with raw, transcript, has_audio.
"""
from __future__ import annotations

from unittest.mock import patch

from app.models import SlackEventArchive, SlackMessage


# --------------------------------------------------------------------------- #
# slack_events_archive: every event captured
# --------------------------------------------------------------------------- #


def test_archive_records_passive_message(
    patched_session_scope, services_task, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message

    handle_message(
        event={
            "type": "message",
            "ts": "100.0",
            "user": "U1",
            "text": "hello world",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Ev-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=__import__("tests.requirements.test_passive_draft_card", fromlist=["_Sender"])._Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        rows = s.query(SlackEventArchive).all()
        assert len(rows) == 1
        r = rows[0]
        assert r.event_id == "Ev-1"
        assert r.event_type == "message"
        assert r.conversation_id == "C1"
        assert r.user_id == "U1"
        assert r.text == "hello world"
        # raw is the full event dict.
        assert r.raw["channel"] == "C1"
        assert r.raw["channel_type"] == "channel"


def test_archive_captures_filtered_subtype_events(
    patched_session_scope, services_task, ack, bolt_context, slack_client, SessionFactory
):
    """message_changed / message_deleted / channel_join / bot_message
    events are still archived even though intent processing skips
    them."""
    from app.slack_bot.handlers.events import handle_message
    from tests.requirements.test_passive_draft_card import _Sender

    sender = _Sender()
    for subtype in ("message_changed", "message_deleted", "channel_join", "bot_message"):
        handle_message(
            event={
                "type": "message",
                "subtype": subtype,
                "ts": f"100.{subtype}",
                "user": "U1",
                "text": f"event {subtype}",
                "channel": "C1",
                "channel_type": "channel",
            },
            body={"event_id": f"Ev-{subtype}"},
            client=slack_client,
            context=bolt_context,
            services=services_task,
            sender=sender,
            ack=ack,
        )
    with SessionFactory() as s:
        rows = s.query(SlackEventArchive).all()
        # Every filtered event is archived.
        subtypes = sorted(r.subtype for r in rows if r.subtype)
        assert subtypes == [
            "bot_message",
            "channel_join",
            "message_changed",
            "message_deleted",
        ]
        # And no slack_messages row was created (intent layer ignored them).
        assert s.query(SlackMessage).count() == 0


def test_archive_records_mention_events(
    patched_session_scope, services_task, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_app_mention
    from tests.requirements.test_passive_draft_card import _Sender

    sender = _Sender()
    handle_app_mention(
        event={
            "type": "app_mention",
            "ts": "200.0",
            "user": "U1",
            "text": "<@UBOT> сделать отчёт",
            "channel": "C1",
            "channel_type": "channel",
        },
        body={"event_id": "Mention-1"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=sender,
        ack=ack,
    )
    with SessionFactory() as s:
        rows = s.query(SlackEventArchive).all()
        assert len(rows) == 1
        assert rows[0].event_type == "app_mention"
        assert rows[0].event_id == "Mention-1"


def test_archive_failure_does_not_abort_handler(
    patched_session_scope, services_task, ack, bolt_context, slack_client, SessionFactory
):
    """If session.add into the archive raises, the rest of handle_message
    must still run and produce a draft."""
    from app.slack_bot.handlers.events import handle_message
    from tests.requirements.test_passive_draft_card import _Sender

    real_archive = __import__(
        "app.slack_bot.handlers.shared", fromlist=["archive_event"]
    ).archive_event

    call_count = {"n": 0}

    def _flaky_archive(session, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("disk full")
        return real_archive(session, **kw)

    with patch(
        "app.slack_bot.handlers.events.archive_event",
        side_effect=_flaky_archive,
    ):
        handle_message(
            event={
                "type": "message",
                "ts": "300.0",
                "user": "U1",
                "text": "надо собрать отчёт",
                "channel": "C1",
                "channel_type": "channel",
            },
            body={"event_id": "Ev-flaky"},
            client=slack_client,
            context=bolt_context,
            services=services_task,
            sender=_Sender(),
            ack=ack,
        )
    # Even though the archive insert failed first, the handler completed
    # — slack_messages got the row.
    with SessionFactory() as s:
        assert s.query(SlackMessage).count() == 1


# --------------------------------------------------------------------------- #
# slack_messages: raw, transcript, has_audio
# --------------------------------------------------------------------------- #


def test_slack_messages_stores_raw_event_payload(
    patched_session_scope, services_task, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message
    from tests.requirements.test_passive_draft_card import _Sender

    handle_message(
        event={
            "type": "message",
            "ts": "400.0",
            "user": "U1",
            "text": "надо сделать отчёт",
            "channel": "C1",
            "channel_type": "channel",
            "client_msg_id": "abcd-1234",
            "blocks": [{"type": "rich_text"}],
        },
        body={"event_id": "Ev-raw"},
        client=slack_client,
        context=bolt_context,
        services=services_task,
        sender=_Sender(),
        ack=ack,
    )
    with SessionFactory() as s:
        msg = s.query(SlackMessage).one()
        # raw is the full original event including fields the intent
        # layer never reads (client_msg_id, blocks).
        assert msg.raw is not None
        assert msg.raw["client_msg_id"] == "abcd-1234"
        assert msg.raw["blocks"] == [{"type": "rich_text"}]
        # has_audio is False on a text-only message.
        assert msg.has_audio is False
        assert msg.transcript is None


def test_slack_messages_stores_audio_transcript_and_flag(
    patched_session_scope, services_task, ack, bolt_context, slack_client, SessionFactory
):
    from app.slack_bot.handlers.events import handle_message
    from tests.requirements.test_passive_draft_card import _Sender

    with patch(
        "app.services.transcription.transcribe_audio_files",
        return_value=["надо собрать отчёт"],
    ):
        handle_message(
            event={
                "type": "message",
                "ts": "500.0",
                "user": "U1",
                "text": "",
                "channel": "C1",
                "channel_type": "channel",
                "files": [
                    {
                        "id": "F1",
                        "mimetype": "audio/webm",
                        "url_private": "https://slack/F1",
                        "name": "voice.webm",
                    }
                ],
            },
            body={"event_id": "Ev-voice"},
            client=slack_client,
            context=bolt_context,
            services=services_task,
            sender=_Sender(),
            ack=ack,
        )
    with SessionFactory() as s:
        msg = s.query(SlackMessage).one()
        assert msg.has_audio is True
        assert msg.transcript == "надо собрать отчёт"
        # raw still carries the full files array for forensic use.
        assert msg.raw["files"][0]["mimetype"] == "audio/webm"
