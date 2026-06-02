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


# --------------------------------------------------------------------------- #
# FR-CR-05-148 — Whisper hallucination detection + Zoom VTT fallback
# --------------------------------------------------------------------------- #


def test_looks_like_whisper_hallucination_flags_subtitle_credit_loop():
    """FR-CR-05-148 — operator regression on «Ирина - статус
    по задачам»: Whisper returned 2962 chars of «Редактор
    субтитров А.Семкин Корректор А.Егорова» repeating instead
    of actual speech. Detector must flag this so VTT fallback
    fires."""
    from app.services.transcription import (
        looks_like_whisper_hallucination,
    )

    operator_actual_output = (
        "Редактор субтитров А.Семкин Корректор А.Егорова "
        "Редактор субтитров Н.Александрова Корректор А.Кулакова "
    ) * 50  # ~2900 chars, mimics the production failure
    assert looks_like_whisper_hallucination(operator_actual_output) is True


def test_looks_like_whisper_hallucination_passes_real_transcript():
    """A normal meeting transcript with diverse vocabulary
    must NOT be flagged."""
    from app.services.transcription import (
        looks_like_whisper_hallucination,
    )

    real_transcript = (
        "Артем сказал что нужно продолжить переговоры с Шефлером "
        "и согласовать формулировки base contract value. Алина "
        "уточнила формат investor update и обсудила email-рассылку "
        "по Sanders Capital. Дмитрий Седов отметил риски варантов "
        "и предложил обсуждать только на звонках. Ирина зафиксировала "
        "follow-up по Insight Partners и Bauerdort. " * 10
    )
    assert looks_like_whisper_hallucination(real_transcript) is False


def test_looks_like_whisper_hallucination_short_text_returns_false():
    """Short transcripts (e.g. operator forgot to record) are
    NOT hallucinations — they're empty meetings. Don't trigger
    fallback for them."""
    from app.services.transcription import (
        looks_like_whisper_hallucination,
    )

    assert looks_like_whisper_hallucination("") is False
    assert looks_like_whisper_hallucination("Spasibo.") is False
    assert looks_like_whisper_hallucination("Корректор А.Егорова") is False


def test_looks_like_whisper_hallucination_flags_low_unique_word_ratio():
    """Even without subtitle markers, a transcript that
    repeats the same 4-word phrase 200x is a Whisper loop."""
    from app.services.transcription import (
        looks_like_whisper_hallucination,
    )

    looped = "встреча прошла продуктивно ничего нового " * 200
    assert looks_like_whisper_hallucination(looped) is True


# --------------------------------------------------------------------------- #
# FR-CR-05-157 follow-up — roster-only (silent meeting) transcripts
# --------------------------------------------------------------------------- #
def test_unsummarizable_flags_roster_only_silent_meeting():
    """Operator regression on «PR Status» 2026-06-02: a silent meeting
    (no real audio) clears the 800-char floor because the only surviving
    text is the participant roster inside bilingual-restoration scaffolding.
    Nothing to summarize → must be flagged unsummarizable."""
    from app.services.transcription import is_transcript_unsummarizable

    # The exact production shape: PRIMARY wrapper + <<< … >>> + name list,
    # padded to the real 831-char length so it CLEARS the < 800 floor and
    # exercises the roster path (not the length short-circuit).
    roster = (
        "PRIMARY (Russian-biased pass):\n<<<\n"
        "Humanoid, PR Status, Шлюгер Сергей, Эльмира Ларионова, Suzanne Ley, "
        "Катя Шетинина, Aleksandra Efremova, Olga Ponomarenko, Дмитрий Седов, "
        "Кристиан, CEO_office1 bot, Alexander Egorov, Валентина, "
        "Андрей Кузьминых, Саша Васильев, Антон Новосельцев, Миронова Люба, "
        "Aray, Оля Головина, Pavel Lebedev, Kristina, Thomas Shepherd, "
        "Alina Kolpakova, Anastasia M, George Machitidze, Alexander Grishin, "
        "Юля, Daniella Shabarina, Елена, Viktor, Ekaterina Selezneva, Лиля, "
        "Fedor Pavlovich, Давид, Игорь, Valeriya Tarasova, Радионова Елена, "
        "Maksim Maksim, Жойкина Наталья, Luiza, Ekaterina Chia, LinkedIn bot, "
        "Валерия, Vlad Gaon, Daniel Minkowitz, Julia Mus, Дима Дроздов, "
        "Genia Xasis, Irina Shipilova, Maria Maria, Jarad Cannon, "
        "Sotirios Stasinopoulos, Jochen Ruda, Boris Yangel\n>>>"
    )
    assert len(roster.strip()) >= 800  # clears the crude length floor
    flagged, reason = is_transcript_unsummarizable(roster)
    assert flagged is True
    assert "roster" in (reason or "")


def test_unsummarizable_passes_real_discussion_over_800():
    """A real ≥800-char discussion (sentences, punctuation) must NOT be
    mistaken for a roster."""
    from app.services.transcription import is_transcript_unsummarizable

    real = (
        "Артем: обсудили статус PR по запуску, договорились о пресс-релизе. "
        "Ольга предложила перенести анонс на следующую неделю. "
        "Решили согласовать формулировки с юристами и вернуться в среду. "
    ) * 5
    assert len(real) >= 800
    flagged, _ = is_transcript_unsummarizable(real)
    assert flagged is False


def test_unsummarizable_passes_comma_heavy_prose():
    """A comma-heavy but genuine sentence (with terminators) is NOT a
    roster — guards against false positives on enumerations in prose."""
    from app.services.transcription import is_transcript_unsummarizable

    prose = (
        "Обсудили инвесторов: Tether, Schaeffler, Object First и KeyOne, "
        "и решили, что Алина подготовит messaging, Дмитрий пнёт юристов, "
        "а Ирина зафиксирует follow-up по каждому из них к среде. " * 6
    )
    assert len(prose) >= 800
    flagged, _ = is_transcript_unsummarizable(prose)
    assert flagged is False


def test_parse_vtt_to_plain_text_strips_timing_and_cue_ids():
    """FR-CR-05-148 — Zoom VTT format: WEBVTT header, optional
    cue-id (numeric), timing line `00:00:00.000 --> ...`, then
    cue text. Parser drops everything except cue text and
    joins lines with `\\n`."""
    from app.zoom.client import _parse_vtt_to_plain_text

    vtt = (
        "WEBVTT\n"
        "\n"
        "1\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Привет, начинаем встречу.\n"
        "\n"
        "2\n"
        "00:00:05.500 --> 00:00:12.000\n"
        "Артем: давайте обсудим Schaeffler.\n"
        "\n"
        "3\n"
        "00:00:12.500 --> 00:00:18.000\n"
        "Алина: подготовлю письмо до пятницы.\n"
    )
    out = _parse_vtt_to_plain_text(vtt)
    assert "Привет, начинаем встречу." in out
    assert "Артем: давайте обсудим Schaeffler." in out
    assert "Алина: подготовлю письмо до пятницы." in out
    assert "WEBVTT" not in out
    assert "00:00:" not in out
    # Cue ids (1, 2, 3) on their own lines stripped.
    lines = out.split("\n")
    assert "1" not in lines and "2" not in lines and "3" not in lines


def test_parse_vtt_to_plain_text_handles_no_cue_ids():
    """VTT without numeric cue-ids — Zoom often omits them."""
    from app.zoom.client import _parse_vtt_to_plain_text

    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Hello world.\n"
        "\n"
        "00:00:05.500 --> 00:00:10.000\n"
        "Second line.\n"
    )
    out = _parse_vtt_to_plain_text(vtt)
    assert out == "Hello world.\nSecond line."


def test_parse_vtt_to_plain_text_empty_input():
    from app.zoom.client import _parse_vtt_to_plain_text

    assert _parse_vtt_to_plain_text("") == ""
    assert _parse_vtt_to_plain_text("WEBVTT\n\n") == ""


def test_looks_like_whisper_hallucination_flags_youtube_vk_loop():
    """FR-CR-05-153 — operator regression on Fundraising daily
    05/05: Whisper produced 1795 chars of «Девушкиной науке
    Университет candy samurai Университет voice ... Университет
    youtube Университет https://vk.com.ua» on a 26-min recording
    of normal speech. Different from the May 4 «Редактор
    субтитров» loop (different vocabulary), so the original
    detector missed it. Three new signals catch it:

      - URL/social markers («youtube», «vk.com», «https://vk.»)
      - Lower unique-word ratio threshold (5% → 8%)
      - Bigram-loop detector (a 2-word phrase repeating ≥20×)
    """
    from app.services.transcription import (
        looks_like_whisper_hallucination,
    )

    operator_actual_output = (
        "Девушкиной науке Университет candy samurai Университет "
        "voice, промышленная сфера Университет hablva, "
        "социальный обзор Университет сogle Университет "
        "https://vk.com.ua Университет https://vk.com.ua "
        + ("Университет youtube " * 100)
    )
    assert looks_like_whisper_hallucination(operator_actual_output) is True


def test_looks_like_whisper_hallucination_flags_pure_bigram_loop():
    """Pure bigram loop with 0 markers — caught by the new
    bigram-counter signal alone."""
    from app.services.transcription import (
        looks_like_whisper_hallucination,
    )

    text = "слово фраза " * 100
    assert looks_like_whisper_hallucination(text) is True


def test_looks_like_whisper_hallucination_real_transcript_with_some_repetition():
    """Real meeting transcripts have natural repetition (filler
    words, common phrases). Must NOT be flagged as
    hallucination. Pinning a realistic Russian-meeting
    transcript with diverse vocabulary."""
    from app.services.transcription import (
        looks_like_whisper_hallucination,
    )

    # ~600 char realistic snippet from a fundraising meeting.
    real = (
        "Артем сказал что нужно подготовить рассылку по Schaeffler. "
        "Алина уточнила тему письма и формат. Дмитрий Седов добавил "
        "что варанты обсуждаем только на звонках. Ирина зафиксировала "
        "follow-up по Insight Partners. По Sanders Capital решили "
        "включить в общий апдейт. Bauerdort пригласить на кофе "
        "и демо робота. Прайм Муверс пересчитать вклад при "
        "разных размерах раунда. К сожалению Tencent в private side "
        "пока не активно. Фонд QIA попросить интро через "
        "существующих контактов." * 3
    )
    assert looks_like_whisper_hallucination(real) is False
