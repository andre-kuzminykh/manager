"""FR-CR-05-116 — Zoom cloud-recording pipeline.

Mirror of `app/fireflies/pipeline.py` for Zoom. Reuses the
shared helpers (`_split_audio_into_chunks`, `_truncate`,
`_render_known_employees_table`, `_admin_fallback_owner_id`)
from the Fireflies module so the two pipelines stay
behaviour-identical.

Steps (same as Fireflies, FR-CR-05-39 / -56 / -115):

  1. Upsert `ZoomRecording` row keyed by Zoom UUID.
  2. Download audio via `ZoomClient.download_audio` (bearer-
     auth'd `download_url` from the recording-files list).
  3. Whisper transcribe — chunked at ≤24 MB via shared
     ffmpeg helper when needed.
  4. Detailed RU summary via `complete_text` + the same
     `MEETING_SUMMARY_PROMPT` Fireflies uses.
  5. Google Doc export with anyone-with-link writer.
  6. Short summary DM'd to admins + Tasks extracted via
     gpt-5.5 (same prompt, source_kind=zoom).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.fireflies.pipeline import (
    _admin_fallback_owner_id,
    _render_known_employees_table,
    _split_audio_into_chunks,
    _truncate,
)
from app.fireflies.prompts import (
    DETAILED_SUMMARY_SYSTEM as MEETING_SUMMARY_PROMPT,
    SHORT_SUMMARY_SYSTEM as MEETING_SHORT_SUMMARY_PROMPT,
    TASK_EXTRACTION_SYSTEM as MEETING_TASKS_PROMPT,
)
from app.logging_setup import get_logger
from app.models import Task, TaskSourceKind, ZoomRecording
from app.models.task import TaskPriority, TaskStatus
from app.zoom.client import ZoomClient, ZoomRecordingMeta

log = get_logger(__name__)


@dataclass
class ZoomPipelineReport:
    recording_id: int | None
    zoom_id: str
    title: str | None
    transcript_chars: int = 0
    detailed_chars: int = 0
    short_chars: int = 0
    google_doc_url: str | None = None
    tasks_created: int = 0
    short_summary_recipients: int = 0
    skipped_reason: str | None = None
    errors: list[str] = None  # type: ignore[assignment]


class ZoomPipeline:
    """Zoom counterpart of `FirefliesPipeline`.

    Constructed once at process startup; ``process_one`` is the
    main entry-point and is safe to call repeatedly on the same
    recording (each step short-circuits when its flag is set).
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: ZoomClient,
        llm_backend: Any,
        docs_factory=None,
        sender=None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._llm = llm_backend
        self._docs_factory = docs_factory
        self._sender = sender

    # --- step 0: upsert the row -------------------------------

    def _upsert_recording(
        self, session: Session, m: ZoomRecordingMeta
    ) -> ZoomRecording:
        row = (
            session.query(ZoomRecording)
            .filter(ZoomRecording.zoom_id == m.id)
            .first()
        )
        if row is None:
            row = ZoomRecording(
                zoom_id=m.id,
                zoom_meeting_id=m.meeting_id,
                title=m.title,
                meeting_date=m.meeting_date,
                duration_seconds=m.duration_seconds,
                participants=list(m.participants),
                audio_url=m.audio_url,
                zoom_share_url=m.share_url,
            )
            session.add(row)
            session.flush()
        else:
            if m.title and not row.title:
                row.title = m.title
            if m.meeting_date and not row.meeting_date:
                row.meeting_date = m.meeting_date
            if m.audio_url and not row.audio_url:
                row.audio_url = m.audio_url
            if m.share_url and not row.zoom_share_url:
                row.zoom_share_url = m.share_url
        return row

    # --- step 1: download audio -------------------------------

    def _step_download_audio(self, row: ZoomRecording) -> bool:
        if (
            row.audio_downloaded
            and row.audio_path
            and os.path.exists(row.audio_path)
        ):
            return True
        if not row.audio_url:
            row.last_error = "no audio_url on Zoom record"
            return False
        # FR-CR-05-117 — Zoom UUIDs are base64 with `/` and `=`,
        # which create unwanted subdirs / weird filenames when
        # used directly. Sanitize for the on-disk path.
        safe_id = (row.zoom_id or "").replace("/", "_").replace("=", "")
        # Provisional .bin extension; we sniff magic bytes after
        # download and rename to the real container so Whisper +
        # ffmpeg get the right hint. Zoom's URL never contains
        # the file extension, so we can't decide it upfront.
        dest = os.path.join(
            self._settings.zoom_audio_dir, f"{safe_id}.bin"
        )
        size = self._client.download_audio(
            url=row.audio_url,
            dest_path=dest,
            max_bytes=self._settings.zoom_audio_max_bytes,
        )
        if size is None:
            row.last_error = "audio download failed or exceeded cap"
            return False
        # Detect the real container from magic bytes and rename.
        from app.fireflies.pipeline import _sniff_audio_extension

        ext = _sniff_audio_extension(dest) or "mp4"
        final = os.path.join(
            self._settings.zoom_audio_dir, f"{safe_id}.{ext}"
        )
        if final != dest:
            os.replace(dest, final)
        row.audio_path = final
        row.audio_downloaded = True
        row.last_error = None
        return True

    # --- step 2: Whisper transcribe (with FR-CR-05-115 chunking)

    def _step_transcribe(
        self, row: ZoomRecording, session: Session | None = None
    ) -> bool:
        if row.transcribed and row.transcript_text:
            return True
        if not row.audio_path or not os.path.exists(row.audio_path):
            row.last_error = "audio_path missing for transcription"
            return False
        api_key = self._settings.openai_api_key
        if not api_key:
            row.last_error = "OPENAI_API_KEY not set"
            return False
        from app.services.transcription import (
            build_whisper_bias_prompt, transcribe_bytes,
            transcribe_chunks_parallel,
        )

        # FR-CR-05-153 reverted (operator-pinned «мне нужна
        # именно Whisper транскибация всегда»): Whisper FIRST
        # with bias-prompt for proper-noun fidelity, VTT kept
        # as fallback ONLY when Whisper hallucinates (FR-CR-05-148
        # behaviour). Detector improvements from FR-CR-05-153
        # (bigram-loop, URL/social markers) preserved — they
        # catch the «Университет youtube» class of hallucinations
        # that the original detector missed.

        # FR-CR-05-127 — bias Whisper toward the operator's
        # canonical name registries (counterparties + team) so
        # brand names don't mutate in transcription.
        try:
            whisper_prompt = build_whisper_bias_prompt(
                session,
                meeting_title=row.title,
                participants=getattr(row, "participants", None),
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "zoom_whisper_bias_prompt_failed",
                zoom_id=row.zoom_id, error=str(e),
            )
            whisper_prompt = None
        if whisper_prompt:
            from app.services.trace_log import trace_event as _zte0
            log.info(
                "zoom_whisper_bias_prompt_built",
                zoom_id=row.zoom_id,
                prompt_chars=len(whisper_prompt),
            )
            _zte0(source="zoom", recording_id=row.zoom_id,
                  event="whisper_bias_prompt_built",
                  prompt_chars=len(whisper_prompt),
                  prompt_preview=whisper_prompt[:240])

        size = os.path.getsize(row.audio_path)
        whisper_max = 24 * 1024 * 1024
        if size <= whisper_max:
            audio_paths = [row.audio_path]
        else:
            try:
                audio_paths = _split_audio_into_chunks(
                    row.audio_path, max_bytes=whisper_max
                )
            except Exception as e:  # noqa: BLE001
                row.last_error = f"audio chunking failed: {e}"
                return False
            log.info(
                "zoom_audio_chunked_for_whisper",
                zoom_id=row.zoom_id,
                size=size,
                chunks=len(audio_paths),
            )
        # FR-CR-05-146a — parallel Whisper across all chunks
        # (was sequential — operator-pinned «Whisper-чанки
        # параллельно»). Order preserved by `pool.map`, so the
        # joined transcript is still chronological.
        def _mt(path: str) -> str:
            return (
                "audio/mp4" if path.lower().endswith(".m4a") else
                ("video/mp4" if path.lower().endswith(".mp4")
                 else "audio/mpeg")
            )

        transcript_parts = transcribe_chunks_parallel(
            audio_paths,
            openai_api_key=api_key,
            model=self._settings.fireflies_whisper_model,
            prompt=whisper_prompt,
            mimetype_for=_mt,
            max_workers=3,
        )
        whisper_failed = any(t is None or not (t or "").strip()
                             for t in transcript_parts)
        transcript = "\n".join(t or "" for t in transcript_parts).strip()

        # FR-CR-05-148 — Zoom VTT fallback when Whisper either
        # returns empty OR hallucinates (loops on Russian
        # subtitle-credit phrases). Operator regression on
        # «Ирина - статус по задачам»: Whisper produced 2962
        # chars of «Редактор субтитров А.Семкин» repeating;
        # Zoom's own VTT had the actual speech.
        from app.services.transcription import (
            looks_like_whisper_hallucination,
        )

        is_hallucinated = looks_like_whisper_hallucination(transcript)
        if whisper_failed or is_hallucinated:
            log.info(
                "zoom_whisper_fallback_to_vtt",
                zoom_id=row.zoom_id,
                whisper_failed=whisper_failed,
                hallucinated=is_hallucinated,
                whisper_chars=len(transcript),
            )
            from app.services.trace_log import trace_event as _zte_h
            _zte_h(
                source="zoom", recording_id=row.zoom_id,
                event="zoom_whisper_fallback_to_vtt",
                whisper_failed=whisper_failed,
                hallucinated=is_hallucinated,
                whisper_chars=len(transcript),
                whisper_preview=transcript[:300],
            )
            vtt_url = self._find_vtt_download_url(row)
            if vtt_url:
                vtt_text = self._client.fetch_vtt_transcript(vtt_url)
                if vtt_text and len(vtt_text) > 100:
                    log.info(
                        "zoom_vtt_transcript_used",
                        zoom_id=row.zoom_id,
                        vtt_chars=len(vtt_text),
                    )
                    _zte_h(
                        source="zoom", recording_id=row.zoom_id,
                        event="zoom_vtt_transcript_used",
                        vtt_chars=len(vtt_text),
                    )
                    transcript = vtt_text
                    is_hallucinated = False
                    whisper_failed = False
                else:
                    log.warning(
                        "zoom_vtt_transcript_empty",
                        zoom_id=row.zoom_id, vtt_url=vtt_url[:120],
                    )
            else:
                log.warning(
                    "zoom_vtt_url_not_found",
                    zoom_id=row.zoom_id,
                )

        if whisper_failed and not transcript:
            row.last_error = "transcribe failed: Whisper empty + no VTT fallback"
            return False
        if not transcript:
            row.last_error = "transcribe returned empty"
            return False
        if len(audio_paths) > 1:
            for p in audio_paths:
                if p != row.audio_path:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        row.transcript_text = transcript
        row.transcribed = True
        row.last_error = None
        return True

    def _find_vtt_download_url(self, row: ZoomRecording) -> str | None:
        """FR-CR-05-148 — find the Zoom-side VTT transcript file
        URL for this recording. We re-list and match on uuid
        (cheap — one HTTP call) instead of re-querying the
        single-recording endpoint."""
        try:
            metas = self._client.list_recordings(
                limit=50, page_size=100,
            )
        except Exception:  # noqa: BLE001
            return None
        for m in metas:
            if m.id != row.zoom_id:
                continue
            files = (m.raw or {}).get("recording_files") or []
            for rf in files:
                if not isinstance(rf, dict):
                    continue
                rt = (rf.get("recording_type") or "").lower()
                ext = (rf.get("file_extension") or "").upper()
                if rt == "audio_transcript" and ext == "VTT":
                    return rf.get("download_url")
        return None

    # --- step 3: detailed summary -----------------------------

    def _step_detailed_summary(
        self, row: ZoomRecording, *, session: Session | None = None,
    ) -> bool:
        if row.detailed_summarised and row.detailed_summary:
            return True
        if not row.transcript_text:
            row.last_error = "no transcript for detailed summary"
            return False
        # FR-CR-05-146c — kick off participants extraction in
        # parallel with the detailed_summary LLM call. Both read
        # only `transcript_text` and don't depend on each other.
        # `_ensure_team_participants` (called later from
        # `_step_extract_tasks`) joins the future and applies
        # the post-filter.
        if session is not None:
            self._kickoff_team_participants_async(row, session)
        meta_lines = [
            f"Заголовок: {row.title or '(без названия)'}",
            f"Дата: {row.meeting_date.isoformat() if row.meeting_date else '—'}",
            (
                f"Продолжительность: {row.duration_seconds // 60} мин"
                if row.duration_seconds
                else "Продолжительность: —"
            ),
        ]
        if row.participants:
            meta_lines.append("Участники: " + ", ".join(row.participants))
        user_prompt = (
            "\n".join(meta_lines) + "\n\nТранскрипт:\n" + row.transcript_text
        )
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=MEETING_SUMMARY_PROMPT,
                user_prompt=user_prompt,
                model=self._settings.fireflies_summary_model,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"detailed summary failed: {e}"
            return False
        if not text:
            row.last_error = "detailed summary returned empty"
            return False
        from app.fireflies.pipeline import _strip_markdown_emphasis

        row.detailed_summary = _strip_markdown_emphasis(text)
        row.detailed_summarised = True
        row.last_error = None
        return True

    # --- step 4: Google Doc export ----------------------------

    def _step_doc_export(
        self, session: Session, row: ZoomRecording
    ) -> bool:
        if row.doc_exported and row.google_doc_url:
            return True
        if not row.detailed_summary:
            row.last_error = "no detailed summary to export"
            return False
        if self._docs_factory is None:
            row.last_error = "Docs factory not configured"
            return False
        try:
            docs = self._docs_factory()
        except Exception as e:  # noqa: BLE001
            row.last_error = f"Docs factory failed: {e}"
            return False
        if docs is None:
            row.last_error = "Docs credentials unavailable"
            return False
        title = row.title or f"Zoom meeting {row.zoom_id}"
        # FR-CR-05-119 follow-up — append the full task list to
        # the doc body. FR-CR-05-125 — also append the matched
        # counterparties block. Pipeline order is detailed →
        # match → tasks → doc so both already exist by here.
        from app.fireflies.pipeline import (
            _build_counterparties_section_for_doc,
            _build_full_tasks_section_for_doc,
        )

        body = row.detailed_summary
        cp_section = _build_counterparties_section_for_doc(
            session, source_kind="zoom", source_id=row.zoom_id,
        )
        if cp_section:
            body = body.rstrip() + "\n\n" + cp_section
        tasks_section = _build_full_tasks_section_for_doc(
            session,
            source_kind=TaskSourceKind.zoom,
            source_conversation_id=row.zoom_id,
        )
        if tasks_section:
            body = body.rstrip() + "\n\n" + tasks_section
        try:
            doc_id, url = docs.export_summary(
                title=title,
                body=body,
                parent_folder_id=(
                    self._settings.zoom_docs_folder_id
                    or self._settings.fireflies_docs_folder_id
                ),
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"Docs export failed: {e}"
            return False
        row.google_doc_id = doc_id
        row.google_doc_url = url
        row.doc_exported = True
        row.last_error = None
        return True

    # --- step 5: short summary + Telegram -----------------------

    def _step_short_summary(
        self, session: Session, row: ZoomRecording
    ) -> bool:
        if row.short_summary_sent and row.short_summary:
            return True
        if not row.detailed_summary:
            row.last_error = "no detailed summary for short summary"
            return False
        # FR-CR-05-130 / FR-CR-05-139 — same helper as
        # _step_extract_tasks; stash on row to avoid double LLM
        # call. LLM reads the transcript + team_members and
        # emits the team-side real-name participants. Falls
        # back to row.participants (Zoom API metadata) if the
        # LLM call fails or no team
        # members are configured.
        # FR-CR-05-139 — pull team-validated participants via the
        # cached helper; reused by extract_tasks above so the
        # extraction LLM call only fires once per recording.
        from app.services.team_members import as_known_employees
        try:
            tm_rows = as_known_employees(session, prefer_telegram=True)
        except Exception:  # noqa: BLE001
            tm_rows = []
        team_participants = self._ensure_team_participants(row, tm_rows)
        effective_participants = team_participants or list(row.participants or [])
        participants_block = "\n".join(
            f"  - {p}" for p in effective_participants if p
        ) or "  (нет данных)"
        meta_line = (
            f"meeting_title: {row.title or ''}\n"
            f"meeting_date: {row.meeting_date.isoformat() if row.meeting_date else ''}\n"
            f"duration_min: {row.duration_seconds // 60 if row.duration_seconds else ''}\n"
            f"google_doc_url: {row.google_doc_url or ''}\n"
            f"\nparticipants:\n{participants_block}\n\n"
        )
        body_in = meta_line + "Подробный отчёт:\n" + row.detailed_summary
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=MEETING_SHORT_SUMMARY_PROMPT,
                user_prompt=body_in,
                model=self._settings.fireflies_short_summary_model,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            row.last_error = f"short summary failed: {e}"
            return False
        text = _truncate(text, limit=3800)
        if not text:
            row.last_error = "short summary returned empty"
            return False
        # FR-CR-05-119 — drop any LLM-emitted To-Do section so we
        # can append the deterministic one. Also strip markdown.
        from app.fireflies.pipeline import (
            _build_todo_section,
            _strip_llm_todo_block,
        )

        text = _strip_llm_todo_block(text)
        # FR-CR-05-128 follow-up — verbose To-Do always; if
        # overview overflows, splitter chunks into multiple
        # Telegram DMs (operator-pinned).
        todo = _build_todo_section(
            session,
            source_kind=TaskSourceKind.zoom,
            source_conversation_id=row.zoom_id,
        )
        if todo:
            text = text.rstrip() + "\n\n" + todo
        # FR-CR-05-129 follow-up — drop «🔗 Контрагенты»
        # row (operator-pinned).
        # FR-CR-05-127 — title becomes an HTML hyperlink to the
        # Google Doc; the «📄 Подробный отчёт: <url>» trailer is
        # gone (replaced by the wrap on the first line). Sent
        # with parse_mode=HTML.
        # FR-CR-05-156 — first line MUST be the raw meeting title
        # (operator: «такие же названия тайтлов как в самих встречах»),
        # prefixed with the `DD/MM` of the meeting date.
        from app.fireflies.pipeline import (
            _force_meeting_title_first_line,
        )
        text = _force_meeting_title_first_line(
            text, row.title or "", row.meeting_date,
        )
        if row.google_doc_url:
            from app.fireflies.pipeline import (
                _wrap_short_summary_with_doc_link,
            )
            text = _wrap_short_summary_with_doc_link(
                text.rstrip(), row.google_doc_url,
            )
        row.short_summary = text
        row.last_error = None
        if self._sender is not None:
            self._send_short_summary(row)
        else:
            row.short_summary_sent = True
        return True

    def _send_short_summary(self, row: ZoomRecording) -> int:
        """DM the short summary to every admin uid. FR-CR-05-119:
        the deterministic To-Do block can push the body past
        Telegram's 4096-char per-message limit, so we split into
        chunks at paragraph boundaries and send each as a
        separate DM. Returns the number of admins who received
        the FULL set of chunks."""
        from app.fireflies.pipeline import _split_for_telegram
        from app.telegram_bot.handlers import admin_user_ids

        chunks = _split_for_telegram(row.short_summary or "", limit=4096)
        if not chunks:
            return 0
        sent = 0
        for uid in sorted(admin_user_ids()):
            if not uid.lstrip("-").isdigit():
                continue
            uid_chunks = 0
            for chunk in chunks:
                try:
                    resp = self._sender.send_message(
                        chat_id=int(uid), text=chunk,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "zoom_short_summary_dm_failed",
                        uid=uid, error=str(e),
                    )
                    break
                if (resp or {}).get("message_id"):
                    uid_chunks += 1
                else:
                    break
            if uid_chunks == len(chunks):
                sent += 1
        row.short_summary_sent = sent > 0
        # FR-CR-05-137 — mirror the same body into Slack (Artem
        # AI's bot DM by default). Failures here MUST NOT block
        # the rest of the pipeline. Configured via
        # SLACK_MEETING_CHANNEL_ID + SLACK_BOT_TOKEN env.
        try:
            from app.services.slack_mirror import (
                post_meeting_summary_to_slack,
            )
            channel = self._settings.slack_meeting_channel_id
            token = self._settings.slack_bot_token
            if channel and token and (row.short_summary or "").strip():
                # FR-CR-05-141 — multi-chunk delivery; returns
                # list of responses, one per ≤35K-char chunk.
                resps = post_meeting_summary_to_slack(
                    slack_token=token, channel_id=channel,
                    body=row.short_summary or "",
                )
                posted = sum(
                    1 for r in (resps or [])
                    if isinstance(r, dict) and r.get("ok")
                )
                from app.services.trace_log import trace_event as _te
                _te(source="zoom", recording_id=row.zoom_id,
                    event="zoom_slack_mirror_posted",
                    channel_id=channel,
                    chunks_posted=posted,
                    body_chars=len(row.short_summary or ""))
                log.info(
                    "zoom_slack_mirror_posted",
                    zoom_id=row.zoom_id, channel_id=channel,
                    chunks_posted=posted,
                    body_chars=len(row.short_summary or ""),
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "zoom_slack_mirror_unexpected_error",
                zoom_id=row.zoom_id, error=str(e),
            )
        return sent

    def _step_match_counterparties(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-125 / FR-CR-05-129 — TWO-PASS canonical
        counterparty resolution (symmetric with Fireflies)."""
        from app.models import Counterparty, CounterpartyMention
        from app.services.counterparty_match import (
            extract_counterparty_mentions,
            resolve_mentions_to_directory,
        )

        if not row.transcript_text:
            return 0
        directory = (
            session.query(Counterparty)
            .order_by(Counterparty.name)
            .all()
        )
        if not directory:
            log.info(
                "zoom_counterparty_match_skipped_empty_directory",
                zoom_id=row.zoom_id,
            )
            return 0
        try:
            mentions = extract_counterparty_mentions(
                row.transcript_text,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                trace_source="zoom",
                trace_recording_id=row.zoom_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "zoom_counterparty_extract_unexpected_error",
                zoom_id=row.zoom_id, error=str(e),
            )
            return 0
        if not mentions:
            log.info(
                "zoom_counterparty_no_mentions_in_transcript",
                zoom_id=row.zoom_id,
            )
            return 0
        try:
            mention_to_id = resolve_mentions_to_directory(
                mentions,
                directory,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                batch_size=self._settings.counterparty_resolve_batch_size,
                max_workers=self._settings.counterparty_resolve_max_workers,
                trace_source="zoom",
                trace_recording_id=row.zoom_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "zoom_counterparty_resolve_unexpected_error",
                zoom_id=row.zoom_id, error=str(e),
            )
            return 0
        by_id = {cp.id: cp for cp in directory}
        mention_to_canonical: dict[str, str] = {}
        canonical_norms: set[str] = set()
        for mention, cid in mention_to_id.items():
            if cid is not None and cid in by_id:
                cp = by_id[cid]
                mention_to_canonical[mention] = cp.name
                if cp.name_normalised:
                    canonical_norms.add(cp.name_normalised)
        if not hasattr(row, "_zm_canonical_map"):
            row.__dict__["_zm_canonical_map"] = mention_to_canonical
        # FR-CR-05-133 — stash unresolved mentions for the
        # post-match enrollment step (mirror of fireflies).
        unresolved = [
            mention for mention, cid in mention_to_id.items()
            if cid is None
        ]
        row.__dict__["_zm_unresolved_mentions"] = unresolved

        # FR-CR-05-129 follow-up — RACE FIX: refetch fresh ids
        # by name_normalised before insert so the listener
        # auto-pull (mid-flight wipe-and-replace) doesn't
        # invalidate our snapshot's ids.
        from app.models import Counterparty as _Counterparty
        fresh = (
            session.query(_Counterparty.id, _Counterparty.name_normalised)
            .filter(_Counterparty.name_normalised.in_(canonical_norms))
            .all()
        )
        norm_to_fresh_id: dict[str, int] = {n: i for i, n in fresh}

        session.query(CounterpartyMention).filter(
            CounterpartyMention.source_kind == "zoom",
            CounterpartyMention.source_id == row.zoom_id,
        ).delete()
        session.flush()
        unique_ids: set[int] = set()
        skipped_stale = 0
        for mention, cid in mention_to_id.items():
            if cid is None or cid not in by_id:
                continue
            cp = by_id[cid]
            fresh_id = norm_to_fresh_id.get(cp.name_normalised or "")
            if fresh_id is None:
                skipped_stale += 1
                continue
            if fresh_id in unique_ids:
                continue
            unique_ids.add(fresh_id)
            session.add(
                CounterpartyMention(
                    counterparty_id=fresh_id,
                    source_kind="zoom",
                    source_id=row.zoom_id,
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.flush()
        log.info(
            "zoom_counterparty_match_done",
            zoom_id=row.zoom_id,
            mentions=len(mentions), matched=len(unique_ids),
            skipped_stale_after_pull_race=skipped_stale,
        )
        return len(unique_ids)

    def _step_enroll_unresolved(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-133 — mirror of fireflies. Posts the
        «Track «<name>»? [Yes] [No]» widget to admin DMs for
        every unresolved counterparty mention from Pass 2."""
        unresolved = (
            row.__dict__.get("_zm_unresolved_mentions") or []
        )
        if not unresolved or self._sender is None or not getattr(
            self._sender, "enabled", False
        ):
            return 0
        from app.telegram_bot.handlers import admin_user_ids

        recipients_raw = sorted(admin_user_ids())
        recipient_ids: list[int] = []
        for uid in recipients_raw:
            try:
                recipient_ids.append(int(uid))
            except (TypeError, ValueError):
                continue
        if not recipient_ids:
            return 0

        # FR-CR-05-138 — switched from per-entity yes/no
        # widgets to a single batch multi-select widget.
        from app.services.counterparty_enrollment_batch import (
            post_enrollment_batch,
        )

        result = post_enrollment_batch(
            session,
            sender=self._sender,
            source_kind="zoom",
            source_id=row.zoom_id,
            meeting_title=row.title or "",
            unresolved_mentions=unresolved,
            recipient_user_ids=recipient_ids,
        )
        return result.batches_created

    def _step_canonicalize_task_names(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-129 / FR-CR-05-130 — universal LLM rewrite
        of task title/description to canonical names. Replaces
        the regex+SequenceMatcher fuzzy fallback (operator: «без
        regexp, как универсальное решение»)."""
        from app.models import Counterparty
        from app.services.counterparty_match import (
            canonicalize_task_content_via_llm,
        )

        tasks = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.zoom)
            .filter(Task.source_conversation_id == row.zoom_id)
            .filter(Task.deleted_at.is_(None))
            .all()
        )
        if not tasks:
            return 0
        directory = session.query(Counterparty).all()
        if not directory:
            return 0
        task_dicts = [
            {"id": t.id, "title": t.title, "description": t.description}
            for t in tasks
        ]
        try:
            rewrites_map = canonicalize_task_content_via_llm(
                task_dicts,
                directory,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                trace_source="zoom",
                trace_recording_id=row.zoom_id,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "zoom_canonicalize_tasks_unexpected_error",
                zoom_id=row.zoom_id, error=str(e),
            )
            return 0
        applied = 0
        by_id = {t.id: t for t in tasks}
        for tid, ch in rewrites_map.items():
            t = by_id.get(tid)
            if not t:
                continue
            if "title" in ch:
                t.title = ch["title"]
            if "description" in ch:
                t.description = ch["description"]
            applied += 1
        if applied:
            session.flush()
            log.info(
                "zoom_task_canonical_rewrite_applied",
                zoom_id=row.zoom_id,
                applied=applied,
            )
        return applied

    def _step_consolidate_tasks(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-131 — LLM consolidation pass (mirror of
        Fireflies). Merges sequential-phase tasks and splits
        composite topics."""
        from datetime import datetime as _dt, timezone as _tz

        from app.services.counterparty_match import (
            consolidate_tasks_via_llm,
        )

        tasks = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.zoom)
            .filter(Task.source_conversation_id == row.zoom_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
            .all()
        )
        if len(tasks) < 2:
            return len(tasks)
        task_dicts = [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "owner": t.owner_user_id,
                "owner_display_name": t.owner_display_name,
                "priority": t.priority.value if t.priority else "medium",
            }
            for t in tasks
        ]
        try:
            result = consolidate_tasks_via_llm(
                task_dicts,
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort or None
                ),
                trace_source="zoom",
                trace_recording_id=row.zoom_id,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "zoom_consolidate_tasks_unexpected_error",
                zoom_id=row.zoom_id, error=str(e),
            )
            return len(tasks)
        if not result:
            return len(tasks)
        by_id = {t.id: t for t in tasks}
        kept_ids: set[int] = set()
        for entry in result:
            merged_from = entry.get("merged_from") or []
            new_id = entry.get("id")
            if new_id is not None and new_id in by_id and len(merged_from) <= 1:
                t = by_id[new_id]
                if entry.get("title"):
                    t.title = entry["title"][:10_000]
                if entry.get("description"):
                    t.description = entry["description"]
                kept_ids.add(t.id)
                continue
            primary_id = merged_from[0] if merged_from else None
            if primary_id and primary_id in by_id:
                t = by_id[primary_id]
                if entry.get("title"):
                    t.title = entry["title"][:10_000]
                if entry.get("description"):
                    t.description = entry["description"]
                try:
                    t.priority = TaskPriority(entry.get("priority") or "medium")
                except ValueError:
                    pass
                kept_ids.add(t.id)
        soft_deleted = 0
        now = _dt.now(_tz.utc)
        for t in tasks:
            if t.id not in kept_ids:
                t.deleted_at = now
                soft_deleted += 1
        if kept_ids or soft_deleted:
            session.flush()
            log.info(
                "zoom_task_consolidate_applied",
                zoom_id=row.zoom_id,
                input=len(tasks),
                kept=len(kept_ids),
                soft_deleted=soft_deleted,
            )
        return len(kept_ids)

    def _run_participants_llm(
        self,
        row: ZoomRecording,
        known_employees: list[dict[str, Any]],
    ) -> list[str]:
        """FR-CR-05-146c — pure LLM call (no DB, no caching),
        suitable for running in a background thread."""
        if not row.transcript_text or not known_employees:
            return []
        try:
            from app.services.zoom_participants import (
                extract_zoom_participants_via_llm,
            )

            return extract_zoom_participants_via_llm(
                row.transcript_text,
                [
                    {
                        "real_name": (e.get("real_name") or "").strip(),
                        "role": e.get("role") or "",
                        "notes": e.get("notes") or "",
                    }
                    for e in known_employees
                ],
                llm_backend=self._llm,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
                meeting_title=row.title,
                trace_source="zoom",
                trace_recording_id=row.zoom_id,
            )
        except Exception as e:  # noqa: BLE001
            log.info(
                "zoom_participants_unexpected_error",
                zoom_id=row.zoom_id, error=str(e),
            )
            return []

    def _kickoff_team_participants_async(
        self,
        row: ZoomRecording,
        session: Session,
    ) -> None:
        """FR-CR-05-146c — start participants LLM in background
        thread so it overlaps with `_step_detailed_summary`'s
        own LLM call. Both read only `transcript_text`. Saves
        ~2 minutes per meeting (operator-pinned).

        We pre-fetch `known_employees` here on the calling
        thread (DB session is single-threaded). The thread call
        only does HTTP to OpenAI.
        """
        if row.__dict__.get("_zm_team_participants") is not None:
            return
        if row.__dict__.get("_zm_participants_future") is not None:
            return
        if not row.transcript_text:
            return
        from app.services.team_members import as_known_employees
        from concurrent.futures import ThreadPoolExecutor

        try:
            tm_rows = as_known_employees(session)
        except Exception:  # noqa: BLE001
            tm_rows = []
        if not tm_rows:
            row.__dict__["_zm_team_participants"] = []
            return
        # Snapshot the row attrs the thread needs so we don't
        # touch the SQLA-bound row from outside the main thread.
        row.__dict__["_zm_known_employees_snapshot"] = tm_rows
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="zm-participants",
        )
        future = executor.submit(self._run_participants_llm, row, tm_rows)
        # Schedule executor shutdown when the future is done so
        # we don't leak threads. `cancel_futures=False` because
        # we always want this one to complete.
        future.add_done_callback(lambda _f: executor.shutdown(wait=False))
        row.__dict__["_zm_participants_future"] = future
        log.info(
            "zoom_participants_kickoff_async",
            zoom_id=row.zoom_id, team_members_count=len(tm_rows),
        )

    def _ensure_team_participants(
        self,
        row: ZoomRecording,
        known_employees: list[dict[str, Any]],
    ) -> list[str]:
        """FR-CR-05-139 — extract once, cache on row.__dict__.

        FR-CR-05-146c — when the kickoff helper has already
        started a future (parallel with detailed_summary), wait
        for it instead of starting a fresh LLM call.

        Returns a list of canonical real_names from
        team_members.real_name (validated, no Whisper
        hallucinations). Empty list when no team_members are
        configured / LLM call fails / transcript missing.
        """
        cached = row.__dict__.get("_zm_team_participants")
        if cached is not None:
            return list(cached)
        if not row.transcript_text or not known_employees:
            row.__dict__["_zm_team_participants"] = []
            return []
        future = row.__dict__.get("_zm_participants_future")
        if future is not None:
            try:
                participants = future.result(timeout=600) or []
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_participants_future_failed",
                    zoom_id=row.zoom_id, error=str(e),
                )
                participants = []
            row.__dict__["_zm_participants_future"] = None
        else:
            participants = self._run_participants_llm(row, known_employees)
        # FR-CR-05-145 — Python-side defense for «не участвует
        # в X» / «не вести X-задачи» notes. Even when the LLM
        # ignores the rule (it did this on the Fundrising sync
        # rerun), drop forbidden teammates here. Topic source =
        # meeting title + detailed_summary excerpt + transcript
        # excerpt — covers cases where Zoom's auto-title is
        # «Artem Sokolov's Zoom Meeting» without the «Fundrising»
        # keyword.
        if participants:
            from app.services.team_members import (
                filter_participants_by_notes_forbid,
                infer_topic_keywords_from_text,
            )

            topic_text = " ".join([
                row.title or "",
                (row.detailed_summary or "")[:3000],
                (row.transcript_text or "")[:1500],
            ])
            topic_kw = infer_topic_keywords_from_text(topic_text)
            kept, dropped = filter_participants_by_notes_forbid(
                participants,
                known_employees=[
                    {
                        "real_name": (e.get("real_name") or "").strip(),
                        "notes": e.get("notes") or "",
                    }
                    for e in known_employees
                ],
                topic_keywords=topic_kw,
            )
            if dropped:
                log.info(
                    "zoom_participants_post_filter_applied",
                    zoom_id=row.zoom_id,
                    topic_keywords=topic_kw,
                    dropped=dropped, kept=kept,
                )
                from app.services.trace_log import trace_event as _zte_pf
                _zte_pf(
                    source="zoom", recording_id=row.zoom_id,
                    event="zoom_participants_post_filter_applied",
                    topic_keywords=topic_kw,
                    dropped=dropped, kept=kept,
                )
                participants = kept
        row.__dict__["_zm_team_participants"] = participants
        return participants

    # --- step 6: extract tasks --------------------------------

    def _step_extract_tasks(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """Run gpt-5.5 over the detailed summary to extract
        action items, materialise as Task rows with
        source_kind=zoom + source_permalink=share_url. Returns
        the count of new Tasks.

        FR-CR-05-129 — wipe prior Task rows for this recording
        before re-extracting so `--rerun` doesn't stack 100s of
        tasks across reruns."""
        if row.tasks_extracted:
            return row.tasks_extracted_count or 0
        if not row.detailed_summary:
            row.last_error = "no detailed summary for task extraction"
            return 0
        prior = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.zoom)
            .filter(Task.source_conversation_id == row.zoom_id)
            .filter(Task.deleted_at.is_(None))
            .all()
        )
        for t in prior:
            t.deleted_at = datetime.now(timezone.utc)
        if prior:
            session.flush()
            log.info(
                "zoom_extract_wiped_prior_tasks",
                zoom_id=row.zoom_id,
                wiped=len(prior),
            )

        # Same as Fireflies: feed known_employees so the LLM
        # routes owner_user_id to a real teammate.
        from app.services.team_members import as_known_employees

        try:
            known_employees = as_known_employees(session)
        except Exception:  # noqa: BLE001
            known_employees = []

        admin_uid = _admin_fallback_owner_id()
        emp_table = _render_known_employees_table(known_employees)

        # FR-CR-05-139 — extract real-name participants from
        # transcript (validated against team_members) so the LLM
        # can disambiguate identical first names (Rule 8).
        # Cached on row.__dict__ for short_summary reuse.
        team_participants = self._ensure_team_participants(
            row, known_employees,
        )
        participants_block = (
            "\n".join(f"  - {p}" for p in team_participants if p)
            or "  (нет данных)"
        )

        prompt_user = (
            f"Заголовок: {row.title or '(без названия)'}\n"
            f"Дата: {row.meeting_date.isoformat() if row.meeting_date else '—'}\n"
            "\nmeeting_participants (REAL NAMES of who was on this call,\n"
            "use to disambiguate identical first names — Rule 8):\n"
            f"{participants_block}\n"
            "\nИзвестные сотрудники:\n"
            f"{emp_table}\n\nПодробный отчёт:\n{row.detailed_summary}"
        )
        # FR-CR-05-129 — JSON-mode (no tools) so reasoning works.
        prompt_user = (
            "Return JSON: `{\"tasks\": [{\"title\": ..., "
            "\"description\": ..., \"owner\": ..., "
            "\"priority\": ...}, ...]}`. Empty list ok.\n\n"
            + prompt_user
        )
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=MEETING_TASKS_PROMPT,
                user_prompt=prompt_user,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
                response_format={"type": "json_object"},
            ) or ""
            try:
                import json as _json
                data = _json.loads(text) if text else {}
            except _json.JSONDecodeError:
                log.warning(
                    "zoom_task_extraction_json_parse_failed",
                    zoom_id=row.zoom_id,
                    text_preview=text[:200],
                )
                data = {}
        except Exception as e:  # noqa: BLE001
            row.last_error = f"task extraction failed: {e}"
            log.warning(
                "zoom_task_extraction_llm_failed",
                zoom_id=row.zoom_id,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=self._settings.fireflies_tasks_reasoning_effort,
                error=str(e),
            )
            return 0
        tasks = (data or {}).get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        # FR-CR-05-126 — full trace of what the LLM emitted.
        _z_raw_titles = [
            (t.get("title") or "")[:80]
            for t in tasks if isinstance(t, dict)
        ][:25]
        _z_raw_owners = [
            t.get("owner") for t in tasks if isinstance(t, dict)
        ][:25]
        log.info(
            "zoom_task_extraction_llm_returned",
            zoom_id=row.zoom_id,
            model=self._settings.fireflies_tasks_model,
            raw_count=len(tasks),
            raw_titles=_z_raw_titles,
            raw_owners=_z_raw_owners,
        )
        from app.services.trace_log import trace_event as _zte1
        _zte1(source="zoom", recording_id=row.zoom_id,
              event="task_extraction_llm_returned",
              model=self._settings.fireflies_tasks_model,
              raw_count=len(tasks),
              raw_titles=_z_raw_titles, raw_owners=_z_raw_owners)
        # FR-CR-05-120 follow-up — log when we got 0 tasks back
        # so the operator can tell «meeting was procedural, no
        # actions» from «model rejected the call» / «prompt
        # broke». Includes the model name for fast triage.
        if not tasks:
            log.info(
                "zoom_task_extraction_returned_empty",
                zoom_id=row.zoom_id,
                model=self._settings.fireflies_tasks_model,
                detailed_chars=len(row.detailed_summary or ""),
                hint=(
                    "either the meeting was procedural (no "
                    "actionable items) or the LLM call returned "
                    "an empty array — check `last_error` and the "
                    "model name"
                ),
            )
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        created = 0
        from app.persistence.tasks import normalize_task_title

        today = datetime.now(timezone.utc).date()
        for t in tasks:
            if not isinstance(t, dict):
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            try:
                title = normalize_task_title(title)
            except ValueError:
                continue
            llm_owner_raw = (t.get("owner") or "").strip() or None
            owner_uid = llm_owner_raw
            if owner_uid and owner_uid not in valid_ids:
                owner_uid = None
            # FR-CR-05-142a — never emit owner=null. Cascade
            # fallback: pick PRINCIPAL among present participants
            # whose notes don't forbid the topic; never the admin
            # row (FR-CR-05-134 still holds).
            owner_resolution = "llm" if owner_uid else "fallback_pending"
            if not owner_uid:
                from app.services.team_members import (
                    infer_topic_keywords_from_text,
                    pick_meeting_owner_fallback,
                )

                topic_text = " ".join(
                    [
                        row.title or "",
                        title or "",
                        (t.get("description") or "")[:300],
                    ]
                )
                owner_uid = pick_meeting_owner_fallback(
                    known_employees=known_employees,
                    participants_real_names=team_participants or [],
                    topic_keywords=infer_topic_keywords_from_text(topic_text),
                )
                owner_resolution = (
                    "fallback_principal" if owner_uid else "fallback_no_participants"
                )
            try:
                priority = TaskPriority(t.get("priority") or "medium")
            except ValueError:
                priority = TaskPriority.medium
            owner_display_name = None
            if owner_uid and known_employees:
                for e in known_employees:
                    if e.get("slack_user_id") == owner_uid:
                        owner_display_name = (
                            e.get("real_name")
                            or e.get("display_name")
                            or owner_uid
                        )
                        break
            log.info(
                "zoom_task_owner_resolved",
                zoom_id=row.zoom_id, title=title[:80],
                llm_owner=llm_owner_raw, final_owner=owner_uid,
                resolution=owner_resolution,
            )
            from app.services.trace_log import trace_event as _zte_o
            _zte_o(source="zoom", recording_id=row.zoom_id,
                   event="task_owner_resolved", title=title[:80],
                   llm_owner=llm_owner_raw, final_owner=owner_uid,
                   resolution=owner_resolution)
            try:
                task = Task(
                    title=title[:10_000],
                    description=(t.get("description") or "").strip() or None,
                    priority=priority,
                    status=TaskStatus.todo,
                    owner_user_id=owner_uid,
                    owner_display_name=owner_display_name,
                    due_date=today,
                    due_time=time(18, 0),  # FR-CR-05-63
                    is_current_week=True,
                    source_kind=TaskSourceKind.zoom,
                    # FR-CR-05-118 — wire join key so
                    # `JOIN zoom_recordings ON
                    #   z.zoom_id = t.source_conversation_id`
                    # finds the originating meeting. Mirrors
                    # the Fireflies path (FR-CR-05-39).
                    source_conversation_id=row.zoom_id,
                    source_message_ts=row.zoom_id,
                    source_permalink=row.zoom_share_url
                    or row.google_doc_url,
                    created_by_slack_user_id=admin_uid,
                )
                session.add(task)
                session.flush()
                # Initial status history row (None → todo).
                from app.models import TaskStatusHistory

                session.add(
                    TaskStatusHistory(
                        task_id=task.id,
                        from_status=None,
                        to_status=TaskStatus.todo,
                        changed_by_slack_user_id=admin_uid,
                        reason="zoom_extracted",
                        at=datetime.now(timezone.utc),
                    )
                )
                created += 1
                # Schedule Sheets / Google Tasks sync after the
                # outer commit lands.
                from app.sync.task_sync import schedule_sync_task

                schedule_sync_task(session, task.id)
                # FR-CR-05-120 follow-up — DM card posting moved
                # to a separate `_step_post_task_cards` step that
                # runs AFTER the short summary is sent. Operator
                # pinned: get the meeting overview first (Суть +
                # To-Do in one message), then dive into per-task
                # cards. Tasks created here just sit in the
                # session waiting for the post step.
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_task_create_failed",
                    title=title[:80],
                    error=str(e),
                )
        row.tasks_extracted = True
        row.tasks_extracted_count = created
        row.last_error = None
        return created

    def _step_verify_tasks(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-121 — second LLM pass to catch tasks missed
        by `_step_extract_tasks`. Symmetric to the Fireflies
        verifier; reuses the same prompt + tool schema."""
        from app.fireflies.prompts import (
            TASK_EXTRACTION_TOOL_DESCRIPTION as _DESC,
            TASK_EXTRACTION_TOOL_NAME as _NAME,
            TASK_EXTRACTION_TOOL_PARAMETERS as _PARAMS,
            TASK_VERIFICATION_SYSTEM,
        )
        from app.models import (
            Task,
            TaskPriority,
            TaskSourceKind,
            TaskStatus,
            TaskStatusHistory,
        )
        from app.persistence.tasks import normalize_task_title
        from app.services.team_members import as_known_employees
        from app.sync.task_sync import schedule_sync_task

        if not row.transcript_text or not row.detailed_summary:
            return 0
        existing = (
            session.query(Task)
            .filter(Task.source_kind == TaskSourceKind.zoom)
            .filter(Task.source_conversation_id == row.zoom_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
            .all()
        )
        existing_block = "\n".join(
            f"- {t.title}: {(t.description or '')[:300]} "
            f"[owner={t.owner_display_name or '—'}]"
            for t in existing
        ) or "  (no tasks were extracted on the first pass)"
        try:
            known_employees = as_known_employees(session)
        except Exception:  # noqa: BLE001
            known_employees = []
        emp_table = _render_known_employees_table(known_employees)
        prompt_user = (
            f"Заголовок: {row.title or '(без названия)'}\n"
            f"Дата: {row.meeting_date.isoformat() if row.meeting_date else '—'}\n"
            "\nИзвестные сотрудники:\n"
            f"{emp_table}\n\n"
            "Already-extracted tasks (DO NOT duplicate these):\n"
            + existing_block + "\n\n"
            "Транскрипт встречи:\n"
            + row.transcript_text
        )
        # FR-CR-05-129 — JSON-mode.
        prompt_user = (
            "Return JSON: `{\"tasks\": [{\"title\": ..., "
            "\"description\": ..., \"owner\": ..., "
            "\"priority\": ...}, ...]}`. Empty list ok.\n\n"
            + prompt_user
        )
        try:
            text = self._llm.complete_text(  # type: ignore[attr-defined]
                system_prompt=TASK_VERIFICATION_SYSTEM,
                user_prompt=prompt_user,
                model=self._settings.fireflies_tasks_model,
                reasoning_effort=(
                    self._settings.fireflies_tasks_reasoning_effort
                    or None
                ),
                response_format={"type": "json_object"},
            ) or ""
            try:
                import json as _json
                data = _json.loads(text) if text else {}
            except _json.JSONDecodeError:
                log.warning(
                    "zoom_task_verification_json_parse_failed",
                    zoom_id=row.zoom_id,
                    text_preview=text[:200],
                )
                data = {}
        except Exception as e:  # noqa: BLE001
            log.info(
                "zoom_task_verification_failed",
                zoom_id=row.zoom_id, error=str(e),
            )
            return 0
        new_tasks = (data or {}).get("tasks") or []
        if not isinstance(new_tasks, list):
            new_tasks = []
        _z_new_titles = [
            (t.get("title") or "")[:80]
            for t in new_tasks if isinstance(t, dict)
        ][:25]
        _z_new_owners = [
            t.get("owner") for t in new_tasks if isinstance(t, dict)
        ][:25]
        log.info(
            "zoom_task_verification_done",
            zoom_id=row.zoom_id,
            existing_count=len(existing),
            newly_added=len(new_tasks),
            new_titles=_z_new_titles,
            new_owners=_z_new_owners,
        )
        from app.services.trace_log import trace_event as _zte2
        _zte2(source="zoom", recording_id=row.zoom_id,
              event="task_verification_done",
              existing_count=len(existing), newly_added=len(new_tasks),
              new_titles=_z_new_titles, new_owners=_z_new_owners)
        if not new_tasks:
            return 0
        valid_ids = {e.get("slack_user_id") for e in known_employees}
        admin_uid = _admin_fallback_owner_id()
        today = datetime.now(timezone.utc).date()
        added = 0
        for t in new_tasks:
            if not isinstance(t, dict):
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            try:
                title = normalize_task_title(title)
            except ValueError:
                continue
            owner_uid = (t.get("owner") or "").strip() or None
            if owner_uid and owner_uid not in valid_ids:
                owner_uid = None
            # FR-CR-05-134 — same fix as the first extract loop:
            # do NOT force admin when LLM left owner null.
            owner_display_name = None
            if owner_uid and known_employees:
                for e in known_employees:
                    if e.get("slack_user_id") == owner_uid:
                        owner_display_name = (
                            e.get("real_name")
                            or e.get("display_name")
                            or owner_uid
                        )
                        break
            try:
                priority = TaskPriority(t.get("priority") or "medium")
            except ValueError:
                priority = TaskPriority.medium
            try:
                task = Task(
                    title=title[:10_000],
                    description=(t.get("description") or "").strip() or None,
                    priority=priority,
                    status=TaskStatus.todo,
                    owner_user_id=owner_uid,
                    owner_display_name=owner_display_name,
                    due_date=today,
                    due_time=time(18, 0),
                    is_current_week=True,
                    source_kind=TaskSourceKind.zoom,
                    source_conversation_id=row.zoom_id,
                    source_message_ts=row.zoom_id,
                    source_permalink=(
                        row.zoom_share_url or row.google_doc_url
                    ),
                    created_by_slack_user_id=admin_uid,
                )
                session.add(task)
                session.flush()
                session.add(
                    TaskStatusHistory(
                        task_id=task.id,
                        from_status=None,
                        to_status=TaskStatus.todo,
                        changed_by_slack_user_id=admin_uid,
                        reason="zoom_verified",
                        at=datetime.now(timezone.utc),
                    )
                )
                schedule_sync_task(session, task.id)
                added += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_task_verify_create_failed",
                    title=title[:80], error=str(e),
                )
        if added:
            row.tasks_extracted_count = (row.tasks_extracted_count or 0) + added
        return added

    def _step_post_task_cards(
        self, session: Session, row: ZoomRecording
    ) -> int:
        """FR-CR-05-120 follow-up — post a DM card per extracted
        Task ROW to admin/owner. Runs AFTER `_step_send_short_
        summary` so the operator gets the meeting overview first
        (Суть + To-Do in one message) and then per-task cards
        cascade in. Returns number of cards posted.

        FR-CR-05-146d — parallel posting via thread-pool, max 10
        concurrent (Telegram allows 30 msg/sec across chats). Each
        thread opens its own session via `session_scope()` so we
        don't share SQLA state across threads. Operator-pinned:
        «параллельные TG-вызовы … но аккуратно по 10 максимум»."""
        if (
            self._sender is None
            or not getattr(self._sender, "enabled", False)
        ):
            return 0
        from concurrent.futures import ThreadPoolExecutor

        from app.db import session_scope
        from app.models import Task as _Task
        from app.telegram_bot.cards import post_initial_card

        admin_uid = _admin_fallback_owner_id()
        task_ids = [
            t.id for t in (
                session.query(_Task.id)
                .filter(_Task.source_kind == TaskSourceKind.zoom)
                .filter(_Task.source_conversation_id == row.zoom_id)
                .filter(_Task.deleted_at.is_(None))
                .order_by(_Task.id.asc())
                .all()
            )
        ]
        if not task_ids:
            return 0

        sender = self._sender

        def _send_one(task_id: int) -> bool:
            with session_scope() as s:
                t = s.query(_Task).filter(_Task.id == task_id).first()
                if t is None:
                    return False
                try:
                    post_initial_card(
                        sender=sender,
                        session=s,
                        task=t,
                        chat_id=0,
                        reply_to_message_id=None,
                        author_user_id=admin_uid,
                    )
                    return True
                except Exception as e:  # noqa: BLE001
                    log.info(
                        "zoom_task_card_post_failed",
                        task_id=task_id, error=str(e),
                    )
                    return False

        workers = max(1, min(10, len(task_ids)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="zm-cards",
        ) as pool:
            results = list(pool.map(_send_one, task_ids))
        return sum(1 for r in results if r)

    # --- main entry-point -------------------------------------

    def process_one(
        self, session: Session, m: ZoomRecordingMeta
    ) -> ZoomPipelineReport:
        """Run all 6 steps for the given meeting. Idempotent —
        each step short-circuits when its flag is set on the
        DB row."""
        row = self._upsert_recording(session, m)
        report = ZoomPipelineReport(
            recording_id=row.id, zoom_id=row.zoom_id, title=row.title,
            errors=[],
        )
        row.attempts = (row.attempts or 0) + 1
        row.processed_at = datetime.now(timezone.utc)

        # FR-CR-05-122 — every step bracketed by `_trace_step`
        # so `grep zoom_step_(started|done|failed)` over the
        # listener log walks through a single recording's run
        # with `duration_ms` per step.
        from app.fireflies.pipeline import _trace_step
        ctx = {"zoom_id": row.zoom_id}

        # FR-CR-05-119 follow-up — order: detailed → tasks →
        # verify → doc → short → post_cards.
        # FR-CR-05-127 — transcribe receives session so it can
        # pull team + counterparty names for the Whisper bias
        # prompt. Other early steps don't need it.
        for label, fn in (
            ("download", lambda r: self._step_download_audio(r)),
            ("transcribe", lambda r: self._step_transcribe(r, session=session)),
            (
                "detailed_summary",
                lambda r: self._step_detailed_summary(r, session=session),
            ),
        ):
            with _trace_step("zoom", label, **ctx):
                ok = fn(row)
                if not ok:
                    if row.last_error:
                        report.errors.append(row.last_error)
                    log.info(
                        "zoom_recording_step_failed",
                        zoom_id=row.zoom_id, step=label,
                        err=row.last_error,
                    )
                    break

        # FR-CR-05-125 — match counterparties before tasks +
        # doc + short so all three surfaces can render the
        # «🔗 Контрагенты» block.
        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "match_counterparties", **ctx):
                    self._step_match_counterparties(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_counterparty_match_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )
            # FR-CR-05-133 — enrollment widgets for unresolved
            # mentions (mirror of fireflies). Failures here MUST
            # NOT break the rest of the pipeline.
            try:
                with _trace_step("zoom", "enroll_unresolved", **ctx):
                    self._step_enroll_unresolved(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_enroll_unresolved_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )

        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "extract_tasks", **ctx):
                    report.tasks_created = self._step_extract_tasks(
                        session, row
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "zoom_extract_tasks_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )

        # FR-CR-05-121 — verifier pass.
        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "verify_tasks", **ctx):
                    self._step_verify_tasks(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_task_verification_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )
        # FR-CR-05-129 — canonicalize task names from the Pass-2
        # mapping BEFORE dedupe, so all phonetic variants of
        # the same counterparty share one topic-prefix.
        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "canonicalize_task_names", **ctx):
                    self._step_canonicalize_task_names(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_task_canonicalize_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )
        # FR-CR-05-131 — LLM consolidation: merge sequential
        # phases + split composite topics.
        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "consolidate_tasks", **ctx):
                    self._step_consolidate_tasks(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_task_consolidate_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )
        # FR-CR-05-128 — final near-duplicate safety net.
        if row.detailed_summarised:
            try:
                from app.fireflies.pipeline import _dedupe_meeting_tasks
                with _trace_step("zoom", "dedupe_tasks", **ctx):
                    _dedupe_meeting_tasks(
                        session,
                        source_kind=TaskSourceKind.zoom,
                        conversation_id=row.zoom_id,
                    )
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_task_dedupe_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )
        # Recount after dedupe.
        from app.models import Task as _Task
        report.tasks_created = (
            session.query(_Task)
            .filter(_Task.source_kind == TaskSourceKind.zoom)
            .filter(_Task.source_conversation_id == row.zoom_id)
            .filter(_Task.deleted_at.is_(None))
            .count()
        )
        row.tasks_extracted_count = report.tasks_created

        if row.detailed_summarised:
            with _trace_step("zoom", "doc_export", **ctx):
                ok = self._step_doc_export(session, row)
                if not ok and row.last_error:
                    report.errors.append(row.last_error)

        if row.detailed_summarised:
            with _trace_step("zoom", "short_summary", **ctx):
                ok = self._step_short_summary(session, row)
                if not ok and row.last_error:
                    report.errors.append(row.last_error)

        # FR-CR-05-120 follow-up — DM cards posted LAST.
        if row.detailed_summarised:
            try:
                with _trace_step("zoom", "post_task_cards", **ctx):
                    self._step_post_task_cards(session, row)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "zoom_post_task_cards_unexpected_error",
                    zoom_id=row.zoom_id, error=str(e),
                )

        report.transcript_chars = len(row.transcript_text or "")
        report.detailed_chars = len(row.detailed_summary or "")
        report.short_chars = len(row.short_summary or "")
        report.google_doc_url = row.google_doc_url

        # FR-CR-05-126 — single end-of-pipeline summary log.
        # NB: Task / TaskSourceKind are imported at module level
        # (line 44); a local re-import here would create a
        # local binding and shadow earlier references in
        # process_one (UnboundLocalError).
        from app.models import Counterparty, CounterpartyMention

        cp_matches = (
            session.query(Counterparty.name)
            .join(
                CounterpartyMention,
                CounterpartyMention.counterparty_id == Counterparty.id,
            )
            .filter(CounterpartyMention.source_kind == "zoom")
            .filter(CounterpartyMention.source_id == row.zoom_id)
            .order_by(CounterpartyMention.id.asc())
            .all()
        )
        recent_tasks = (
            session.query(Task.title, Task.owner_display_name)
            .filter(Task.source_kind == TaskSourceKind.zoom)
            .filter(Task.source_conversation_id == row.zoom_id)
            .filter(Task.deleted_at.is_(None))
            .order_by(Task.id.asc())
            .all()
        )
        _z_summary = dict(
            title=(row.title or "")[:80],
            transcript_chars=report.transcript_chars,
            detailed_chars=report.detailed_chars,
            short_chars=report.short_chars,
            tasks_count=len(recent_tasks),
            tasks_titles=[t.title[:80] for t in recent_tasks][:25],
            tasks_owners=[
                t.owner_display_name for t in recent_tasks
            ][:25],
            counterparties_count=len(cp_matches),
            counterparties=[{"name": row_[0]} for row_ in cp_matches][:25],
            google_doc_url=row.google_doc_url,
            errors=report.errors,
        )
        log.info("zoom_pipeline_summary", zoom_id=row.zoom_id, **_z_summary)
        from app.services.trace_log import trace_event as _zte3
        _zte3(source="zoom", recording_id=row.zoom_id,
              event="pipeline_summary", **_z_summary)
        return report


__all__ = ["ZoomPipeline", "ZoomPipelineReport"]
